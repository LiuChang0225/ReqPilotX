from __future__ import annotations

import json
import unittest

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from scripts.pdf_body_review_artifacts import (  # noqa: E402
    validate_pdf_body_review_draft,
)
from scripts.pdf_body_review_candidate import (  # noqa: E402
    PdfBodyReviewCandidateError,
    prepare_pdf_body_review_candidate_draft,
)
from scripts.pdf_body_review_table_capture import (  # noqa: E402
    convert_bailian_document_parsing_tables,
)
from tests.test_extraction_case_runner_body_sequence import (  # noqa: E402
    PAGE_IMAGE_SHA256,
    SOURCE_SHA256,
    VISUAL_TABLE_PROVIDER_VERSION,
    _asset,
    _body_snapshot,
    _image_node,
)


class PdfBodyReviewCandidateTests(unittest.TestCase):
    def test_native_candidate_is_text_free_and_requires_explicit_dispositions(
        self,
    ) -> None:
        snapshot = _body_snapshot()
        snapshot.nodes = (
            *snapshot.nodes,
            _image_node("image-one", "asset-one", block_index=5),
        )
        snapshot.assets = (_asset("asset-one", "1" * 64),)

        unresolved = self.prepare_native(snapshot)
        page = unresolved["profile"]["pages"][0]
        self.assertEqual(page["expected_image_count"], 0)
        self.assertEqual(page["expected_table_count"], 0)
        self.assertEqual(page["image_occurrences"][0]["disposition"], "UNRESOLVED")
        self.assertEqual(page["table_occurrences"][0]["disposition"], "UNRESOLVED")

        resolved = self.prepare_native(
            snapshot,
            image_dispositions_by_occurrence={
                page["image_occurrences"][0]["occurrence_sha256"]: "CONTENT"
            },
            table_dispositions_by_occurrence={
                page["table_occurrences"][0]["occurrence_sha256"]: "CONTENT"
            },
        )
        validated = validate_pdf_body_review_draft(resolved)
        resolved_page = validated["profile"]["pages"][0]
        self.assertEqual(resolved_page["expected_body_line_count"], 5)
        self.assertEqual(resolved_page["expected_image_count"], 1)
        self.assertEqual(resolved_page["expected_table_count"], 1)
        self.assertEqual(resolved_page["expected_table_cell_count"], 3)
        rendered = json.dumps(validated, ensure_ascii=False)
        self.assertNotIn("Alpha requirement", rendered)
        self.assertNotIn("Header", rendered)
        self.assertNotIn("raw_text", rendered)
        self.assertNotIn("text_lines", rendered)

    def test_non_content_occurrences_remain_bound_but_do_not_affect_counts(
        self,
    ) -> None:
        snapshot = _body_snapshot()
        unresolved = self.prepare_native(snapshot)
        occurrence_id = unresolved["profile"]["pages"][0]["table_occurrences"][0][
            "occurrence_sha256"
        ]

        resolved = self.prepare_native(
            snapshot,
            table_dispositions_by_occurrence={occurrence_id: "NON_CONTENT"},
        )

        page = resolved["profile"]["pages"][0]
        self.assertEqual(page["expected_table_count"], 0)
        self.assertEqual(page["table_occurrences"][0]["disposition"], "NON_CONTENT")

    def test_stale_decision_occurrence_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            PdfBodyReviewCandidateError,
            "candidate_table_decision_unknown_occurrence",
        ):
            self.prepare_native(
                _body_snapshot(),
                table_dispositions_by_occurrence={"f" * 64: "CONTENT"},
            )

    def test_remote_candidate_binds_visual_text_and_document_table_capture(
        self,
    ) -> None:
        snapshot = _body_snapshot()
        capture = _remote_table_capture()
        unresolved = prepare_pdf_body_review_candidate_draft(
            snapshot,
            assertion_id="FMT-005-A4",
            document_id="DOC-SYNTHETIC-PDF",
            body_page_start=2,
            body_page_end=2,
            review_mode="REMOTE_VISUAL",
            remote_table_captures_by_page={2: capture},
            approved_visual_provider="bailian",
        )
        occurrence_id = unresolved["profile"]["pages"][0]["table_occurrences"][0][
            "occurrence_sha256"
        ]

        resolved = prepare_pdf_body_review_candidate_draft(
            snapshot,
            assertion_id="FMT-005-A4",
            document_id="DOC-SYNTHETIC-PDF",
            body_page_start=2,
            body_page_end=2,
            review_mode="REMOTE_VISUAL",
            table_dispositions_by_occurrence={occurrence_id: "CONTENT"},
            remote_table_captures_by_page={2: capture},
            approved_visual_provider="bailian",
        )

        profile = validate_pdf_body_review_draft(resolved)["profile"]
        page = profile["pages"][0]
        self.assertEqual(profile["visual_provider_id"], "aliyun-bailian-qwen-ocr")
        self.assertEqual(page["page_image_sha256"], PAGE_IMAGE_SHA256)
        self.assertEqual(page["expected_body_line_count"], 5)
        self.assertEqual(page["expected_table_count"], 1)
        self.assertEqual(page["expected_table_row_count"], 2)
        self.assertEqual(page["expected_table_cell_count"], 3)

    def test_remote_capture_must_match_visual_page_image(self) -> None:
        capture = _remote_table_capture(page_image_sha256="e" * 64)
        with self.assertRaisesRegex(
            PdfBodyReviewCandidateError,
            "candidate_table_text_page_image_mismatch",
        ):
            prepare_pdf_body_review_candidate_draft(
                _body_snapshot(),
                assertion_id="FMT-005-A4",
                document_id="DOC-SYNTHETIC-PDF",
                body_page_start=2,
                body_page_end=2,
                review_mode="REMOTE_VISUAL",
                remote_table_captures_by_page={2: capture},
                approved_visual_provider="bailian",
            )

    def prepare_native(self, snapshot, **kwargs):
        return prepare_pdf_body_review_candidate_draft(
            snapshot,
            assertion_id="FMT-005-A4",
            document_id="DOC-SYNTHETIC-PDF",
            body_page_start=2,
            body_page_end=2,
            review_mode="NATIVE",
            **kwargs,
        )


