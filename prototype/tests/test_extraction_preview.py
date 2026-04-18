import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import extraction_preview


class ExtractionPreviewFallbackTests(unittest.TestCase):
    def test_default_table_snapshot_root_prefers_staged_symlink_parent(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            staged_dir = root / "staged"
            source_dir.mkdir()
            staged_dir.mkdir()
            source_pdf = source_dir / "doc.pdf"
            source_pdf.write_bytes(b"%PDF-1.7\n")
            staged_pdf = staged_dir / "doc.pdf"
            staged_pdf.symlink_to(source_pdf)

            with mock.patch.object(extraction_preview, "resolve_storage_dir") as mock_storage_dir:
                resolved = extraction_preview._default_table_snapshot_root(str(staged_pdf))

            self.assertEqual(resolved, staged_dir / "table_snapshots")
            self.assertTrue(resolved.exists())
            mock_storage_dir.assert_not_called()

    def test_default_table_snapshot_root_falls_back_to_storage_when_sidecar_unwritable(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_dir = root / "source"
            storage_dir = root / "storage"
            source_dir.mkdir()
            pdf_path = source_dir / "doc.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n")

            expected_candidate = source_dir / "table_snapshots"
            expected_fallback = (
                storage_dir
                / "table_snapshots"
                / "adhoc"
                / extraction_preview.hashlib.sha1(str(pdf_path).encode("utf-8")).hexdigest()[:12]
            )
            with mock.patch.object(extraction_preview, "_ensure_snapshot_root_writable", return_value=False) as mock_probe:
                with mock.patch.object(extraction_preview, "resolve_storage_dir", return_value=storage_dir):
                    resolved = extraction_preview._default_table_snapshot_root(str(pdf_path))

            self.assertEqual(resolved, expected_fallback)
            self.assertTrue(resolved.exists())
            mock_probe.assert_called_once_with(expected_candidate)

    def test_drawing_counts_as_semantic_visual_ignores_plain_fill_rect(self) -> None:
        drawing = {
            "type": "f",
            "fill": (1.0, 1.0, 0.0),
            "color": None,
            "width": None,
            "items": [("re", (0, 0, 240, 48))],
        }
        rect = mock.Mock(width=240.0, height=48.0)
        rect.get_area.return_value = 11520.0

        self.assertFalse(extraction_preview._drawing_counts_as_semantic_visual(drawing, rect))

    def test_drawing_counts_as_semantic_visual_keeps_stroked_flow_shape(self) -> None:
        drawing = {
            "type": "s",
            "fill": None,
            "color": (0.0, 0.0, 0.0),
            "width": 0.75,
            "items": [("l", (0, 0), (100, 0)), ("c", (100, 0), (110, 0), (110, 10), (100, 10))],
        }
        rect = mock.Mock(width=180.0, height=64.0)
        rect.get_area.return_value = 11520.0

        self.assertTrue(extraction_preview._drawing_counts_as_semantic_visual(drawing, rect))

    def test_text_block_counts_as_layout_table_label_detects_short_left_label(self) -> None:
        clip = mock.Mock(width=520.0, x0=44.0)
        block_rect = mock.Mock(width=68.0, x0=62.0, height=18.0)

        self.assertTrue(
            extraction_preview._text_block_counts_as_layout_table_label(
                "执行动作：",
                block_rect,
                clip,
            )
        )

    def test_row_needs_layout_table_snapshot_fallback_detects_multi_page_label_value_layout(self) -> None:
        row = {
            "table_refs": [],
        }

        self.assertTrue(
            extraction_preview._row_needs_layout_table_snapshot_fallback(
                row,
                section_window_meta={
                    "visual_block_count": 0,
                    "page_span_count": 4,
                    "left_label_block_count": 4,
                    "wide_body_block_count": 12,
                },
            )
        )

    def test_row_needs_layout_table_snapshot_fallback_skips_single_page_text_section(self) -> None:
        row = {
            "table_refs": [],
        }

        self.assertFalse(
            extraction_preview._row_needs_layout_table_snapshot_fallback(
                row,
                section_window_meta={
                    "visual_block_count": 0,
                    "page_span_count": 1,
                    "left_label_block_count": 4,
                    "wide_body_block_count": 12,
                },
            )
        )

    def test_raw_markdown_table_body_row_count_counts_body_rows(self) -> None:
        raw_text = (
            "2.1 承接功能清单\n"
            "|序号|系统编号|系统名称|\n"
            "|1|ASC_F/R|-|\n"
            "|2|ASC_F/R|D5-F9-3-UC01|\n"
            "|3|ASC_F/R|-|\n"
            "|4|ASC_F/R|D4-F12-8-UC01|"
        )

        self.assertEqual(extraction_preview._raw_markdown_table_body_row_count(raw_text), 4)

    def test_row_needs_full_page_table_fallback_when_structured_rows_are_missing(self) -> None:
        row = {
            "raw_text": (
                "2.1 承接功能清单\n"
                "|序号|系统编号|系统名称|\n"
                "|1|ASC_F/R|-|\n"
                "|2|ASC_F/R|D5-F9-3-UC01|\n"
                "|3|ASC_F/R|-|\n"
                "|4|ASC_F/R|D4-F12-8-UC01|"
            ),
            "table_refs": [
                {
                    "slice_render_mode": "structured",
                    "row_ids": ["r1", "r2", "r3"],
                }
            ],
        }

        self.assertTrue(extraction_preview._row_needs_full_page_table_fallback(row))

    def test_row_needs_full_page_table_fallback_skips_subtable_only_ref(self) -> None:
        row = {
            "section_code": "4.4.2",
            "section_title": "功能描述",
            "display_text": "4.4.2 功能描述\nASC_R:",
            "raw_text": (
                "4.4.2 功能描述\n"
                "|前置条件：|说明|\n"
                "|a|ASC_F|\n"
                "|b|ASC_R|\n"
                "|c|备注|\n"
                "|d|退出|"
            ),
            "table_refs": [
                {
                    "table_title": "ASC_R:",
                    "slice_render_mode": "structured",
                    "row_ids": ["r1", "r2", "r3"],
                }
            ],
        }

        self.assertFalse(extraction_preview._row_needs_full_page_table_fallback(row))

    def test_visual_snapshot_fallback_uses_visual_cue_text(self) -> None:
        row = {
            "section_title": "产品效果图",
            "display_text": "产品效果图",
            "raw_text": "",
            "table_refs": [],
            "source_page": 28,
            "source_page_end": 28,
        }

        self.assertTrue(extraction_preview._row_needs_visual_snapshot_fallback(row, {"page_details": []}))

    def test_visual_snapshot_fallback_uses_section_window_visual_signal(self) -> None:
        row = {
            "section_title": "功能描述",
            "display_text": "功能描述",
            "raw_text": "车辆上电\nVIU0 发送 NM 报文",
            "table_refs": [],
            "source_page": 22,
            "source_page_end": 23,
        }
        self.assertTrue(
            extraction_preview._row_needs_visual_snapshot_fallback(
                row,
                {"page_details": []},
                section_window_meta={"visual_block_count": 1},
            )
        )

    def test_visual_snapshot_fallback_skips_same_page_image_noise_without_section_visuals(self) -> None:
        row = {
            "section_title": "文件版本编号",
            "display_text": "1.6.2 文件版本编号",
            "raw_text": "版本编号由三层数字构成N1.N2.N3",
            "table_refs": [],
            "source_page": 6,
            "source_page_end": 6,
        }
        diagnostics = {
            "page_details": [
                {"page_no": 6, "image_count": 1, "graphics_count": 0},
            ]
        }

        self.assertFalse(
            extraction_preview._row_needs_visual_snapshot_fallback(
                row,
                diagnostics,
                section_window_meta={"visual_block_count": 0},
            )
        )

    def test_visual_snapshot_override_prefers_section_snapshot_for_multi_structured_visual_refs(self) -> None:
        row = {
            "section_title": "高压互锁检测流程",
            "display_text": "4.2.2.4 高压互锁检测流程",
            "raw_text": "检测方式1 示意图（参考）\n检测方式2 示意图（参考）",
            "table_refs": [
                {
                    "table_id": "structured-1",
                    "slice_render_mode": "structured",
                },
                {
                    "table_id": "structured-2",
                    "slice_render_mode": "structured",
                },
            ],
        }

        self.assertTrue(
            extraction_preview._row_prefers_visual_snapshot_over_structured_refs(
                row,
                section_window_meta={"visual_block_count": 1},
            )
        )

    def test_content_blocks_with_single_table_preserves_paragraph_order(self) -> None:
        blocks = [
            {"kind": "paragraph", "text": "前置说明"},
            {"kind": "table", "table_id": "old"},
            {"kind": "paragraph", "text": "尾部说明"},
        ]

        self.assertEqual(
            extraction_preview._content_blocks_with_single_table(blocks, "new"),
            [
                {"kind": "paragraph", "text": "前置说明"},
                {"kind": "table", "table_id": "new"},
                {"kind": "paragraph", "text": "尾部说明"},
            ],
        )

    def test_content_blocks_for_snapshot_fallback_compacts_table_fallback_to_title(self) -> None:
        row = {
            "display_text": "4.1.1 总线信号",
            "raw_text": (
                "4.1.1 总线信号\n"
                "|信号名称|信号类型|\n"
                "|ASCFL|CAN|\n"
                "0x1: Phase_Short_Circuit_Active"
            ),
            "content_blocks": [
                {"kind": "paragraph", "text": "4.1.1 总线信号"},
                {"kind": "paragraph", "text": "0x1: Phase_Short_Circuit_Active"},
            ],
        }

        self.assertEqual(
            extraction_preview._content_blocks_for_snapshot_fallback(
                row,
                "tbl-1",
                suffix="table_fallback",
            ),
            [
                {"kind": "paragraph", "text": "4.1.1 总线信号"},
                {"kind": "table", "table_id": "tbl-1"},
            ],
        )

    def test_content_blocks_for_snapshot_fallback_keeps_short_visual_lines(self) -> None:
        row = {
            "display_text": "9.3 安装位置",
            "raw_text": (
                "9.3 安装位置\n"
                "MPU_F 安装在前副车架前横梁内测，如下图所示：\n"
                "MPU_R 安装在后副车架后横梁外侧，如下图所示："
            ),
            "content_blocks": [
                {
                    "kind": "paragraph",
                    "text": (
                        "9.3 安装位置\n"
                        "MPU_F 安装在前副车架前横梁内测，如下图所示：\n"
                        "MPU_R 安装在后副车架后横梁外侧，如下图所示："
                    ),
                }
            ],
        }

        self.assertEqual(
            extraction_preview._content_blocks_for_snapshot_fallback(
                row,
                "tbl-9.3",
                suffix="visual_fallback",
            ),
            [
                {"kind": "paragraph", "text": "9.3 安装位置"},
                {"kind": "paragraph", "text": "MPU_F 安装在前副车架前横梁内测，如下图所示："},
                {"kind": "paragraph", "text": "MPU_R 安装在后副车架后横梁外侧，如下图所示："},
                {"kind": "table", "table_id": "tbl-9.3"},
            ],
        )

    def test_content_blocks_for_multi_visual_fallback_pairs_cue_lines(self) -> None:
        row = {
            "display_text": "9.3 安装位置",
            "raw_text": (
                "9.3 安装位置\n"
                "MPU_F 安装在前副车架前横梁内测，如下图所示：\n"
                "MPU_R 安装在后副车架后横梁外侧，如下图所示："
            ),
            "content_blocks": [
                {
                    "kind": "paragraph",
                    "text": (
                        "9.3 安装位置\n"
                        "MPU_F 安装在前副车架前横梁内测，如下图所示：\n"
                        "MPU_R 安装在后副车架后横梁外侧，如下图所示："
                    ),
                }
            ],
        }

        self.assertEqual(
            extraction_preview._content_blocks_for_multi_visual_fallback(
                row,
                [
                    {"table_id": "tbl-9.3-1"},
                    {"table_id": "tbl-9.3-2"},
                ],
            ),
            [
                {"kind": "paragraph", "text": "9.3 安装位置"},
                {"kind": "paragraph", "text": "MPU_F 安装在前副车架前横梁内测，如下图所示："},
                {"kind": "table", "table_id": "tbl-9.3-1"},
                {"kind": "paragraph", "text": "MPU_R 安装在后副车架后横梁外侧，如下图所示："},
                {"kind": "table", "table_id": "tbl-9.3-2"},
            ],
        )

    def test_row_needs_layout_table_snapshot_fallback_skips_verbose_section(self) -> None:
        row = {
            "raw_text": "\n".join(f"第{i}行内容" for i in range(40)),
            "display_text": "\n".join(f"第{i}行内容" for i in range(40)),
            "table_refs": [],
        }

        self.assertFalse(
            extraction_preview._row_needs_layout_table_snapshot_fallback(
                row,
                section_window_meta={
                    "visual_block_count": 0,
                    "page_span_count": 3,
                    "left_label_block_count": 8,
                    "wide_body_block_count": 8,
                },
            )
        )

    def test_repair_local_clause_pipe_table_preview_rebuilds_two_tables(self) -> None:
        row = {
            "entry_id": "REQ-4.4.2",
            "req_code": "REQ-4.4.2",
            "section_code": "4.4.2",
            "section_key": "MAIN:4.4.2",
            "section_instance_key": "MAIN:4.4.2",
            "section_title": "功能描述",
            "source_page": 24,
            "source_page_end": 25,
            "raw_text": (
                "4.4.2 功能描述\n"
                "|触发条件：|ASC_F 接收到 VIU0 发送的最大允许回收功率；|\n"
                "|执行动作：|ASC_F 在最大允许回收功率范围内进行能量回收|\n"
                "|退出条件：|/|\n"
                "ASC_R:\n"
                "|前置条件：|ASC_R 上高压；|\n"
                "|触发条件：|ASC_R 接收到 VIU0 发送的最大允许回收功率；|\n"
                "|执行动作：|ASC_R 在最大允许回收功率范围内进行能量回收|\n"
                "|退出条件：|/|"
            ),
            "table_refs": [
                {
                    "table_id": "broken",
                    "slice_render_mode": "structured",
                    "inherited_headers": ["退出条件：", "退出条件： ASC_R:", "/", "/"],
                }
            ],
            "content_blocks": [
                {"kind": "paragraph", "text": "4.4.2 功能描述"},
                {"kind": "table", "table_id": "broken"},
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            repaired, assets = extraction_preview._repair_local_clause_pipe_table_preview(
                row,
                document_id="doc",
                snapshot_root=Path(tmpdir),
                source_snapshot_path="/tmp/4.4.2-source.png",
                source_image_block_count=1,
            )

        self.assertIsNotNone(repaired)
        self.assertEqual(
            [ref["table_title"] for ref in repaired["table_refs"]],
            ["ASC_F:", "ASC_R:"],
        )
        self.assertEqual(
            [ref["inherited_headers"] for ref in repaired["table_refs"]],
            [[], []],
        )
        self.assertEqual(
            [ref["source_snapshot_path"] for ref in repaired["table_refs"]],
            ["/tmp/4.4.2-source.png", "/tmp/4.4.2-source.png"],
        )
        self.assertEqual(
            [block["kind"] for block in repaired["content_blocks"]],
            ["paragraph", "paragraph", "table", "paragraph", "table"],
        )
        self.assertEqual(len(assets), 2)

    def test_build_full_page_snapshot_ref_uses_source_snapshot_as_preview_image(self) -> None:
        row = {
            "section_code": "9.1",
            "section_title": "产品效果图",
            "section_key": "MAIN:9.1",
            "section_instance_key": "MAIN:9.1",
            "source_page": 28,
            "source_page_end": 28,
        }
        snapshot_path = Path("/tmp/cts-source.png")

        with mock.patch.object(
            extraction_preview,
            "_write_full_page_span_source_snapshot",
            return_value=(snapshot_path, {"image_block_count": 2}),
        ):
            ref, asset = extraction_preview._build_full_page_snapshot_ref(
                row,
                document_id="doc",
                snapshot_root=Path("/tmp"),
                source_file_path="/tmp/source.pdf",
                source_snapshot_cache={},
                suffix="visual_fallback",
            )

        self.assertIsNotNone(ref)
        self.assertIsNotNone(asset)
        self.assertEqual(ref["slice_snapshot_path"], str(snapshot_path))
        self.assertEqual(asset["snapshot_path"], str(snapshot_path))

    def test_sanitize_row_table_refs_drops_suspicious_duplicate_ref(self) -> None:
        row = {
            "table_refs": [
                {
                    "table_id": "bad",
                    "slice_render_mode": "structured",
                    "inherited_headers": ["退出条件：", "退出条件： ASC_R:", "/", "/"],
                },
                {
                    "table_id": "good",
                    "slice_render_mode": "structured",
                    "inherited_headers": ["ASC_R:", "说明"],
                },
            ],
            "content_blocks": [
                {"kind": "paragraph", "text": "4.4.2 功能描述"},
                {"kind": "table", "table_id": "bad"},
                {"kind": "paragraph", "text": "ASC_R:"},
                {"kind": "table", "table_id": "good"},
            ],
        }

        cleaned = extraction_preview._sanitize_row_table_refs(row)

        self.assertEqual([ref["table_id"] for ref in cleaned["table_refs"]], ["good"])
        self.assertEqual(
            cleaned["content_blocks"],
            [
                {"kind": "paragraph", "text": "4.4.2 功能描述"},
                {"kind": "paragraph", "text": "ASC_R:"},
                {"kind": "table", "table_id": "good"},
            ],
        )

    def test_strip_synthetic_local_clause_headers_keeps_headerless_kv_tables(self) -> None:
        row = {
            "table_refs": [
                {
                    "table_id": "t1",
                    "table_title": "ASC_F:",
                    "slice_render_mode": "structured",
                    "inherited_headers": ["ASC_F:", "说明"],
                    "slice_rows": [
                        ["触发条件：", "ASC_F 接收到 VIU0 发送的最大允许回收功率；"],
                        ["执行动作：", "ASC_F 在最大允许回收功率范围内进行能量回收"],
                        ["退出条件：", "/"],
                    ],
                },
                {
                    "table_id": "t2",
                    "table_title": "ASC_R:",
                    "slice_render_mode": "structured",
                    "inherited_headers": ["ASC_R:", "说明"],
                    "slice_rows": [
                        ["前置条件：", "ASC_R 上高压；"],
                        ["触发条件：", "ASC_R 接收到 VIU0 发送的最大允许回收功率；"],
                        ["执行动作：", "ASC_R 在最大允许回收功率范围内进行能量回收"],
                        ["退出条件：", "/"],
                    ],
                },
            ],
            "content_blocks": [
                {"kind": "paragraph", "text": "4.4.2 功能描述"},
                {"kind": "table", "table_id": "t1"},
                {"kind": "paragraph", "text": "ASC_R:"},
                {"kind": "table", "table_id": "t2"},
            ],
        }

        cleaned = extraction_preview._strip_synthetic_local_clause_headers(row)

        self.assertEqual([ref["inherited_headers"] for ref in cleaned["table_refs"]], [[], []])
        self.assertEqual(
            cleaned["content_blocks"],
            [
                {"kind": "paragraph", "text": "4.4.2 功能描述"},
                {"kind": "paragraph", "text": "ASC_F:"},
                {"kind": "table", "table_id": "t1"},
                {"kind": "paragraph", "text": "ASC_R:"},
                {"kind": "table", "table_id": "t2"},
            ],
        )

    def test_recover_layout_headerless_tables_from_line_specs_builds_subject_groups(self) -> None:
        line_specs = [
            {"text": "4.1.2.2 功能策略", "bbox": (56.0, 60.0, 140.0, 72.0), "clip_x0": 44.0, "page_index": 15},
            {"text": "ASC_F 接收到控制方式 req", "bbox": (150.0, 86.0, 520.0, 98.0), "clip_x0": 44.0, "page_index": 16},
            {"text": "到使能信号 valid", "bbox": (150.0, 118.0, 520.0, 130.0), "clip_x0": 44.0, "page_index": 16},
            {"text": "ASC_F 触发条", "bbox": (62.0, 164.0, 128.0, 176.0), "clip_x0": 44.0, "page_index": 16},
            {"text": "参数补充", "bbox": (150.0, 180.0, 520.0, 192.0), "clip_x0": 44.0, "page_index": 16},
            {"text": "件：", "bbox": (62.0, 195.0, 86.0, 207.0), "clip_x0": 44.0, "page_index": 16},
            {"text": "更多触发条件", "bbox": (150.0, 211.0, 520.0, 223.0), "clip_x0": 44.0, "page_index": 16},
            {
                "text": "a.若满足触发条件 a ， ASC_F 发送当前工作状态",
                "bbox": (150.0, 305.0, 540.0, 317.0),
                "clip_x0": 44.0,
                "page_index": 16,
            },
            {"text": "System_Active", "bbox": (167.0, 336.0, 320.0, 348.0), "clip_x0": 44.0, "page_index": 16},
            {"text": "执行动作：", "bbox": (62.0, 367.0, 118.0, 379.0), "clip_x0": 44.0, "page_index": 16},
            {"text": "/转矩控制", "bbox": (167.0, 399.0, 420.0, 411.0), "clip_x0": 44.0, "page_index": 16},
            {"text": "备注：", "bbox": (56.0, 477.0, 92.0, 489.0), "clip_x0": 44.0, "page_index": 16},
            {"text": "1. ASC_F 支持左右两轮单独控制；", "bbox": (77.0, 508.0, 260.0, 520.0), "clip_x0": 44.0, "page_index": 16},
            {"text": "ASC_R 触发条件", "bbox": (62.0, 387.0, 142.0, 399.0), "clip_x0": 44.0, "page_index": 17},
            {"text": "a. ASC_R 接收到控制方式", "bbox": (150.0, 387.0, 420.0, 399.0), "clip_x0": 44.0, "page_index": 17},
            {"text": "执行动作", "bbox": (62.0, 606.0, 114.0, 618.0), "clip_x0": 44.0, "page_index": 17},
            {"text": "/转矩控制", "bbox": (167.0, 637.0, 420.0, 649.0), "clip_x0": 44.0, "page_index": 17},
            {"text": "备注：", "bbox": (66.0, 74.0, 102.0, 86.0), "clip_x0": 44.0, "page_index": 18},
            {"text": "1. ASC_R 支持左右两轮单独控制；", "bbox": (66.0, 105.0, 250.0, 117.0), "clip_x0": 44.0, "page_index": 18},
        ]

        groups = extraction_preview._recover_layout_headerless_tables_from_line_specs(
            line_specs,
            section_heading_tokens={"4.1.2.2功能策略", "功能策略", "4.1.2.2"},
        )

        self.assertEqual([group["title"] for group in groups], ["ASC_F:", "ASC_R:"])
        self.assertEqual([row[0] for row in groups[0]["rows"]], ["触发条件：", "执行动作：", "备注："])
        self.assertTrue(groups[0]["rows"][1][1].startswith("a.若满足触发条件"))
        self.assertIn("1. ASC_R 支持左右两轮单独控制；", groups[1]["rows"][2][1])

    def test_extract_section_level_tail_notes_from_layout_groups_moves_cross_subject_note_out(self) -> None:
        groups = [
            {
                "title": "ASC_F:",
                "rows": [["前置条件：", "a.ASC_F上高压；"], ["退出条件：", "/"]],
                "row_specs": [
                    {"values": ["前置条件：", "a.ASC_F上高压；"], "used_indexes": [1, 2]},
                    {"values": ["退出条件：", "/"], "used_indexes": [3, 4]},
                ],
            },
            {
                "title": "ASC_R:",
                "rows": [
                    ["前置条件：", "a.ASC_R上高压；"],
                    ["退出条件：", "/"],
                    ["备注：", "1.ASC_F 需实时反馈电流；\n2.ASC_R 需实时反馈电流；"],
                ],
                "row_specs": [
                    {"values": ["前置条件：", "a.ASC_R上高压；"], "used_indexes": [5, 6]},
                    {"values": ["退出条件：", "/"], "used_indexes": [7, 8]},
                    {"values": ["备注：", "1.ASC_F 需实时反馈电流；\n2.ASC_R 需实时反馈电流；"], "used_indexes": [9, 10]},
                ],
            },
        ]

        updated_groups, trailing_notes = extraction_preview._extract_section_level_tail_notes_from_layout_groups(groups)

        self.assertEqual(len(updated_groups[1]["rows"]), 2)
        self.assertEqual(trailing_notes, ["备注：\n1.ASC_F 需实时反馈电流；\n2.ASC_R 需实时反馈电流；"])

    def test_build_layout_table_structured_preview_builds_headerless_refs(self) -> None:
        row = {
            "entry_id": "REQ-4.1.2.2",
            "req_code": "REQ-4.1.2.2",
            "section_code": "4.1.2.2",
            "section_key": "MAIN:4.1.2.2",
            "section_instance_key": "MAIN:4.1.2.2",
            "section_title": "功能策略",
            "section_label": "4.1.2.2",
            "source_page": 15,
            "source_page_end": 18,
        }
        recovered_groups = [
            {
                "title": "ASC_F:",
                "rows": [
                    ["触发条件：", "ASC_F 接收到控制方式 req"],
                    ["执行动作：", "a.若满足触发条件 a ， ASC_F 发送当前工作状态"],
                    ["备注：", "1. ASC_F 支持左右两轮单独控制；"],
                ],
                "row_specs": [
                    {"values": ["触发条件：", "ASC_F 接收到控制方式 req"], "used_indexes": [1, 2]},
                    {"values": ["执行动作：", "a.若满足触发条件 a ， ASC_F 发送当前工作状态"], "used_indexes": [3, 4]},
                    {"values": ["备注：", "1. ASC_F 支持左右两轮单独控制；"], "used_indexes": [5, 6]},
                ],
            },
            {
                "title": "ASC_R:",
                "rows": [
                    ["触发条件：", "ASC_R 接收到控制方式 req"],
                    ["执行动作：", "a.若满足触发条件 a ， ASC_R 发送当前工作状态"],
                    ["备注：", "1. ASC_R 支持左右两轮单独控制；"],
                ],
                "row_specs": [
                    {"values": ["触发条件：", "ASC_R 接收到控制方式 req"], "used_indexes": [7, 8]},
                    {"values": ["执行动作：", "a.若满足触发条件 a ， ASC_R 发送当前工作状态"], "used_indexes": [9, 10]},
                    {"values": ["备注：", "1. ASC_R 支持左右两轮单独控制；"], "used_indexes": [11, 12]},
                ],
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(extraction_preview, "_build_layout_table_line_specs", return_value=[]):
                with mock.patch.object(
                    extraction_preview,
                    "_recover_layout_headerless_tables_from_line_specs",
                    return_value=recovered_groups,
                ):
                    with mock.patch.object(
                        extraction_preview,
                        "_build_layout_group_source_snapshot",
                        side_effect=[("/tmp/4.1.2.2-ascf.png", 1), ("/tmp/4.1.2.2-ascr.png", 1)],
                    ):
                        rebuilt, assets = extraction_preview._build_layout_table_structured_preview(
                            row,
                            document_id="doc",
                            snapshot_root=Path(tmpdir),
                            source_file_path="/tmp/source.pdf",
                            source_snapshot_cache={},
                            pdf_document=object(),
                            source_key="source.pdf",
                            section_window_meta={"plan": [(14, (44.0, 600.0, 560.0, 802.0))]},
                            line_detection_cache={},
                            source_snapshot_path="/tmp/4.1.2.2-source.png",
                            source_image_block_count=2,
                        )

        self.assertIsNotNone(rebuilt)
        self.assertEqual([ref["table_title"] for ref in rebuilt["table_refs"]], ["ASC_F:", "ASC_R:"])
        self.assertEqual([ref["inherited_headers"] for ref in rebuilt["table_refs"]], [[], []])
        self.assertEqual(
            [ref["source_snapshot_path"] for ref in rebuilt["table_refs"]],
            ["/tmp/4.1.2.2-ascf.png", "/tmp/4.1.2.2-ascr.png"],
        )
        self.assertEqual(
            rebuilt["content_blocks"],
            [
                {"kind": "paragraph", "text": "4.1.2.2 功能策略"},
                {"kind": "paragraph", "text": "ASC_F:"},
                {"kind": "table", "table_id": rebuilt["table_refs"][0]["table_id"]},
                {"kind": "paragraph", "text": "ASC_R:"},
                {"kind": "table", "table_id": rebuilt["table_refs"][1]["table_id"]},
            ],
        )
        self.assertEqual(len(assets), 2)

    def test_build_layout_table_structured_preview_moves_section_tail_note_out_of_last_table(self) -> None:
        row = {
            "entry_id": "REQ-4.4.2",
            "req_code": "REQ-4.4.2",
            "section_code": "4.4.2",
            "section_key": "MAIN:4.4.2",
            "section_instance_key": "MAIN:4.4.2",
            "section_title": "功能描述",
            "section_label": "4.4.2",
            "source_page": 24,
            "source_page_end": 25,
        }
        recovered_groups = [
            {
                "title": "ASC_F:",
                "rows": [["前置条件：", "a.ASC_F上高压；"], ["退出条件：", "/"]],
                "row_specs": [
                    {"values": ["前置条件：", "a.ASC_F上高压；"], "used_indexes": [1, 2]},
                    {"values": ["退出条件：", "/"], "used_indexes": [3, 4]},
                ],
            },
            {
                "title": "ASC_R:",
                "rows": [
                    ["前置条件：", "a.ASC_R上高压；"],
                    ["退出条件：", "/"],
                    ["备注：", "1.ASC_F 需实时反馈电流；\n2.ASC_R 需实时反馈电流；"],
                ],
                "row_specs": [
                    {"values": ["前置条件：", "a.ASC_R上高压；"], "used_indexes": [5, 6]},
                    {"values": ["退出条件：", "/"], "used_indexes": [7, 8]},
                    {"values": ["备注：", "1.ASC_F 需实时反馈电流；\n2.ASC_R 需实时反馈电流；"], "used_indexes": [9, 10]},
                ],
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(extraction_preview, "_build_layout_table_line_specs", return_value=[]):
                with mock.patch.object(
                    extraction_preview,
                    "_recover_layout_headerless_tables_from_line_specs",
                    return_value=recovered_groups,
                ):
                    with mock.patch.object(
                        extraction_preview,
                        "_build_layout_group_source_snapshot",
                        side_effect=[("/tmp/4.4.2-ascf.png", 1), ("/tmp/4.4.2-ascr.png", 1)],
                    ):
                        rebuilt, _assets = extraction_preview._build_layout_table_structured_preview(
                            row,
                            document_id="doc",
                            snapshot_root=Path(tmpdir),
                            source_file_path="/tmp/source.pdf",
                            source_snapshot_cache={},
                            pdf_document=object(),
                            source_key="source.pdf",
                            section_window_meta={"plan": [(23, (44.0, 680.0, 560.0, 802.0))]},
                            line_detection_cache={},
                            source_snapshot_path="/tmp/4.4.2-source.png",
                            source_image_block_count=2,
                        )

        self.assertEqual(
            rebuilt["table_refs"][1]["slice_rows"],
            [["前置条件：", "a.ASC_R上高压；"], ["退出条件：", "/"]],
        )
        self.assertEqual(
            rebuilt["content_blocks"][-1],
            {"kind": "paragraph", "text": "备注：\n1.ASC_F 需实时反馈电流；\n2.ASC_R 需实时反馈电流；"},
        )

    def test_apply_preview_fallback_assets_prefers_layout_recovery_before_pipe_repair(self) -> None:
        entries = [
            {
                "entry_id": "REQ-4.4.2",
                "entry_kind": "REQUIREMENT",
                "section_code": "4.4.2",
                "section_key": "MAIN:4.4.2",
                "section_instance_key": "MAIN:4.4.2",
                "section_title": "功能描述",
                "section_label": "4.4.2",
                "raw_text": "4.4.2 功能描述\n|触发条件：|ASC_F|",
                "source_page": 24,
                "source_page_end": 25,
                "table_refs": [{"table_id": "old-ref"}],
                "content_blocks": [{"kind": "table", "table_id": "old-ref"}],
            }
        ]
        rebuilt_row = {
            **entries[0],
            "table_refs": [{"table_id": "rebuilt"}],
            "content_blocks": [{"kind": "table", "table_id": "rebuilt"}],
            "has_table_asset": True,
            "table_render_mode": "structured",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(
                extraction_preview,
                "_build_section_window_index",
                return_value={"MAIN:4.4.2": {"section_code": "4.4.2"}},
            ):
                with mock.patch.object(
                    extraction_preview,
                    "_get_section_window_snapshot_meta",
                    return_value={"plan": [(23, (44.0, 680.0, 560.0, 802.0))]},
                ):
                    with mock.patch.object(
                        extraction_preview,
                        "_build_structured_table_source_snapshot",
                        return_value=("/tmp/4.4.2-source.png", 1),
                    ):
                        with mock.patch.object(
                            extraction_preview,
                            "_build_layout_table_structured_preview",
                            return_value=(rebuilt_row, [{"table_id": "rebuilt"}]),
                        ) as mock_layout:
                            with mock.patch.object(
                                extraction_preview,
                                "_repair_local_clause_pipe_table_preview",
                            ) as mock_pipe:
                                updated, assets = extraction_preview._apply_preview_fallback_assets(
                                    entries,
                                    document_id="doc",
                                    snapshot_root=Path(tmpdir),
                                    source_file_path="",
                                    parser_diagnostics={},
                                    source_snapshot_cache={},
                                    sections=[],
                                )

        self.assertEqual(updated[0]["table_refs"][0]["table_id"], "rebuilt")
        self.assertEqual(len(assets), 1)
        mock_layout.assert_called_once()
        mock_pipe.assert_not_called()

    def test_apply_preview_fallback_assets_skips_heading_visual_when_requirement_has_asset(self) -> None:
        entries = [
            {
                "entry_id": "H-main_2_1",
                "entry_kind": "HEADING",
                "section_code": "2.1",
                "section_key": "MAIN:2.1",
                "section_instance_key": "MAIN:2.1",
                "section_title": "承接功能清单",
                "display_text": "承接功能清单",
                "source_page": 7,
                "source_page_end": 7,
                "table_refs": [],
                "content_blocks": [{"kind": "paragraph", "text": "承接功能清单"}],
            },
            {
                "entry_id": "REQ-2.1",
                "entry_kind": "REQUIREMENT",
                "section_code": "2.1",
                "section_key": "MAIN:2.1",
                "section_instance_key": "MAIN:2.1",
                "section_title": "承接功能清单",
                "display_text": "2.1 承接功能清单",
                "raw_text": (
                    "2.1 承接功能清单\n"
                    "|序号|系统编号|系统名称|\n"
                    "|1|ASC_F/R|-|\n"
                    "|2|ASC_F/R|D5-F9-3-UC01|\n"
                    "|3|ASC_F/R|-|\n"
                    "|4|ASC_F/R|D4-F12-8-UC01|"
                ),
                "source_page": 7,
                "source_page_end": 7,
                "table_refs": [
                    {
                        "table_id": "structured-old",
                        "slice_render_mode": "structured",
                        "row_ids": ["r1", "r2", "r3"],
                    }
                ],
                "content_blocks": [
                    {"kind": "paragraph", "text": "2.1 承接功能清单"},
                    {"kind": "table", "table_id": "structured-old"},
                ],
            },
        ]

        def fake_build_ref(row, *, suffix, **_: object):
            table_id = f"{row['entry_id']}::{suffix}"
            return (
                {
                    "table_id": table_id,
                    "slice_render_mode": "snapshot_only",
                    "slice_snapshot_path": f"/tmp/{table_id}.png",
                },
                {
                    "table_id": table_id,
                    "snapshot_path": f"/tmp/{table_id}.png",
                },
            )

        with mock.patch.object(extraction_preview, "_build_full_page_snapshot_ref", side_effect=fake_build_ref):
            updated, extra_assets = extraction_preview._apply_preview_fallback_assets(
                entries,
                document_id="doc",
                snapshot_root=Path("/tmp"),
                source_file_path="/tmp/source.pdf",
                parser_diagnostics={"page_details": []},
                source_snapshot_cache={},
            )

        self.assertEqual(updated[0]["table_refs"], [])
        self.assertEqual(
            updated[1]["table_refs"],
            [
                {
                    "table_id": "REQ-2.1::table_fallback",
                    "slice_render_mode": "snapshot_only",
                    "slice_snapshot_path": "/tmp/REQ-2.1::table_fallback.png",
                }
            ],
        )
        self.assertEqual(len(extra_assets), 1)

    def test_apply_preview_fallback_assets_replaces_multi_structured_visual_refs(self) -> None:
        entries = [
            {
                "entry_id": "REQ-4.2.2.4",
                "entry_kind": "REQUIREMENT",
                "section_code": "4.2.2.4",
                "section_key": "MAIN:4.2.2.4",
                "section_instance_key": "MAIN:4.2.2.4",
                "section_title": "高压互锁检测流程",
                "display_text": "4.2.2.4 高压互锁检测流程",
                "raw_text": "检测方式1 示意图（参考）\n检测方式2 示意图（参考）",
                "source_page": 19,
                "source_page_end": 21,
                "table_refs": [
                    {
                        "table_id": "structured-old-1",
                        "slice_render_mode": "structured",
                    },
                    {
                        "table_id": "structured-old-2",
                        "slice_render_mode": "structured",
                    },
                ],
                "content_blocks": [
                    {"kind": "paragraph", "text": "4.2.2.4 高压互锁检测流程"},
                    {"kind": "table", "table_id": "structured-old-1"},
                    {"kind": "paragraph", "text": "电气连接是否完整"},
                    {"kind": "table", "table_id": "structured-old-2"},
                ],
            }
        ]

        def fake_build_ref(row, *, suffix, **_: object):
            table_id = f"{row['entry_id']}::{suffix}"
            return (
                {
                    "table_id": table_id,
                    "slice_render_mode": "snapshot_only",
                    "slice_snapshot_path": f"/tmp/{table_id}.png",
                },
                {
                    "table_id": table_id,
                    "snapshot_path": f"/tmp/{table_id}.png",
                },
            )

        with mock.patch.object(extraction_preview, "_build_full_page_snapshot_ref", side_effect=fake_build_ref):
            updated, extra_assets = extraction_preview._apply_preview_fallback_assets(
                entries,
                document_id="doc",
                snapshot_root=Path("/tmp"),
                source_file_path="/tmp/source.pdf",
                parser_diagnostics={"page_details": []},
                source_snapshot_cache={},
            )

        self.assertEqual(
            updated[0]["table_refs"],
            [
                {
                    "table_id": "REQ-4.2.2.4::visual_override",
                    "slice_render_mode": "snapshot_only",
                    "slice_snapshot_path": "/tmp/REQ-4.2.2.4::visual_override.png",
                }
            ],
        )
        self.assertEqual(
            updated[0]["content_blocks"],
            [
                {"kind": "paragraph", "text": "4.2.2.4 高压互锁检测流程"},
                {"kind": "paragraph", "text": "电气连接是否完整"},
                {"kind": "table", "table_id": "REQ-4.2.2.4::visual_override"},
            ],
        )
        self.assertEqual(len(extra_assets), 1)

    def test_simple_scoped_local_clause_does_not_surface_as_section(self) -> None:
        self.assertFalse(
            extraction_preview._should_surface_scoped_local_clause_as_section(
                {
                    "appendix_scope": "",
                    "section_code": "2",
                    "is_scoped_local_clause": True,
                }
            )
        )
        self.assertTrue(
            extraction_preview._should_surface_scoped_local_clause_as_section(
                {
                    "appendix_scope": "",
                    "section_code": "8.1.2",
                    "is_scoped_local_clause": True,
                }
            )
        )

    def test_complete_requirement_entries_skip_heading_for_simple_scoped_local_clause(self) -> None:
        sections = [
            {
                "appendix_scope": "",
                "section_code": "8.10.4",
                "section_key": "8.10.4",
                "section_instance_key": "8.10.4",
                "section_title": "执行规定",
                "section_level": 4,
                "parent_section_code": "8.10",
                "parent_section_key": "8.10",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 85,
                "source_page_end": 86,
                "source_line_index_start": 0,
                "source_line_index_end": 0,
                "line_count": 1,
                "section_char_count": 4,
                "requirement_like": False,
                "is_appendix_marker": False,
                "requirement_count": 0,
                "total_chars": 0,
                "text_preview": "执行规定",
                "first_req_order": 0,
                "outline_order": 0,
                "is_scoped_local_clause": False,
            }
        ]
        requirements = [
            {
                "req_code": "REQ-0406",
                "entry_id": "REQ-0406",
                "appendix_scope": "",
                "section_code": "2",
                "section_key": "8.10.4::2",
                "section_instance_key": "8.10.4::2",
                "section_title": "当诊断工具发送清除诊断信息（14h）服务时，DTC 数目将被清除。",
                "section_label": "2）",
                "owner_section_code": "2",
                "owner_section_title": "当诊断工具发送清除诊断信息（14h）服务时，DTC 数目将被清除。",
                "owner_section_label": "2）",
                "section_level": 1,
                "source_page": 85,
                "source_page_end": 85,
                "source_anchor": "Page 85 | Section 8.10.4 执行规定 | Clause 2 当诊断工具发送清除诊断信息（14h）服务时，DTC 数目将被清除。",
                "requirement_type": "",
                "priority": "",
                "text_length": 52,
                "requirement_text": "2）当诊断工具发送清除诊断信息（14h）服务时，DTC 数目将被清除。",
                "req_order": 1,
                "source_line_index": 1,
                "source_line_index_end": 1,
                "structural_parent_section_code": "8.10.4",
                "structural_parent_section_key": "8.10.4",
                "structural_parent_section_title": "执行规定",
                "structural_parent_section_label": "8.10.4 执行规定",
                "is_scoped_local_clause": True,
            }
        ]

        extraction_preview._attach_requirement_section_context(requirements, sections)
        entries = extraction_preview._build_complete_requirement_entries(requirements, sections)

        heading_codes = [row.get("section_code") for row in entries if row.get("entry_kind") == "HEADING"]
        self.assertEqual(heading_codes, ["8.10.4"])
        req_rows = [row for row in entries if row.get("entry_kind") == "REQUIREMENT"]
        self.assertEqual(len(req_rows), 1)
        self.assertEqual(req_rows[0]["owner_section_code"], "8.10.4")
        self.assertEqual(req_rows[0]["owner_section_label"], "8.10.4 执行规定")
        self.assertEqual(req_rows[0]["parent_section_code"], "8.10.4")
        self.assertIn("2）当诊断工具发送清除诊断信息", req_rows[0]["display_text"])

    def test_complete_requirement_entries_keep_heading_for_multilevel_scoped_local_clause(self) -> None:
        sections = [
            {
                "appendix_scope": "",
                "section_code": "3.2.2",
                "section_key": "3.2.2",
                "section_instance_key": "3.2.2",
                "section_title": "产品详细描述",
                "section_level": 3,
                "parent_section_code": "3.2",
                "parent_section_key": "3.2",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 7,
                "source_page_end": 7,
                "source_line_index_start": 0,
                "source_line_index_end": 0,
                "line_count": 1,
                "section_char_count": 6,
                "requirement_like": False,
                "is_appendix_marker": False,
                "requirement_count": 0,
                "total_chars": 0,
                "text_preview": "产品详细描述",
                "first_req_order": 0,
                "outline_order": 0,
                "is_scoped_local_clause": False,
            }
        ]
        requirements = [
            {
                "req_code": "REQ-0045",
                "entry_id": "REQ-0045",
                "appendix_scope": "",
                "section_code": "8.1.2",
                "section_key": "3.2.2::8.1.2",
                "section_instance_key": "3.2.2::8.1.2",
                "section_title": "产品各方向解耦率及各向频率应满足下表的要求",
                "section_label": "8.1.2）",
                "owner_section_code": "8.1.2",
                "owner_section_title": "产品各方向解耦率及各向频率应满足下表的要求",
                "owner_section_label": "8.1.2）",
                "section_level": 3,
                "source_page": 7,
                "source_page_end": 7,
                "source_anchor": "Page 7 | Section 3.2.2 产品详细描述 | Clause 8.1.2 产品各方向解耦率及各向频率应满足下表的要求",
                "requirement_type": "",
                "priority": "",
                "text_length": 56,
                "requirement_text": "8.1.2）产品各方向解耦率及各向频率应满足下表的要求",
                "req_order": 44,
                "source_line_index": 1,
                "source_line_index_end": 1,
                "structural_parent_section_code": "8.1",
                "structural_parent_section_key": "3.2.2::8.1",
                "structural_parent_section_title": "",
                "structural_parent_section_label": "",
                "is_scoped_local_clause": True,
            }
        ]

        extraction_preview._attach_requirement_section_context(requirements, sections)
        entries = extraction_preview._build_complete_requirement_entries(requirements, sections)

        heading_codes = [row.get("section_code") for row in entries if row.get("entry_kind") == "HEADING"]
        self.assertEqual(heading_codes, ["3.2.2", "8.1.2"])
        req_rows = [row for row in entries if row.get("entry_kind") == "REQUIREMENT"]
        self.assertEqual(len(req_rows), 1)
        self.assertEqual(req_rows[0]["owner_section_label"], "8.1.2）")

    def test_complete_requirement_entries_keeps_hidden_scoped_local_clauses_inline_with_parent(self) -> None:
        sections = [
            {
                "appendix_scope": "",
                "section_code": "6.2.2",
                "section_key": "MAIN:6.2.2",
                "section_instance_key": "MAIN:6.2.2",
                "section_title": "故障自诊断范围",
                "section_level": 3,
                "parent_section_code": "6.2",
                "parent_section_key": "MAIN:6.2",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 18,
                "source_page_end": 18,
                "source_line_index_start": 0,
                "source_line_index_end": 0,
                "line_count": 1,
                "section_char_count": 6,
                "requirement_like": False,
                "is_appendix_marker": False,
                "requirement_count": 0,
                "total_chars": 0,
                "text_preview": "故障自诊断范围",
                "first_req_order": 29,
                "outline_order": 0,
                "is_scoped_local_clause": False,
            },
            {
                "appendix_scope": "",
                "section_code": "6.3",
                "section_key": "MAIN:6.3",
                "section_instance_key": "MAIN:6.3",
                "section_title": "故障信息",
                "section_level": 2,
                "parent_section_code": "6",
                "parent_section_key": "MAIN:6",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 19,
                "source_page_end": 19,
                "source_line_index_start": 0,
                "source_line_index_end": 0,
                "line_count": 1,
                "section_char_count": 4,
                "requirement_like": False,
                "is_appendix_marker": False,
                "requirement_count": 0,
                "total_chars": 0,
                "text_preview": "故障信息",
                "first_req_order": 40,
                "outline_order": 1,
                "is_scoped_local_clause": False,
            },
        ]
        requirements = [
            {
                "req_code": "REQ-0029",
                "entry_id": "REQ-0029",
                "appendix_scope": "",
                "section_code": "6.2.2",
                "section_key": "MAIN:6.2.2",
                "section_instance_key": "MAIN:6.2.2",
                "section_title": "故障自诊断范围",
                "section_label": "6.2.2",
                "owner_section_code": "6.2.2",
                "owner_section_title": "故障自诊断范围",
                "owner_section_label": "6.2.2",
                "section_level": 3,
                "source_page": 18,
                "source_page_end": 18,
                "source_anchor": "Page 18 | Section 6.2.2 故障自诊断范围",
                "requirement_text": (
                    "6.2.2 故障自诊断范围\n"
                    "a) 法规确定的条件；\n"
                    "b) 电控单元内部错误；\n"
                    "c) 网络监控错误；\n"
                    "d) 输入 / 输出线路的故障情况，其应包含但不限于如下所列："
                ),
                "req_order": 29,
                "source_line_index": 1,
                "source_line_index_end": 1,
                "structural_parent_section_code": "6.2",
                "structural_parent_section_key": "MAIN:6.2",
                "structural_parent_section_title": "",
                "structural_parent_section_label": "",
                "is_scoped_local_clause": False,
            },
            {
                "req_code": "REQ-0030",
                "entry_id": "REQ-0030",
                "appendix_scope": "",
                "section_code": "1",
                "section_key": "MAIN:6.2.2::1",
                "section_instance_key": "MAIN:6.2.2::1",
                "section_title": "故障自诊断范围",
                "section_label": "1）",
                "owner_section_code": "1",
                "owner_section_title": "故障自诊断范围",
                "owner_section_label": "1）",
                "section_level": 1,
                "source_page": 18,
                "source_page_end": 18,
                "source_anchor": "Page 18 | Section 6.2.2 故障自诊断范围 | Clause 1 开关输入的粘滞和其他故障情况（例如：不合理开关情况）；",
                "requirement_text": "1）开关输入的粘滞和其他故障情况（例如：不合理开关情况）；",
                "req_order": 30,
                "source_line_index": 2,
                "source_line_index_end": 2,
                "structural_parent_section_code": "6.2.2",
                "structural_parent_section_key": "MAIN:6.2.2",
                "structural_parent_section_title": "",
                "structural_parent_section_label": "",
                "is_scoped_local_clause": True,
            },
            {
                "req_code": "REQ-0031",
                "entry_id": "REQ-0031",
                "appendix_scope": "",
                "section_code": "2",
                "section_key": "MAIN:6.2.2::2",
                "section_instance_key": "MAIN:6.2.2::2",
                "section_title": "故障自诊断范围",
                "section_label": "2）",
                "owner_section_code": "2",
                "owner_section_title": "故障自诊断范围",
                "owner_section_label": "2）",
                "section_level": 1,
                "source_page": 18,
                "source_page_end": 18,
                "source_anchor": "Page 18 | Section 6.2.2 故障自诊断范围 | Clause 2 模拟输入的开路、短路和其他故障情况；",
                "requirement_text": "2）模拟输入的开路、短路和其他故障情况；",
                "req_order": 31,
                "source_line_index": 3,
                "source_line_index_end": 3,
                "structural_parent_section_code": "6.2.2",
                "structural_parent_section_key": "MAIN:6.2.2",
                "structural_parent_section_title": "",
                "structural_parent_section_label": "",
                "is_scoped_local_clause": True,
            },
            {
                "req_code": "REQ-0040",
                "entry_id": "REQ-0040",
                "appendix_scope": "",
                "section_code": "6.3",
                "section_key": "MAIN:6.3",
                "section_instance_key": "MAIN:6.3",
                "section_title": "故障信息",
                "section_label": "6.3",
                "owner_section_code": "6.3",
                "owner_section_title": "故障信息",
                "owner_section_label": "6.3",
                "section_level": 2,
                "source_page": 19,
                "source_page_end": 19,
                "source_anchor": "Page 19 | Section 6.3 故障信息",
                "requirement_text": "6.3 故障信息",
                "req_order": 40,
                "source_line_index": 1,
                "source_line_index_end": 1,
                "structural_parent_section_code": "6",
                "structural_parent_section_key": "MAIN:6",
                "structural_parent_section_title": "",
                "structural_parent_section_label": "",
                "is_scoped_local_clause": False,
            },
        ]

        extraction_preview._attach_requirement_section_context(requirements, sections)
        entries = extraction_preview._build_complete_requirement_entries(requirements, sections)

        heading_codes = [row.get("section_code") for row in entries if row.get("entry_kind") == "HEADING"]
        self.assertEqual(heading_codes, ["6.2.2", "6.3"])
        req_rows = [row for row in entries if row.get("entry_kind") == "REQUIREMENT"]
        self.assertEqual(
            [(row.get("section_code"), row.get("req_order")) for row in req_rows],
            [("6.2.2", 29), ("1", 30), ("2", 31), ("6.3", 40)],
        )
        self.assertEqual([row.get("owner_section_code") for row in req_rows[1:3]], ["6.2.2", "6.2.2"])

    def test_dedupe_table_assets_keeps_last_override(self) -> None:
        deduped = extraction_preview._dedupe_table_assets(
            [
                {"table_id": "tbl-1", "snapshot_path": "/tmp/old.png"},
                {"table_id": "tbl-1", "snapshot_path": "/tmp/new.png"},
                {"table_id": "tbl-2", "snapshot_path": "/tmp/other.png"},
            ]
        )

        self.assertEqual(
            deduped,
            [
                {"table_id": "tbl-1", "snapshot_path": "/tmp/new.png"},
                {"table_id": "tbl-2", "snapshot_path": "/tmp/other.png"},
            ],
        )

    def test_repair_row_explicit_visual_assets_rebuilds_mixed_figure_and_table_sequence(self) -> None:
        row = {
            "entry_id": "REQ-0076",
            "req_code": "REQ-0076",
            "entry_kind": "REQUIREMENT",
            "section_code": "7.5.6.2",
            "section_key": "MAIN:7.5.6.2",
            "section_instance_key": "MAIN:7.5.6.2",
            "section_title": "应用层",
            "section_label": "7.5.6.2",
            "source_page": 29,
            "source_page_end": 31,
            "raw_text": (
                "7.5.6.2 应用层\n"
                "图4 应用层诊断时间1\n"
                "图5 应用层诊断时间2\n"
                "表15 应用层诊断时间参数定义\n"
                "注: 若ECU需多次回复NRC78h。"
            ),
            "table_refs": [
                {
                    "table_id": "tbl15",
                    "table_title": "表15 应用层诊断时间参数定义",
                    "slice_render_mode": "snapshot_only",
                    "slice_snapshot_path": "/tmp/tbl15-slice.png",
                    "source_snapshot_path": "/tmp/tbl15-old.png",
                }
            ],
        }
        line_specs = [
            {"page_index": 29, "bbox": (40.0, 100.0, 220.0, 114.0), "text": "图4 应用层诊断时间1"},
            {"page_index": 30, "bbox": (40.0, 120.0, 220.0, 134.0), "text": "图5 应用层诊断时间2"},
            {"page_index": 31, "bbox": (40.0, 150.0, 260.0, 164.0), "text": "表15 应用层诊断时间参数定义"},
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            def fake_snapshot(*, cache_key_hint: str, **_: object):
                stem = cache_key_hint.split(":")[-1]
                path = Path(tmpdir) / f"{stem}.png"
                return path, {"resolved_page_start": 29, "resolved_page_end": 31, "image_block_count": 1}

            with mock.patch.object(
                extraction_preview,
                "_build_section_visual_line_specs",
                return_value=line_specs,
            ):
                with mock.patch.object(
                    extraction_preview,
                    "_build_explicit_visual_event_crop_plan",
                    return_value=[(29, (40.0, 100.0, 400.0, 300.0))],
                ):
                    with mock.patch.object(
                        extraction_preview,
                        "_write_source_snapshot_from_crop_plan",
                        side_effect=fake_snapshot,
                    ):
                        updated, assets = extraction_preview._repair_row_explicit_visual_assets(
                            row,
                            document_id="doc",
                            snapshot_root=Path(tmpdir),
                            source_file_path="/tmp/doc.pdf",
                            source_snapshot_cache={},
                            pdf_document=object(),
                            source_key="doc",
                            section_window_meta={"plan": [(28, (30.0, 90.0, 560.0, 780.0))]},
                            line_detection_cache={},
                        )

        assert updated is not None
        self.assertEqual(
            [ref.get("table_title") for ref in updated["table_refs"]],
            ["图4 应用层诊断时间1", "图5 应用层诊断时间2", "表15 应用层诊断时间参数定义"],
        )
        self.assertEqual(updated["table_refs"][-1]["table_id"], "tbl15")
        self.assertEqual(
            updated["content_blocks"],
            [
                {"kind": "paragraph", "text": "7.5.6.2 应用层"},
                {"kind": "paragraph", "text": "图4 应用层诊断时间1"},
                {"kind": "table", "table_id": updated["table_refs"][0]["table_id"]},
                {"kind": "paragraph", "text": "图5 应用层诊断时间2"},
                {"kind": "table", "table_id": updated["table_refs"][1]["table_id"]},
                {"kind": "paragraph", "text": "表15 应用层诊断时间参数定义"},
                {"kind": "table", "table_id": "tbl15"},
                {"kind": "paragraph", "text": "注: 若ECU需多次回复NRC78h。"},
            ],
        )
        self.assertEqual(len(assets), 3)

    def test_extract_explicit_visual_titles_skips_cross_reference_sentence(self) -> None:
        titles = extraction_preview._extract_explicit_visual_titles_from_text(
            "7.5.6.2 应用层\n"
            "图4、图5 描述了应用层诊断的时间，详细描述请参考ISO 14229-3：2022。\n"
            "图4 应用层诊断时间1\n"
            "图5 应用层诊断时间2\n"
            "表15 应用层诊断时间参数定义\n"
        )

        self.assertEqual(
            [item["title"] for item in titles],
            ["图4 应用层诊断时间1", "图5 应用层诊断时间2", "表15 应用层诊断时间参数定义"],
        )

    def test_find_explicit_visual_cutoff_y_stops_before_table_prose(self) -> None:
        page_specs = [
            {"bbox": (80.0, 130.0, 130.0, 140.0), "text": "故障内码"},
            {"bbox": (170.0, 138.0, 260.0, 148.0), "text": "标准故障码字符1"},
            {"bbox": (205.0, 195.0, 214.0, 204.0), "text": "P"},
            {"bbox": (350.0, 195.0, 458.0, 204.0), "text": "Powertrain 动力系统故障"},
            {
                "bbox": (92.0, 262.0, 539.0, 272.0),
                "text": "第二位数字是0、1、2 或3 表示故障类型，意义如下：",
            },
        ]

        self.assertEqual(
            extraction_preview._find_explicit_visual_cutoff_y(
                page_specs,
                kind="table",
                page_width=600.0,
                start_line_index=0,
            ),
            254.0,
        )

    def test_find_explicit_visual_cutoff_y_stops_before_formal_heading(self) -> None:
        page_specs = [
            {"bbox": (60.0, 180.0, 220.0, 190.0), "text": "注4：多帧传输过程中..."},
            {"bbox": (60.0, 220.0, 180.0, 232.0), "text": "7.5.6 时间参数定义"},
        ]

        self.assertEqual(
            extraction_preview._find_explicit_visual_cutoff_y(
                page_specs,
                kind="table",
                page_width=600.0,
                start_line_index=0,
            ),
            212.0,
        )

    def test_find_explicit_visual_cutoff_y_keeps_split_table_note_lines(self) -> None:
        page_specs = [
            {"bbox": (80.0, 120.0, 140.0, 130.0), "text": "报文类型"},
            {"bbox": (220.0, 120.0, 260.0, 130.0), "text": "CAN ID"},
            {"bbox": (80.0, 146.0, 140.0, 156.0), "text": "连续帧"},
            {"bbox": (220.0, 146.0, 260.0, 156.0), "text": "0010b"},
            {"bbox": (80.0, 172.0, 120.0, 182.0), "text": "注"},
            {"bbox": (80.0, 186.0, 520.0, 198.0), "text": "4：多帧传输过程中，实际FF_DL＞4095 时，采用新的 PCI 格式发送第一帧。"},
            {"bbox": (80.0, 202.0, 260.0, 212.0), "text": "字节2-5 表示FF_DL。"},
        ]

        self.assertIsNone(
            extraction_preview._find_explicit_visual_cutoff_y(
                page_specs,
                kind="table",
                page_width=600.0,
                start_line_index=0,
            )
        )

    def test_build_explicit_visual_event_crop_plan_prefers_figure_area_above_title(self) -> None:
        line_specs = [
            {"page_index": 0, "bbox": (80.0, 300.0, 520.0, 312.0), "text": "网络层参数的定义如图3 示。"},
            {"page_index": 0, "bbox": (250.0, 500.0, 362.0, 512.0), "text": "图3 多帧传输参数定义"},
        ]

        fake_rect = mock.Mock(width=600.0, height=800.0)
        fake_page = mock.Mock(rect=fake_rect)
        fake_pdf = mock.Mock()
        fake_pdf.load_page.return_value = fake_page

        def fake_summary(_document, plan):
            _page_index, bbox = plan[0]
            return {"significant_visual_count": 3 if bbox[1] < 400.0 else 0}

        with mock.patch.object(
            extraction_preview,
            "_summarize_section_window_visual_features",
            side_effect=fake_summary,
        ):
            plan = extraction_preview._build_explicit_visual_event_crop_plan(
                fake_pdf,
                line_specs=line_specs,
                start_index=1,
                next_index=None,
                kind="figure",
            )

        self.assertEqual(plan, [(0, (24.0, 322.0, 576.0, 522.0))])

    def test_build_explicit_visual_event_crop_plan_prefers_taller_figure_area_when_visual_counts_tie(self) -> None:
        line_specs = [
            {"page_index": 0, "bbox": (80.0, 300.0, 520.0, 312.0), "text": "网络层参数的定义如图3 示。"},
            {"page_index": 0, "bbox": (250.0, 720.0, 362.0, 732.0), "text": "图3 多帧传输参数定义"},
        ]

        fake_rect = mock.Mock(width=600.0, height=800.0)
        fake_page = mock.Mock(rect=fake_rect)
        fake_pdf = mock.Mock()
        fake_pdf.load_page.return_value = fake_page

        with mock.patch.object(
            extraction_preview,
            "_summarize_section_window_visual_features",
            return_value={"significant_visual_count": 0},
        ):
            plan = extraction_preview._build_explicit_visual_event_crop_plan(
                fake_pdf,
                line_specs=line_specs,
                start_index=1,
                next_index=None,
                kind="figure",
            )

        self.assertEqual(plan, [(0, (24.0, 322.0, 576.0, 742.0))])

    def test_build_explicit_visual_event_crop_plan_clamps_last_table_to_observed_lines(self) -> None:
        line_specs = [
            {"page_index": 0, "bbox": (200.0, 120.0, 360.0, 132.0), "text": "表14 网络层流控制参数要求"},
            {"page_index": 0, "bbox": (120.0, 340.0, 438.0, 352.0), "text": "注3：BS 为0 表示发送所有后续数据帧。"},
        ]

        fake_rect = mock.Mock(width=600.0, height=800.0)
        fake_page = mock.Mock(rect=fake_rect)
        fake_pdf = mock.Mock()
        fake_pdf.load_page.return_value = fake_page

        with mock.patch.object(
            extraction_preview,
            "_collect_anchor_window_content_bboxes",
            return_value=[(90.0, 120.0, 520.0, 760.0)],
        ):
            with mock.patch.object(
                extraction_preview,
                "_expand_section_window_crop_bbox",
                return_value=(90.0, 120.0, 520.0, 760.0),
            ):
                plan = extraction_preview._build_explicit_visual_event_crop_plan(
                    fake_pdf,
                    line_specs=line_specs,
                    start_index=0,
                    next_index=None,
                    kind="table",
                )

        self.assertEqual(plan, [(0, (90.0, 120.0, 520.0, 364.0))])

    def test_repair_preview_clause_rows_from_pdf_repairs_wrapped_items_and_appends_alpha_tail(self) -> None:
        sections = [
            {
                "section_code": "6.2.2",
                "section_key": "MAIN:6.2.2",
                "section_instance_key": "MAIN:6.2.2",
                "section_title": "故障自诊断范围",
                "source_page_start": 18,
                "source_page_end": 18,
            }
        ]
        base_rows = []
        for idx, text in enumerate(
            [
                "1) 开关输入的粘滞和其他故障情况（例如：不合理开关情况）；",
                "2) 模拟输入的开路、短路和其他故障情况（例如：信号变动率过慢或过快，不合理信号情况等）；",
                "3) 调制输入（频率调制、脉冲编码调制、脉宽调制等）的开路、短路和其他故障情况（例如：\n信号变动率过慢或过快，不合理信号情况等）；",
                "4) 低端驱动输出的短路、开路和其他故障情况（例如：位置反馈信号不匹配、低端驱动温度过",
                "5) 高端驱动输出的短路、开路和其他故障情况（例如：位置反馈信号不匹配、高端驱动温度过",
                "6) 电控单元电源电压高或低。\ne) 超出线路正常运行范围的错误信号；\nf) 影响或限制系统性能的情况；\ng) 可引起用户对性能问题感知的异常情况；\nh) 迫使系统以失效安全模式运行的情况。",
            ],
            start=1,
        ):
            base_rows.append(
                {
                    "req_code": f"REQ-00{30 + idx}",
                    "entry_id": f"REQ-00{30 + idx}",
                    "entry_kind": "REQUIREMENT",
                    "section_code": str(idx),
                    "section_key": f"MAIN:6.2.2::{idx}",
                    "section_instance_key": f"MAIN:6.2.2::{idx}",
                    "section_title": "故障自诊断范围",
                    "section_label": f"{idx}）",
                    "owner_section_code": "6.2.2",
                    "owner_section_title": "故障自诊断范围",
                    "owner_section_label": "6.2.2",
                    "structural_parent_section_code": "6.2.2",
                    "structural_parent_section_key": "MAIN:6.2.2",
                    "source_page": 18,
                    "source_page_end": 18,
                    "source_anchor": "Page 18 | Section 6.2.2 故障自诊断范围",
                    "requirement_text": text,
                    "display_text": text,
                    "raw_text": text,
                    "req_order": 30 + idx,
                    "is_scoped_local_clause": True,
                }
            )

        recovered_specs = [
            {"page_index": 17, "bbox": (40.0, 523.1, 300.0, 537.0), "text": "1) 开关输入的粘滞和其他故障情况（例如：不合理开关情况）；"},
            {"page_index": 17, "bbox": (40.0, 538.7, 300.0, 552.0), "text": "2) 模拟输入的开路、短路和其他故障情况（例如：信号变动率过慢或过快，不合理信号情况等）；"},
            {"page_index": 17, "bbox": (40.0, 554.3, 300.0, 568.0), "text": "3) 调制输入（频率调制、脉冲编码调制、脉宽调制等）的开路、短路和其他故障情况（例如："},
            {"page_index": 17, "bbox": (40.0, 569.9, 300.0, 583.0), "text": "信号变动率过慢或过快，不合理信号情况等）；"},
            {"page_index": 17, "bbox": (40.0, 585.5, 300.0, 599.0), "text": "4) 低端驱动输出的短路、开路和其他故障情况（例如：位置反馈信号不匹配、低端驱动温度过"},
            {"page_index": 17, "bbox": (40.0, 601.1, 140.0, 615.0), "text": "高等）；"},
            {"page_index": 17, "bbox": (40.0, 616.7, 300.0, 630.0), "text": "5) 高端驱动输出的短路、开路和其他故障情况（例如：位置反馈信号不匹配、高端驱动温度过"},
            {"page_index": 17, "bbox": (40.0, 632.3, 140.0, 646.0), "text": "高等）；"},
            {"page_index": 17, "bbox": (40.0, 647.9, 220.0, 661.0), "text": "6) 电控单元电源电压高或低。"},
            {"page_index": 17, "bbox": (40.0, 663.5, 220.0, 677.0), "text": "e) 超出线路正常运行范围的错误信号；"},
            {"page_index": 17, "bbox": (40.0, 679.1, 220.0, 693.0), "text": "f) 影响或限制系统性能的情况；"},
            {"page_index": 17, "bbox": (40.0, 694.7, 220.0, 708.0), "text": "g) 可引起用户对性能问题感知的异常情况；"},
            {"page_index": 17, "bbox": (40.0, 710.3, 220.0, 724.0), "text": "h) 迫使系统以失效安全模式运行的情况。"},
        ]

        fake_doc_cm = mock.MagicMock()
        fake_doc_cm.__enter__.return_value = object()
        fake_doc_cm.__exit__.return_value = False

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "doc.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n")
            with mock.patch.object(extraction_preview.fitz, "open", return_value=fake_doc_cm):
                with mock.patch.object(
                    extraction_preview,
                    "_get_section_window_snapshot_meta",
                    return_value={"plan": [(17, (40.0, 500.0, 560.0, 740.0))]},
                ):
                    with mock.patch.object(
                        extraction_preview,
                        "_build_section_visual_line_specs",
                        return_value=recovered_specs,
                    ):
                        repaired = extraction_preview._repair_preview_clause_rows_from_pdf(
                            base_rows,
                            sections=sections,
                            source_file_path=str(pdf_path),
                        )

        self.assertEqual(repaired[3]["requirement_text"], "4) 低端驱动输出的短路、开路和其他故障情况（例如：位置反馈信号不匹配、低端驱动温度过\n高等）；")
        self.assertEqual(repaired[4]["requirement_text"], "5) 高端驱动输出的短路、开路和其他故障情况（例如：位置反馈信号不匹配、高端驱动温度过\n高等）；")
        self.assertEqual(repaired[5]["requirement_text"], "6) 电控单元电源电压高或低。")
        self.assertEqual([row["section_label"] for row in repaired[-4:]], ["e）", "f）", "g）", "h）"])
        self.assertTrue(all(row["owner_section_code"] == "6.2.2" for row in repaired[-4:]))

    def test_repair_preview_clause_rows_from_pdf_preserves_repeated_numeric_groups_and_alpha_positions(self) -> None:
        sections = [
            {
                "section_code": "8.13.4",
                "section_key": "MAIN:8.13.4",
                "section_instance_key": "MAIN:8.13.4",
                "section_title": "故障处理要求",
                "source_page_start": 42,
                "source_page_end": 42,
            }
        ]
        base_rows = []
        for req_order, (label, text) in enumerate(
            [
                ("1", "1) 第一组旧文本"),
                ("2", "2) 第一组旧文本-第二项"),
                ("1", "1) 第二组旧文本"),
                ("2", "2) 第二组旧文本-第二项"),
                ("3", "3) 结束条件旧文本"),
            ],
            start=101,
        ):
            base_rows.append(
                {
                    "req_code": f"REQ-{req_order}",
                    "entry_id": f"REQ-{req_order}",
                    "entry_kind": "REQUIREMENT",
                    "section_code": label,
                    "section_key": f"MAIN:8.13.4::{req_order}",
                    "section_instance_key": f"MAIN:8.13.4::{req_order}",
                    "section_title": "故障处理要求",
                    "section_label": f"{label}）",
                    "owner_section_code": "8.13.4",
                    "owner_section_title": "故障处理要求",
                    "owner_section_label": "8.13.4",
                    "structural_parent_section_code": "8.13.4",
                    "structural_parent_section_key": "MAIN:8.13.4",
                    "source_page": 42,
                    "source_page_end": 42,
                    "source_anchor": "Page 42 | Section 8.13.4 故障处理要求",
                    "requirement_text": text,
                    "display_text": text,
                    "raw_text": text,
                    "req_order": req_order,
                    "is_scoped_local_clause": True,
                }
            )

        recovered_specs = [
            {"page_index": 41, "bbox": (40.0, 120.0, 260.0, 134.0), "text": "1) 第一组启动条件"},
            {"page_index": 41, "bbox": (40.0, 138.0, 300.0, 152.0), "text": "2) 第一组处理动作"},
            {"page_index": 41, "bbox": (40.0, 156.0, 280.0, 170.0), "text": "a) 第一组补充说明"},
            {"page_index": 41, "bbox": (40.0, 184.0, 260.0, 198.0), "text": "1) 第二组启动条件"},
            {"page_index": 41, "bbox": (40.0, 202.0, 300.0, 216.0), "text": "2) 第二组处理动作"},
            {"page_index": 41, "bbox": (40.0, 220.0, 280.0, 234.0), "text": "b) 第二组补充说明一"},
            {"page_index": 41, "bbox": (40.0, 238.0, 280.0, 252.0), "text": "c) 第二组补充说明二"},
            {"page_index": 41, "bbox": (40.0, 266.0, 260.0, 280.0), "text": "3) 统一结束条件"},
        ]

        fake_doc_cm = mock.MagicMock()
        fake_doc_cm.__enter__.return_value = object()
        fake_doc_cm.__exit__.return_value = False

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "doc.pdf"
            pdf_path.write_bytes(b"%PDF-1.7\n")
            with mock.patch.object(extraction_preview.fitz, "open", return_value=fake_doc_cm):
                with mock.patch.object(
                    extraction_preview,
                    "_get_section_window_snapshot_meta",
                    return_value={"plan": [(41, (30.0, 100.0, 560.0, 320.0))]},
                ):
                    with mock.patch.object(
                        extraction_preview,
                        "_build_section_visual_line_specs",
                        return_value=recovered_specs,
                    ):
                        repaired = extraction_preview._repair_preview_clause_rows_from_pdf(
                            base_rows,
                            sections=sections,
                            source_file_path=str(pdf_path),
                        )

        numeric_rows = [row for row in repaired if row.get("is_scoped_local_clause")]
        self.assertEqual(
            [row["requirement_text"] for row in numeric_rows],
            [
                "1) 第一组启动条件",
                "2) 第一组处理动作",
                "1) 第二组启动条件",
                "2) 第二组处理动作",
                "3) 统一结束条件",
            ],
        )
        self.assertEqual(
            [row["section_label"] for row in repaired],
            ["1）", "2）", "a）", "1）", "2）", "b）", "c）", "3）"],
        )
        alpha_rows = [row for row in repaired if not row.get("is_scoped_local_clause")]
        self.assertEqual(
            [row["requirement_text"] for row in alpha_rows],
            ["a) 第一组补充说明", "b) 第二组补充说明一", "c) 第二组补充说明二"],
        )
        self.assertTrue(all(row["owner_section_code"] == "8.13.4" for row in alpha_rows))


if __name__ == "__main__":
    unittest.main()
