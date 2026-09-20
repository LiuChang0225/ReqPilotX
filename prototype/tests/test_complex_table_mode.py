from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.complex_table_mode import (
    detect_complex_table_zones,
    rewrite_preview_rows_for_complex_tables,
    rewrite_preview_sections_for_complex_tables,
)
from app.services.extraction_preview import build_extraction_preview


_UPLOAD_DIR_CANDIDATES = [
    Path(__file__).resolve().parents[1] / "storage" / "uploads",
]
_OIL_PUMP_SAMPLE_PDF = next(
    (
        sample
        for upload_dir in _UPLOAD_DIR_CANDIDATES
        for sample in sorted(upload_dir.glob("*前后油泵总成*.pdf"))
    ),
    None,
)


class ComplexTableModeTests(unittest.TestCase):
    def test_filename_never_authorizes_a_special_complex_table_zone(self) -> None:
        segments = [
            {
                "source_page": 17,
                "text": (
                    "|技术要求内容|类别|对应章节|要求解读|\n"
                    "|网络安全|要求一|7.2.1|说明|\n"
                    "|数据安全|要求二|7.4.2|说明|"
                ),
            },
            {
                "source_page": 18,
                "text": (
                    "|技术要求内容|类别|对应章节|要求解读|\n"
                    "|网络安全|要求三|7.5.1|说明|\n"
                    "|数据安全|要求四|7.6.2|说明|"
                ),
            },
        ]
        named_like_a_fixture = detect_complex_table_zones(
            segments=segments,
            source_file_path="油泵-PDS.pdf",
        )
        ordinary_name = detect_complex_table_zones(
            segments=segments,
            source_file_path="standard.pdf",
        )

        self.assertEqual(named_like_a_fixture, ordinary_name)
        self.assertEqual(
            [zone["family"] for zone in ordinary_name],
            ["generic_complex_table"],
        )
        self.assertEqual(ordinary_name[0]["groups"], [])
        rows = [
            {"entry_id": "REQ-P17", "req_code": "REQ-P17", "entry_kind": "REQUIREMENT", "source_page": 17, "source_page_end": 17, "parent_section_label": "技术要求内容", "display_text": "技术要求内容", "raw_text": "技术要求内容", "requirement_text": "技术要求内容"},
            {"entry_id": "REQ-P18", "req_code": "REQ-P18", "entry_kind": "REQUIREMENT", "source_page": 18, "source_page_end": 18, "parent_section_label": "技术要求内容", "display_text": "技术要求内容", "raw_text": "技术要求内容", "requirement_text": "技术要求内容"},
        ]
        rewritten = rewrite_preview_rows_for_complex_tables(
            rows,
            zones=ordinary_name,
        )
        self.assertEqual(rewritten, rows)

    def test_historical_zone_metadata_cannot_collapse_sections(self) -> None:
        zones = [
            {
                "zone_id": "cx-lifecycle-matrix-p23-p25",
                "family": "lifecycle_matrix",
                "page_start": 23,
                "page_end": 25,
                "review_title": "产品网络安全生命周期矩阵",
                "groups": [
                    {"group_id": "g1", "label": "1 总体要求", "sort_order": 1, "page_start": 23, "page_end": 23, "section_code": "1", "section_title": "1 总体要求"},
                    {"group_id": "g2", "label": "2 概念设计", "sort_order": 2, "page_start": 23, "page_end": 24, "section_code": "2", "section_title": "2 概念设计"},
                ],
                "warnings": [],
                "source_only": True,
                "summary_review_only": True,
            }
        ]
        sections = [
            {"section_code": "", "section_title": "网络安全活动 Cybersecurity Activity", "text_preview": "网络安全活动 Cybersecurity Activity", "source_page_start": 23, "source_page_end": 23},
            {"section_code": "2", "section_title": "2 概念设计", "text_preview": "2 概念设计", "source_page_start": 23, "source_page_end": 24},
            {"section_code": "8.1", "section_title": "8.1 正常正文", "text_preview": "8.1 正常正文", "source_page_start": 24, "source_page_end": 24},
        ]

        rewritten = rewrite_preview_sections_for_complex_tables(sections, zones=zones)

        self.assertEqual(rewritten, sections)
        self.assertFalse(
            any(row.get("complex_table_mode") for row in rewritten)
        )

    def test_detects_generic_complex_table_candidates_without_collapse(self) -> None:
        segments = [
            {"source_page": 30, "text": "|列1|列2|列3|\n|对应章节|要求解读|备注|\n6.5\n6.7\n7.2.11"},
            {"source_page": 31, "text": "|列1|列2|列3|\n|band|phase|notes|\n5.1.1\n5.6.2\n7.4.2"},
        ]
        zones = detect_complex_table_zones(segments=segments, source_file_path="generic-sample.pdf")

        self.assertEqual([zone["family"] for zone in zones], ["generic_complex_table"])
        self.assertEqual(zones[0]["groups"], [])

        rows = [
            {"entry_id": "REQ-030", "req_code": "REQ-030", "entry_kind": "REQUIREMENT", "source_page": 30, "source_page_end": 30, "display_text": "普通条目", "raw_text": "普通条目", "requirement_text": "普通条目"},
            {"entry_id": "REQ-031", "req_code": "REQ-031", "entry_kind": "REQUIREMENT", "source_page": 31, "source_page_end": 31, "display_text": "普通条目2", "raw_text": "普通条目2", "requirement_text": "普通条目2"},
        ]
        rewritten = rewrite_preview_rows_for_complex_tables(rows, zones=zones)

        self.assertEqual([row["entry_id"] for row in rewritten], ["REQ-030", "REQ-031"])

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_build_preview_does_not_apply_filename_or_page_specific_collapse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            preview = build_extraction_preview(
                str(_OIL_PUMP_SAMPLE_PDF),
                document_id="complex-preview-test",
                snapshot_root=Path(tmpdir),
            )

        zones = preview.get("complex_table_zones") or []
        self.assertTrue(
            all(zone.get("family") == "generic_complex_table" for zone in zones)
        )

        rows = preview.get("complete_requirement_entries") or []
        complex_rows = [row for row in rows if bool(row.get("complex_table_mode"))]
        self.assertEqual(complex_rows, [])

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_build_preview_rewrites_complex_rows_after_generic_dedupe(self) -> None:
        from app.services import extraction_preview as extraction_preview_module

        real_build_section_unified_entries = extraction_preview_module._build_section_unified_entries

        def _assert_non_complex_then_build(entries):
            self.assertFalse(any(bool(row.get("complex_table_mode")) for row in entries))
            return real_build_section_unified_entries(entries)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                extraction_preview_module,
                "_build_section_unified_entries",
                side_effect=_assert_non_complex_then_build,
            ):
                build_extraction_preview(
                    str(_OIL_PUMP_SAMPLE_PDF),
                    document_id="complex-preview-dedupe-order-test",
                    snapshot_root=Path(tmpdir),
                )