def _remote_table_capture(*, page_image_sha256: str = PAGE_IMAGE_SHA256):
    return convert_bailian_document_parsing_tables(
        {
            "output": {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {
                                    "ocr_result": {
                                        "layouts": [
                                            {
                                                "type": "table",
                                                "uniqueId": "table-remote",
                                                "pageNum": 0,
                                                "index": 4,
                                                "pos": [
                                                    {"x": 10, "y": 100},
                                                    {"x": 400, "y": 100},
                                                    {"x": 400, "y": 260},
                                                    {"x": 10, "y": 260},
                                                ],
                                                "numRow": 2,
                                                "numCol": 2,
                                                "cells": [
                                                    {
                                                        "xsc": 0,
                                                        "xec": 1,
                                                        "ysc": 0,
                                                        "yec": 0,
                                                        "layouts": [{"text": "Header"}],
                                                    },
                                                    {
                                                        "xsc": 0,
                                                        "xec": 0,
                                                        "ysc": 1,
                                                        "yec": 1,
                                                        "layouts": [{"text": "Left"}],
                                                    },
                                                    {
                                                        "xsc": 1,
                                                        "xec": 1,
                                                        "ysc": 1,
                                                        "yec": 1,
                                                        "layouts": [{"text": "Right"}],
                                                    },
                                                ],
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        },
        source_sha256=SOURCE_SHA256,
        page_number=2,
        page_bbox=(0.0, 0.0, 612.0, 792.0),
        page_image_sha256=page_image_sha256,
        page_image_media_type="image/png",
        page_image_width_px=612,
        page_image_height_px=792,
        provider_version=VISUAL_TABLE_PROVIDER_VERSION,
    )


if __name__ == "__main__":
    unittest.main()
