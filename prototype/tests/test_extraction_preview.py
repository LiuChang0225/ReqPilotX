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
                "|前置条件|说明|\n"
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
            )

        self.assertIsNotNone(repaired)
        self.assertEqual(
            [ref["table_title"] for ref in repaired["table_refs"]],
            ["ASC_F:", "ASC_R:"],
        )
        self.assertEqual(
            [block["kind"] for block in repaired["content_blocks"]],
            ["paragraph", "table", "paragraph", "table"],
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

        entries = extraction_preview._build_complete_requirement_entries(requirements, sections)

        heading_codes = [row.get("section_code") for row in entries if row.get("entry_kind") == "HEADING"]
        self.assertEqual(heading_codes, ["8.10.4"])
        req_rows = [row for row in entries if row.get("entry_kind") == "REQUIREMENT"]
        self.assertEqual(len(req_rows), 1)
        self.assertEqual(req_rows[0]["owner_section_label"], "2）")
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

        entries = extraction_preview._build_complete_requirement_entries(requirements, sections)

        heading_codes = [row.get("section_code") for row in entries if row.get("entry_kind") == "HEADING"]
        self.assertEqual(heading_codes, ["3.2.2", "8.1.2"])
        req_rows = [row for row in entries if row.get("entry_kind") == "REQUIREMENT"]
        self.assertEqual(len(req_rows), 1)
        self.assertEqual(req_rows[0]["owner_section_label"], "8.1.2）")


if __name__ == "__main__":
    unittest.main()
