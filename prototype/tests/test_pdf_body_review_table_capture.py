from __future__ import annotations

import copy
import re
import unittest

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from scripts.pdf_body_review_table_capture import (  # noqa: E402
    BAILIAN_DOCUMENT_PARSING_PROVIDER_ID,
    BAILIAN_DOCUMENT_PARSING_TASK,
    BAILIAN_DOCUMENT_TABLE_PARSER_VERSION,
    TABLE_CAPTURE_SCHEMA_VERSION,
    PdfBodyReviewTableCaptureError,
    build_bailian_document_parsing_provider_version,
    canonical_review_table_occurrence_sha256,
    convert_bailian_document_parsing_tables,
    validate_bailian_document_parsing_table_capture,
)


SOURCE_SHA256 = "a" * 64
PAGE_IMAGE_SHA256 = "b" * 64
PROVIDER_VERSION = build_bailian_document_parsing_provider_version(
    model="qwen3.5-ocr",
    seed=1234,
    temperature=0.01,
)


class PdfBodyReviewTableCaptureTests(unittest.TestCase):
    def convert(self, response=None, **overrides):
        arguments = {
            "source_sha256": SOURCE_SHA256,
            "page_number": 18,
            "page_bbox": (10.0, 20.0, 210.0, 420.0),
            "page_image_sha256": PAGE_IMAGE_SHA256,
            "page_image_media_type": "image/png",
            "page_image_width_px": 1_000,
            "page_image_height_px": 2_000,
            "provider_version": PROVIDER_VERSION,
        }
        arguments.update(overrides)
        return convert_bailian_document_parsing_tables(
            _response([]) if response is None else response,
            **arguments,
        )

    def assert_capture_error(self, code: str):
        return self.assertRaisesRegex(
            PdfBodyReviewTableCaptureError,
            rf"^{re.escape(code)}$",
        )

    def test_converts_exact_page_binding_spans_text_and_top_level_dedup(self) -> None:
        table = _merged_table(unique_id="table-upper", index=9, y_offset=200)
        duplicate = copy.deepcopy(table)
        response = _response(
            [
                _text_layout(unique_id="text-1"),
                table,
                copy.deepcopy(_text_layout(unique_id="text-1")),
                duplicate,
            ]
        )

        capture = self.convert(response)

        self.assertEqual(capture["schema_version"], TABLE_CAPTURE_SCHEMA_VERSION)
        self.assertEqual(capture["source_sha256"], SOURCE_SHA256)
        self.assertEqual(capture["page_number"], 18)
        self.assertEqual(capture["page_bbox"], [10.0, 20.0, 210.0, 420.0])
        self.assertEqual(capture["page_image_sha256"], PAGE_IMAGE_SHA256)
        self.assertEqual(capture["page_image_media_type"], "image/png")
        self.assertEqual(capture["page_image_width_px"], 1_000)
        self.assertEqual(capture["page_image_height_px"], 2_000)
        self.assertEqual(
            capture["provider_id"],
            BAILIAN_DOCUMENT_PARSING_PROVIDER_ID,
        )
        self.assertEqual(capture["provider_version"], PROVIDER_VERSION)
        self.assertEqual(capture["task"], BAILIAN_DOCUMENT_PARSING_TASK)
        self.assertEqual(
            capture["parser_version"],
            BAILIAN_DOCUMENT_TABLE_PARSER_VERSION,
        )
        self.assertEqual(capture["table_count"], 1)

        occurrence = capture["tables"][0]
        self.assertEqual(occurrence["source_order"], 0)
        self.assertEqual(occurrence["provider_index"], 9)
        self.assertEqual(occurrence["table_bbox"], [30.0, 60.0, 190.0, 220.0])
        self.assertEqual(occurrence["row_count"], 2)
        self.assertEqual(occurrence["column_count"], 2)
        self.assertEqual(occurrence["disposition"], "UNRESOLVED")
        self.assertRegex(occurrence["occurrence_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            occurrence["cells"],
            [
                {
                    "row": 0,
                    "column": 0,
                    "row_span": 2,
                    "column_span": 1,
                    "raw_text": "AA",
                },
                {
                    "row": 0,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "raw_text": "B",
                },
                {
                    "row": 1,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 1,
                    "raw_text": "C",
                },
            ],
        )

    def test_occurrence_hash_excludes_disposition_but_binds_page_image_and_text(
        self,
    ) -> None:
        capture = self.convert(_response([_merged_table()]))
        occurrence = capture["tables"][0]
        expected = occurrence["occurrence_sha256"]

        changed_disposition = copy.deepcopy(occurrence)
        changed_disposition["disposition"] = "APPROVED"
        self.assertEqual(
            canonical_review_table_occurrence_sha256(
                capture,
                changed_disposition,
            ),
            expected,
        )

        changed_image = copy.deepcopy(capture)
        changed_image["page_image_sha256"] = "c" * 64
        self.assertNotEqual(
            canonical_review_table_occurrence_sha256(changed_image, occurrence),
            expected,
        )

        changed_text = copy.deepcopy(occurrence)
        changed_text["cells"][0]["raw_text"] = "different"
        self.assertNotEqual(
            canonical_review_table_occurrence_sha256(capture, changed_text),
            expected,
        )

    def test_canonical_table_order_is_spatial_then_provider_index(self) -> None:
        lower = _merged_table(unique_id="lower", index=1, y_offset=1_000)
        upper_later_index = _merged_table(
            unique_id="upper-z",
            index=7,
            y_offset=100,
        )
        upper_earlier_index = _merged_table(
            unique_id="upper-a",
            index=3,
            y_offset=100,
        )

        capture = self.convert(
            _response([lower, upper_later_index, upper_earlier_index])
        )

        self.assertEqual(
            [table["provider_unique_id"] for table in capture["tables"]],
            ["upper-a", "upper-z", "lower"],
        )
        self.assertEqual(
            [table["source_order"] for table in capture["tables"]],
            [0, 1, 2],
        )

    def test_zero_table_snapshot_is_valid(self) -> None:
        capture = self.convert(_response([{"type": "text", "text": "ignored"}]))

        self.assertEqual(capture["table_count"], 0)
        self.assertEqual(capture["tables"], [])

    def test_conflicting_same_page_top_level_identity_fails_closed(self) -> None:
        first = _merged_table()
        conflicting = copy.deepcopy(first)
        conflicting["cells"][0]["layouts"][0]["text"] = "changed"

        with self.assert_capture_error("top_level_layout_identity_conflict"):
            self.convert(_response([first, conflicting]))

    def test_grid_overlap_gap_and_out_of_bounds_fail_closed(self) -> None:
        gap = _merged_table()
        gap["cells"].pop()
        overlap = _merged_table()
        overlap["cells"][2].update({"xsc": 0, "xec": 0})
        out_of_bounds = _merged_table()
        out_of_bounds["cells"][1]["xec"] = 2

        for code, table in (
            ("table_cell_coverage_gap", gap),
            ("table_cell_coverage_overlap", overlap),
            ("table_cell_span_out_of_bounds", out_of_bounds),
        ):
            with self.subTest(code=code), self.assert_capture_error(code):
                self.convert(_response([table]))

    def test_provider_quadrilateral_must_be_inside_image_and_non_degenerate(
        self,
    ) -> None:
        outside = _merged_table()
        outside["pos"][1]["x"] = 1_001
        degenerate = _merged_table()
        degenerate["pos"] = [
            {"x": 10, "y": 10},
            {"x": 20, "y": 20},
            {"x": 30, "y": 30},
            {"x": 40, "y": 40},
        ]

        for code, table in (
            ("table_position_outside_image", outside),
            ("table_position_degenerate", degenerate),
        ):
            with self.subTest(code=code), self.assert_capture_error(code):
                self.convert(_response([table]))

    def test_provider_version_binds_model_seed_temperature_task_and_parser(
        self,
    ) -> None:
        explicit = build_bailian_document_parsing_provider_version(
            model="qwen3.5-ocr",
            seed=1234,
            temperature=0.01,
            task="document_parsing",
            parser_version=BAILIAN_DOCUMENT_TABLE_PARSER_VERSION,
        )
        self.assertEqual(explicit, PROVIDER_VERSION)
        self.assertIn("model=qwen3.5-ocr", explicit)
        self.assertIn("task=document_parsing", explicit)
        self.assertIn("seed=1234", explicit)
        self.assertIn("temperature=0.01", explicit)
        self.assertIn(
            f"parser={BAILIAN_DOCUMENT_TABLE_PARSER_VERSION}",
            explicit,
        )
        self.assertNotEqual(
            explicit,
            build_bailian_document_parsing_provider_version(
                model="qwen3.5-ocr",
                seed=1235,
                temperature=0.01,
            ),
        )
        with self.assert_capture_error("provider_task_must_be_document_parsing"):
            build_bailian_document_parsing_provider_version(
                model="qwen3.5-ocr",
                seed=1234,
                temperature=0.01,
                task="table_parsing",
            )

    def test_validates_and_detaches_a_complete_normalized_capture(self) -> None:
        capture = self.convert(
            _response(
                [
                    _merged_table(unique_id="upper", index=7, y_offset=100),
                    _merged_table(unique_id="lower", index=1, y_offset=1_000),
                ]
            )
        )

        validated = validate_bailian_document_parsing_table_capture(capture)

        self.assertEqual(validated, capture)
        capture["tables"][0]["cells"][0]["raw_text"] = "mutated"
        self.assertEqual(validated["tables"][0]["cells"][0]["raw_text"], "AA")

    def test_capture_validator_requires_exact_fields_at_every_level(self) -> None:
        capture = self.convert(_response([_merged_table()]))
        variants = []

        top_level = copy.deepcopy(capture)
        top_level["extra"] = True
        variants.append(("capture_fields_invalid", top_level))

        table_level = copy.deepcopy(capture)
        table_level["tables"][0]["extra"] = True
        variants.append(("capture_table_fields_invalid", table_level))

        cell_level = copy.deepcopy(capture)
        cell_level["tables"][0]["cells"][0]["extra"] = True
        variants.append(("capture_table_cell_fields_invalid", cell_level))

        for code, candidate in variants:
            with self.subTest(code=code), self.assert_capture_error(code):
                validate_bailian_document_parsing_table_capture(candidate)

    def test_capture_validator_rechecks_binding_count_and_disposition(self) -> None:
        capture = self.convert(_response([_merged_table()]))
        variants = []

        wrong_task = copy.deepcopy(capture)
        wrong_task["task"] = "table_parsing"
        variants.append(("capture_task_mismatch", wrong_task))

        wrong_count = copy.deepcopy(capture)
        wrong_count["table_count"] = 0
        variants.append(("capture_table_count_mismatch", wrong_count))

        resolved = copy.deepcopy(capture)
        resolved["tables"][0]["disposition"] = "CONTENT"
        variants.append(("capture_table_disposition_must_be_unresolved", resolved))

        outside_page = copy.deepcopy(capture)
        outside_page["tables"][0]["table_bbox"][0] = 0.0
        variants.append(("capture_table_bbox_outside_page", outside_page))

        for code, candidate in variants:
            with self.subTest(code=code), self.assert_capture_error(code):
                validate_bailian_document_parsing_table_capture(candidate)

    def test_capture_validator_rechecks_source_and_canonical_orders(self) -> None:
        capture = self.convert(
            _response(
                [
                    _merged_table(unique_id="upper", index=7, y_offset=100),
                    _merged_table(unique_id="lower", index=1, y_offset=1_000),
                ]
            )
        )

        wrong_source_order = copy.deepcopy(capture)
        wrong_source_order["tables"][0]["source_order"] = 1
        with self.assert_capture_error("capture_table_source_order_mismatch"):
            validate_bailian_document_parsing_table_capture(wrong_source_order)

        wrong_spatial_order = copy.deepcopy(capture)
        wrong_spatial_order["tables"].reverse()
        _refresh_capture_occurrences(wrong_spatial_order)
        with self.assert_capture_error("capture_tables_not_canonical_order"):
            validate_bailian_document_parsing_table_capture(wrong_spatial_order)

        duplicate_provider_index = copy.deepcopy(capture)
        duplicate_provider_index["tables"][1]["provider_index"] = (
            duplicate_provider_index["tables"][0]["provider_index"]
        )
        _refresh_capture_occurrences(duplicate_provider_index)
        with self.assert_capture_error("capture_table_provider_index_duplicate"):
            validate_bailian_document_parsing_table_capture(duplicate_provider_index)

    def test_capture_validator_rechecks_cell_shape_coverage_and_order(self) -> None:
        capture = self.convert(_response([_merged_table()]))

        wrong_cell_order = copy.deepcopy(capture)
        wrong_cell_order["tables"][0]["cells"].reverse()
        with self.assert_capture_error("capture_table_cells_not_canonical_order"):
            validate_bailian_document_parsing_table_capture(wrong_cell_order)

        gap = copy.deepcopy(capture)
        gap["tables"][0]["cells"].pop()
        with self.assert_capture_error("capture_table_cell_coverage_gap"):
            validate_bailian_document_parsing_table_capture(gap)

        overlap = copy.deepcopy(capture)
        overlap["tables"][0]["cells"][2].update(
            {"row": 1, "column": 0, "row_span": 1, "column_span": 1}
        )
        with self.assert_capture_error("capture_table_cell_coverage_overlap"):
            validate_bailian_document_parsing_table_capture(overlap)

    def test_capture_validator_rechecks_occurrence_hash(self) -> None:
        capture = self.convert(_response([_merged_table()]))
        capture["tables"][0]["occurrence_sha256"] = "c" * 64

        with self.assert_capture_error("capture_table_occurrence_sha256_mismatch"):
            validate_bailian_document_parsing_table_capture(capture)


def _refresh_capture_occurrences(capture):
    for source_order, table in enumerate(capture["tables"]):
        table["source_order"] = source_order
        table["occurrence_sha256"] = canonical_review_table_occurrence_sha256(
            capture,
            table,
        )


def _response(layouts):
    return {
        "output": {
            "choices": [
                {"message": {"content": [{"ocr_result": {"layouts": layouts}}]}}
            ]
        }
    }


def _text_layout(*, unique_id: str):
    return {
        "type": "text",
        "uniqueId": unique_id,
        "pageNum": 0,
        "index": 0,
        "text": "non-table",
    }


def _merged_table(
    *,
    unique_id: str = "table-1",
    index: int = 4,
    y_offset: int = 200,
):
    return {
        "type": "table",
        "uniqueId": unique_id,
        "pageNum": 0,
        "index": index,
        "pos": [
            {"x": 100, "y": y_offset},
            {"x": 900, "y": y_offset},
            {"x": 900, "y": y_offset + 800},
            {"x": 100, "y": y_offset + 800},
        ],
        "numRow": 2,
        "numCol": 2,
        "cells": [
            {
                "xsc": 0,
                "xec": 0,
                "ysc": 0,
                "yec": 1,
                "layouts": [
                    {"uniqueId": "nested-same", "text": "A"},
                    {"uniqueId": "nested-same", "text": "A"},
                ],
            },
            {
                "xsc": 1,
                "xec": 1,
                "ysc": 0,
                "yec": 0,
                "layouts": [{"uniqueId": "nested-b", "text": "B"}],
            },
            {
                "xsc": 1,
                "xec": 1,
                "ysc": 1,
                "yec": 1,
                "layouts": [{"uniqueId": "nested-c", "text": "C"}],
            },
        ],
    }


if __name__ == "__main__":
    unittest.main()
