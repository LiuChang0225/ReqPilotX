from __future__ import annotations

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
    Path("/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype/storage/uploads"),
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
    def test_detects_and_rewrites_sample_priority_complex_zones(self) -> None:
        segments = [
            {"source_page": 13, "text": "2 主要设计和验证要求"},
            {"source_page": 17, "text": "|技术要求内容|Col2|Col3|Col4|Col5|\n|类别|技术要求|对应GB 44495章节|技术要求|要求解读|"},
            {"source_page": 18, "text": "安全升级及刷写\n漏洞管理"},
            {"source_page": 19, "text": "|通信安全要求|7.2.1|车辆与车辆制造商云平台通信时，应对其通信对象的身份真实性进行验证。|"},
            {"source_page": 20, "text": "网络安全\n7.2.11\n7.4.2"},
            {"source_page": 21, "text": "5.6.2.1.1\n汽车数据处理者至少应对图像或视频"},
            {"source_page": 22, "text": "数据安全\n5.6 个人信息传输"},
            {
                "source_page": 23,
                "text": "|序号 No.|网络安全活动 Cybersecurity Activity|\n|~~time~~ 1 总体要求 General Requirements|\n2.产品网络安全生命周期-概念设计阶段 Product Cybersecurity Lifecycle -Concept Phase",
            },
            {
                "source_page": 24,
                "text": "3.产品网络安全生命周期-开发阶段 Product Cybersecurity Lifecycle -Product Development\n4 产品网络安全生命周期-生产阶段 Product Cybersecurity Lifecycle -Production\n5 产品网络安全生命周期-运营维护阶段 Produt Cybersecurity Lifecycle -Operations & Maintenance",
            },
            {
                "source_page": 25,
                "text": "6 产品网络安全生命周期-报废/终止支持阶段 Product Cybersecurity Lifecycle -Decommissioning/End Support",
            },
        ]
        zones = detect_complex_table_zones(segments=segments, source_file_path="油泵-PDS.pdf")

        self.assertEqual([zone["family"] for zone in zones], ["merged_requirement_matrix", "lifecycle_matrix"])
        self.assertEqual(len(zones[0]["groups"]), 2)
        self.assertEqual(len(zones[1]["groups"]), 6)

        rows = [
            {"entry_id": "REQ-P13", "req_code": "REQ-P13", "entry_kind": "REQUIREMENT", "source_page": 13, "source_page_end": 13, "display_text": "page13", "raw_text": "page13", "requirement_text": "page13"},
            {"entry_id": "REQ-P17", "req_code": "REQ-P17", "entry_kind": "REQUIREMENT", "source_page": 17, "source_page_end": 17, "parent_section_label": "技术要求内容", "display_text": "技术要求内容", "raw_text": "技术要求内容", "requirement_text": "技术要求内容"},
            {"entry_id": "REQ-P18-LEGIT", "req_code": "REQ-P18-LEGIT", "entry_kind": "REQUIREMENT", "source_page": 18, "source_page_end": 18, "section_code": "8.9", "parent_section_label": "8.9 正常边界条目", "display_text": "正常边界条目", "raw_text": "正常边界条目", "requirement_text": "正常边界条目"},
            {"entry_id": "REQ-P20", "req_code": "REQ-P20", "entry_kind": "REQUIREMENT", "source_page": 20, "source_page_end": 20, "section_code": "7.2.12", "parent_section_label": "7.2.12 日志需要留存6个月以上", "display_text": "7.2.12 日志需要留存6个月以上", "raw_text": "7.2.12 日志需要留存6个月以上", "requirement_text": "7.2.12 日志需要留存6个月以上"},
            {"entry_id": "REQ-P24", "req_code": "REQ-P24", "entry_kind": "REQUIREMENT", "source_page": 24, "source_page_end": 24, "section_code": "4", "parent_section_label": "4 生产", "display_text": "4 产品网络安全生命周期-生产阶段", "raw_text": "4 产品网络安全生命周期-生产阶段", "requirement_text": "4 产品网络安全生命周期-生产阶段"},
            {"entry_id": "REQ-P24-LEGIT", "req_code": "REQ-P24-LEGIT", "entry_kind": "REQUIREMENT", "source_page": 24, "source_page_end": 24, "section_code": "8.1", "parent_section_label": "8.1 正常说明", "display_text": "正常说明", "raw_text": "正常说明", "requirement_text": "正常说明"},
        ]
        rewritten = rewrite_preview_rows_for_complex_tables(rows, zones=zones)

        self.assertTrue(any(row["entry_id"] == "REQ-P13" for row in rewritten))
        self.assertTrue(any(row["entry_id"] == "REQ-P18-LEGIT" for row in rewritten))
        self.assertTrue(any(row["entry_id"] == "REQ-P24-LEGIT" for row in rewritten))
        complex_rows = [row for row in rewritten if bool(row.get("complex_table_mode"))]
        self.assertEqual(len(complex_rows), 2)
        self.assertTrue(all(row.get("complex_role") == "group" for row in complex_rows))
        self.assertTrue(all(row.get("complex_split_level") == "zone" for row in complex_rows))
        self.assertTrue(all(row.get("keep") is True for row in complex_rows))
        self.assertTrue(all(row.get("complex_shadow_signatures") for row in complex_rows))

    def test_rewrite_sections_preserves_unrelated_boundary_sections(self) -> None:
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

        synthetic = [row for row in rewritten if row.get("complex_table_mode")]
        self.assertEqual(len(synthetic), 1)
        self.assertTrue(any(row.get("section_code") == "8.1" for row in rewritten))

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
    def test_build_preview_collapses_oil_pump_complex_table_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            preview = build_extraction_preview(
                str(_OIL_PUMP_SAMPLE_PDF),
                document_id="complex-preview-test",
                snapshot_root=Path(tmpdir),
            )

        zones = preview.get("complex_table_zones") or []
        self.assertEqual([zone.get("family") for zone in zones], ["merged_requirement_matrix", "lifecycle_matrix"])

        rows = preview.get("complete_requirement_entries") or []
        complex_rows = [row for row in rows if bool(row.get("complex_table_mode"))]
        self.assertEqual(len(complex_rows), 2)
        self.assertTrue(all(row.get("complex_role") == "group" for row in complex_rows))
        self.assertTrue(all(row.get("complex_split_level") == "zone" for row in complex_rows))
        self.assertTrue(
            all(
                bool(row.get("complex_table_mode"))
                for row in rows
                if 17 <= int(row.get("source_page") or 0) <= 25
            )
        )

        self.assertTrue(all(row.get("keep") is True for row in complex_rows))
        self.assertTrue(
            all(
                (row.get("table_refs") or [{}])[0].get("export_variant") == "source"
                and (row.get("table_refs") or [{}])[0].get("recommended_export_variant") == "source"
                and bool((row.get("table_refs") or [{}])[0].get("export_variant_locked"))
                for row in complex_rows
            )
        )

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
