import tempfile
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import table_assets as ta
from app.services.table_assets import _extract_structured_table
from app.services.table_assets import _coerce_pdf_candidate_rows_to_table
from app.services.table_assets import _build_pdf_crop_plan
from app.services.table_assets import _build_table_match_fragments
from app.services.table_assets import _build_current_entry_anchor_fragments
from app.services.table_assets import _build_context_anchor_fragments
from app.services.table_assets import _compose_page_crop_bbox
from app.services.table_assets import _find_next_local_clause_anchor
from app.services.table_assets import _find_context_anchor_window
from app.services.table_assets import _match_row_slice_bbox
from app.services.table_assets import _looks_like_trailing_table_context_noise
from app.services.table_assets import _prefer_last_before_context_anchor
from app.services.table_assets import _promote_anchor_to_compact_band_start
from app.services.table_assets import _recover_table_from_pdf_source
from app.services.table_assets import _should_generate_visual_snapshot
from app.services.table_assets import _split_text_blocks
from app.services.table_assets import _tighten_anchor_window_crop_bbox
from app.services.table_assets import _write_source_snapshot
from app.services.table_assets import enrich_entry_with_table_assets
from app.services.table_assets import enrich_entries_with_table_assets
from app.services.table_assets import render_text_snapshot_png

_UPLOAD_DIR_CANDIDATES = [
    Path(__file__).resolve().parents[1] / "storage" / "uploads",
    Path("/Users/liuchang/Documents/ReqPilotX/worktrees/_active/extraction-optimization/prototype/storage/uploads"),
]
_OIL_PUMP_SAMPLE_PDF = next(
    (
        sample
        for upload_dir in _UPLOAD_DIR_CANDIDATES
        for sample in sorted(upload_dir.glob("*前后油泵总成*.pdf"))
    ),
    None,
)
_DAMPER_SAMPLE_PDF = next(
    (
        sample
        for upload_dir in _UPLOAD_DIR_CANDIDATES
        for sample in sorted(upload_dir.glob("*全主动减振器总成*.pdf"))
    ),
    None,
)


class TableAssetsParsingTest(unittest.TestCase):
    def test_write_png_atomically_ignores_stale_read_only_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "snapshot.png"
            stale_temp = target.with_name(f".{target.name}.tmp")
            stale_temp.write_bytes(b"stale")
            stale_temp.chmod(0o444)
            image_bytes = render_text_snapshot_png("snapshot body")

            try:
                written = ta._write_png_atomically(target, image_bytes)
            finally:
                stale_temp.chmod(0o666)

            self.assertEqual(written, target)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), image_bytes)

    def test_single_row_pipe_table_does_not_infer_fake_headers(self) -> None:
        headers, rows, mode = _extract_structured_table([
            "|1|外观检查|USCAR-2|按要求执行|PV|10|C Sample|",
        ])
        self.assertEqual(mode, "snapshot_only")
        self.assertEqual(headers, [])
        self.assertEqual(rows, [])

    def test_sparse_group_matrix_rows_preserve_full_column_layout(self) -> None:
        text = "\n".join(
            [
                "|测试项目 / Test Item|测试分组 / Test Groups（推荐）|Col3|Col4|Col5|Col6|Col7|Col8|Col9|Col10|Col11|Col12|Col13|Col14|Col15|Col16|Col17|",
                "|测试项目 / Test Item|组 1|组 2|组 3|组 4|组 5|组 6|组 7|组 8|组 9|组 10|组 11|组 12|组 13|组 14|组 15|组 16|",
                "|测试项目 / Test Item|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|测试顺序 / Test Sequence|",
                "|样品预处理 / Sample Pre-Treatment|1|1|1|1|1|1|1|1|1|1|1|1|1|1|1|1|",
                "|外观及尺寸 / Visual Inspection and Dimension|2,4|2,5|2,4|2,4|2,4|2,4|||2,10|2,9|2,9|2,8|2,15|2,15|2,6|2|",
            ]
        )
        blocks = _split_text_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["kind"], "table")
        headers, rows, mode = _extract_structured_table(blocks[0].get("lines") or [])
        self.assertEqual(mode, "structured")
        self.assertEqual(len(headers), 17)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(rows[0]), 17)
        self.assertEqual(rows[3][7], "")
        self.assertEqual(rows[3][8], "")
        self.assertEqual(rows[3][9], "2,10")

    def test_explicit_bom_header_and_bridge_lines_form_single_table(self) -> None:
        text = "\n".join(
            [
                "3.2.1 产品信息",
                "序号层级代号名称重量（kg） 材料（主要） 数量开发状态",
                "|1|2|2901700X8D1110|/|前油泵总成 ＜16.1 下级件包含：油泵+油管+支架+油液+|/|/|/|",
                "标准件 1件新开发",
                "|2|3|/|/|/ 前油泵本体 ＜13 NR、SiC AlSi10MnMg等|/|1件|新开发|",
                "|8|2|2911700X8D1110|/|后油泵总成 ＜16.9 下级件包含：油泵+油管+支架+油液+|/|/|/|",
                "标准件 1件新开发",
                "|9|3|/|/|/ 后油泵本体 ＜13 NR、SiC AlSi10MnMg等|/|1件|新开发|",
                "备注：test",
            ]
        )
        blocks = _split_text_blocks(text)
        self.assertEqual([block["kind"] for block in blocks], ["paragraph", "table", "paragraph"])
        table_block = blocks[1]
        self.assertEqual(
            table_block.get("header_cells"),
            ["序号", "层级", "代号", "名称", "重量（kg）", "材料（主要）", "数量", "开发状态"],
        )
        headers, rows, mode = _extract_structured_table(table_block.get("lines") or [], explicit_header=table_block.get("header_cells") or [])
        self.assertEqual(mode, "structured")
        self.assertEqual(headers, ["序号", "层级", "代号", "名称", "重量（kg）", "材料（主要）", "数量", "开发状态"])
        self.assertEqual(len(rows), 4)
        self.assertIn("标准件 1件新开发", rows[0][4])
        self.assertIn("标准件 1件新开发", rows[2][4])

    def test_placeholder_title_rows_become_paragraphs_and_preserve_real_table(self) -> None:
        text = "\n".join(
            [
                "3.2.2 产品详细描述",
                "|油泵总成参考示意图 1）油泵性能要求|Col2|Col3|Col4|Col5|",
                "|序号|项目|项目|要求|备注|",
                "|1|总成重量|总成重量|前＜16.1kg，后＜16.9kg|平均质量|",
                "|2|工作电压范围|工作电压范围|350-800V|满功率运行|",
            ]
        )
        blocks = _split_text_blocks(text)
        self.assertEqual([block["kind"] for block in blocks], ["paragraph", "table"])
        self.assertIn("1） 油泵性能要求", blocks[0]["text"])
        headers, rows, mode = _extract_structured_table(blocks[1]["lines"])
        self.assertEqual(mode, "structured")
        self.assertEqual(headers, ["序号", "项目", "要求", "备注"])
        self.assertEqual(rows[0], ["1", "总成重量", "前＜16.1kg，后＜16.9kg", "平均质量"])

    def test_repeated_pipe_columns_are_collapsed_before_table_parse(self) -> None:
        text = "\n".join(
            [
                "5）油管总成性能要求",
                "|5）油管总成性能要求|5）油管总成性能要求|5）油管总成性能要求|",
                "|序号|序号|序号|项目|项目|要求|要求|要求|",
                "|1|1|1|尺寸|尺寸|软管≤Φ17.6×3.8|软管≤Φ17.6×3.8|软管≤Φ17.6×3.8|",
                "|2|2|2|硬管爆破压力|硬管爆破压力|＞400bar|＞400bar|＞400bar|",
            ]
        )
        blocks = _split_text_blocks(text)
        self.assertEqual([block["kind"] for block in blocks], ["paragraph", "table"])
        self.assertIn("5）油管总成性能要求", blocks[0]["text"])
        headers, rows, mode = _extract_structured_table(blocks[1]["lines"])
        self.assertEqual(mode, "structured")
        self.assertEqual(headers, ["序号", "项目", "要求"])
        self.assertEqual(rows[0], ["1", "尺寸", "软管≤Φ17.6×3.8"])

    def test_non_pipe_table_title_band_starts_new_block(self) -> None:
        text = "\n".join(
            [
                "|用途|Col2|常见不安全密码算法|可遗留使用密码算法|推荐使用的强密码算法|",
                "|对称加密|分组加密|Blowfish| |AES-GCM|",
                "工作产出保密性级别 Confidentiality Level of WP",
                "|高度机密highly confidential|only the creator can access|",
            ]
        )
        blocks = _split_text_blocks(text)
        self.assertEqual([block["kind"] for block in blocks], ["table", "paragraph", "table"])
        self.assertIn("Confidentiality Level", blocks[1]["text"])

    def test_single_cell_markdown_row_becomes_paragraph_text(self) -> None:
        self.assertEqual(ta._normalize_block_input_line("|目的：评估用电器端与线束适配性|"), ["目的：评估用电器端与线束适配性"])

    def test_header_stub_row_merges_into_following_subtable(self) -> None:
        text = "\n".join(
            [
                "2.主要设计和验证要求",
                "|序号|项目|要求|",
                "气候性能",
                "|1|低温存储|/|",
                "|2|高温存储|/|",
            ]
        )
        blocks = _split_text_blocks(text)
        self.assertEqual([block["kind"] for block in blocks], ["paragraph", "paragraph", "table"])
        self.assertEqual(blocks[2].get("header_cells"), ["序号", "项目", "要求"])

    def test_markdown_separator_row_stays_in_same_table_block(self) -> None:
        text = "\n".join(
            [
                "试验条件如下：",
                "| Item | Condition |",
                "| --- | --- |",
                "| Voltage | 12V |",
                "| Current | 3A |",
            ]
        )
        blocks = _split_text_blocks(text)
        self.assertEqual([block["kind"] for block in blocks], ["paragraph", "table"])
        headers, rows, mode = _extract_structured_table(blocks[1]["lines"])
        self.assertEqual(mode, "structured")
        self.assertEqual(headers, ["Item", "Condition"])
        self.assertEqual(rows, [["Voltage", "12V"], ["Current", "3A"]])

    @unittest.skipIf(_OIL_PUMP_SAMPLE_PDF is None, "oil pump sample pdf not available")
    def test_write_source_snapshot_regenerates_corrupt_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_root = Path(tmpdir)
            first_bytes = render_text_snapshot_png("first")
            second_bytes = render_text_snapshot_png("second")
            kwargs = {
                "snapshot_root": snapshot_root,
                "source_file_path": str(_OIL_PUMP_SAMPLE_PDF),
                "source_page": 1,
                "source_page_end": 1,
                "table_text": "测试图示",
                "table_title": "测试图示",
                "headers": [],
                "rows": [],
                "notes": [],
                "before_context_text": "",
                "after_context_text": "",
                "source_snapshot_cache": None,
                "source_table_detection_cache": None,
                "source_line_detection_cache": None,
                "source_page_floor_offsets": {},
            }

            with patch("app.services.table_assets._render_pdf_page_span_snapshot", return_value=(first_bytes, {})):
                path, _ = _write_source_snapshot(**kwargs)

            self.assertIsNotNone(path)
            assert path is not None
            path.write_bytes(b"")

            with patch("app.services.table_assets._render_pdf_page_span_snapshot", return_value=(second_bytes, {})):
                rebuilt, _ = _write_source_snapshot(**kwargs)

            self.assertEqual(rebuilt, path)
            self.assertEqual(path.read_bytes(), second_bytes)

    @unittest.skipIf(_OIL_PUMP_SAMPLE_PDF is None, "oil pump sample pdf not available")
    def test_write_source_snapshot_signature_changes_with_clause_window_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_root = Path(tmpdir)
            first_bytes = render_text_snapshot_png("candidate-window")
            second_bytes = render_text_snapshot_png("clause-window")
            kwargs = {
                "snapshot_root": snapshot_root,
                "source_file_path": str(_OIL_PUMP_SAMPLE_PDF),
                "source_page": 12,
                "source_page_end": 12,
                "table_text": "2.3 特殊特性控制要求\n|测量系统分析|初始过程能力|量产过程能力|",
                "table_title": "2.3 特殊特性控制要求",
                "headers": ["测量系统分析", "初始过程能力", "量产过程能力"],
                "rows": [["Cgk≥1.33", "Ppk≥1.67", "关键特性Cpk≥1.67"]],
                "notes": [
                    "2.3 特殊特性控制要求\n针对特殊特性常见的控制方法有防错 、SPC与能力分析和100%检验等。",
                    "当能力不足时，应对发货的产品的进行 100%测量。",
                ],
                "before_context_text": "2.2 特殊特性分类与标识",
                "after_context_text": "可靠性设计目标",
                "source_snapshot_cache": None,
                "source_table_detection_cache": None,
                "source_line_detection_cache": None,
                "source_page_floor_offsets": {},
            }

            with patch("app.services.table_assets._render_pdf_page_span_snapshot", return_value=(first_bytes, {})):
                candidate_path, _ = _write_source_snapshot(**kwargs, prefer_clause_window=False)

            with patch("app.services.table_assets._render_pdf_page_span_snapshot", return_value=(second_bytes, {})):
                clause_path, _ = _write_source_snapshot(**kwargs, prefer_clause_window=True)

            self.assertIsNotNone(candidate_path)
            self.assertIsNotNone(clause_path)
            assert candidate_path is not None
            assert clause_path is not None
            self.assertNotEqual(candidate_path, clause_path)
            self.assertEqual(clause_path.read_bytes(), second_bytes)

    @unittest.skipIf(_OIL_PUMP_SAMPLE_PDF is None, "oil pump sample pdf not available")
    def test_write_source_snapshot_retries_clause_window_for_shallow_complex_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_root = Path(tmpdir)
            first_bytes = render_text_snapshot_png("short")
            second_bytes = render_text_snapshot_png("\n".join(f"row {index}" for index in range(32)))
            kwargs = {
                "snapshot_root": snapshot_root,
                "source_file_path": str(_OIL_PUMP_SAMPLE_PDF),
                "source_page": 24,
                "source_page_end": 24,
                "table_text": "3.产品网络安全生命周期-开发阶段",
                "table_title": "3 产品网络安全生命周期-开发阶段 Product Cybersecurity Lifecycle -Product Development",
                "headers": ["序号", "活动", "描述", "适用", "产出", "保密级别"],
                "rows": [["-1", "开发", "描述", "适用", "产出", "高度机密"]] * 3,
                "notes": ["开发与设计 Development and Design", "测试与验证（Verification & Validation ）"],
                "before_context_text": "2.产品网络安全生命周期-概念设计阶段",
                "after_context_text": "4.产品网络安全生命周期-生产阶段",
                "source_snapshot_cache": None,
                "source_table_detection_cache": None,
                "source_line_detection_cache": None,
                "source_page_floor_offsets": {},
            }

            with patch(
                "app.services.table_assets._render_pdf_page_span_snapshot",
                side_effect=[(first_bytes, {"page_span_count": 1}), (second_bytes, {"page_span_count": 1})],
            ) as mocked:
                path, _ = _write_source_snapshot(**kwargs)

            self.assertIsNotNone(path)
            assert path is not None
            self.assertEqual(path.read_bytes(), second_bytes)
            self.assertEqual(mocked.call_count, 2)

    def test_compose_vertical_images_does_not_apply_height_cap_by_default(self) -> None:
        images = [
            PILImage.new("RGB", (920, 1000), "white"),
            PILImage.new("RGB", (920, 900), "white"),
        ]

        rendered = ta._compose_vertical_images(images, max_width_px=920)

        with PILImage.open(BytesIO(rendered)) as image:
            self.assertEqual(image.size, (920, 1918))

    def test_compose_vertical_images_applies_height_cap_when_requested(self) -> None:
        images = [
            PILImage.new("RGB", (920, 1000), "white"),
            PILImage.new("RGB", (920, 900), "white"),
        ]

        rendered = ta._compose_vertical_images(images, max_width_px=920, max_height_px=1480)

        with PILImage.open(BytesIO(rendered)) as image:
            self.assertEqual(image.size, (709, 1480))

    def test_build_pdf_crop_plan_can_keep_full_band_without_next_clause_stop(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _TableResult:
            tables: list[object] = []

        class _Page:
            rect = _Rect()

            def find_tables(self):
                return _TableResult()

            def get_text(self, mode: str) -> dict:
                if mode != "dict":
                    raise AssertionError(mode)
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {"bbox": (55.0, 100.0, 180.0, 110.0), "spans": [{"text": "1 总体要求"}]},
                                {"bbox": (70.0, 132.0, 520.0, 150.0), "spans": [{"text": "band row 1"}]},
                                {"bbox": (70.0, 362.0, 520.0, 408.0), "spans": [{"text": "band row 2"}]},
                                {"bbox": (55.0, 500.0, 210.0, 510.0), "spans": [{"text": "2 概念设计"}]},
                            ],
                        }
                    ]
                }

            def get_drawings(self) -> list[dict]:
                return []

        class _Document:
            def load_page(self, page_index: int):
                if page_index != 0:
                    raise AssertionError(page_index)
                return _Page()

        document = _Document()
        start_anchor = {"page_index": 0, "bbox": (55.0, 100.0, 180.0, 110.0), "text": "1 总体要求"}
        end_anchor = {"page_index": 0, "bbox": (55.0, 500.0, 210.0, 510.0), "text": "2 概念设计"}

        with patch(
            "app.services.table_assets._find_next_local_clause_anchor",
            return_value={"page_index": 0, "bbox": (70.0, 160.0, 220.0, 170.0), "text": "供应商网络安全能力证明"},
        ):
            truncated_plan = _build_pdf_crop_plan(
                document,
                source_key="sample",
                page_indices=[0],
                match_fragments=[],
                headers=[],
                slice_rows=[],
                start_anchor=start_anchor,
                end_anchor=end_anchor,
                detection_cache={},
                source_page_floor_offsets={},
                prefer_anchor_window=True,
                allow_next_clause_stop=True,
            )
            band_plan = _build_pdf_crop_plan(
                document,
                source_key="sample",
                page_indices=[0],
                match_fragments=[],
                headers=[],
                slice_rows=[],
                start_anchor=start_anchor,
                end_anchor=end_anchor,
                detection_cache={},
                source_page_floor_offsets={},
                prefer_anchor_window=True,
                allow_next_clause_stop=False,
            )

        self.assertLess(truncated_plan[0][1][3], 180.0)
        self.assertGreater(band_plan[0][1][3], 400.0)

    def test_short_table_with_meaningful_context_prefers_clause_window(self) -> None:
        self.assertTrue(
            ta._should_prefer_source_clause_window(
                table_title="2.2 特殊特性分类与标识",
                render_mode="structured",
                headers=["项目", "内容", "备注"],
                rows=[["安全相关关键特性", "法规相关关键特性", "功能相关重要特性"]],
                source_page=12,
                source_page_end=12,
                notes=[
                    "2.2 特殊特性分类与标识\n我司对特殊特性分类：安全相关关键特性，法规相关关键特性，功能相关重要特性，对应特性标识符号见下表。",
                ],
            )
        )

    def test_short_heading_subtable_prefers_clause_window(self) -> None:
        self.assertFalse(
            ta._should_prefer_source_clause_window(
                table_title="气候性能",
                render_mode="structured",
                headers=["序号", "项目", "要求"],
                rows=[["1", "低温存储", "/"]] * 8,
                source_page=13,
                source_page_end=13,
                notes=[],
            )
        )

    def test_cross_page_numbered_section_table_does_not_prefer_clause_window(self) -> None:
        self.assertFalse(
            ta._should_prefer_source_clause_window(
                table_title="15 高温寿命(仅防水件) High Temperature Exposure",
                render_mode="structured",
                headers=["15", "高温寿命(仅防水件) High Temperature Exposure", "外观 Visual Inspection"],
                rows=[
                    ["15", "高温寿命(仅防水件) High Temperature Exposure", "连接器或端子插拔 Connector and/or Terminal Cycling"],
                    ["15", "高温寿命(仅防水件) High Temperature Exposure", "绝缘电阻 Insulation Resistance"],
                    ["15", "高温寿命(仅防水件) High Temperature Exposure", "气密性 Pressure Leakage"],
                ],
                source_page=33,
                source_page_end=34,
                notes=["15 高温寿命(仅防水件) High Temperature Exposure"],
                trailing_context_text="盐雾试验 Salt Spray Test",
            )
        )

    def test_large_delivery_table_uses_candidate_window(self) -> None:
        self.assertFalse(
            ta._should_prefer_source_clause_window(
                table_title="5.2 交付物要求",
                render_mode="structured",
                headers=["交付物清单", "类别", "交付时间", "备注"],
                rows=[
                    ["1", "设计方案说明书", "/", "TR阶段"],
                    ["2", "初版3D数模", "/", "TR阶段"],
                    ["3", "初版2D总成图纸", "/", "软模阶段交样前"],
                    ["4", "BOM", "/", "软模阶段交样前"],
                    ["5", "DFMEA", "/", ""],
                    ["6", "尺寸检测报告", "/", ""],
                    ["7", "性能检测报告", "/", ""],
                    ["8", "DVP测试报告", "/", ""],
                    ["9", "产品描述书", "/", ""],
                ],
                source_page=10,
                source_page_end=10,
                notes=[],
            )
        )

    def test_long_software_development_table_uses_candidate_window(self) -> None:
        self.assertFalse(
            ta._should_prefer_source_clause_window(
                table_title="软件开发要求",
                render_mode="structured",
                headers=["条款编号", "要求项目", "要求描述"],
                rows=[
                    ["SE00.001", "供应商软件质量资质评估", "1. 取得软件认证ASPICE，CMMI(考察项)..."],
                    ["SE00.002", "有软件开发流程按照ASPICE 管控", "1. 有明确清晰的软件开发流程..."],
                    ["SE00.003", "对阶段软件度量指标管理与评审", "1. 满足公司对软件质量提出的质量目标..."],
                    ["SE00.004", "测试用例联合评审", "1. 组织与甲方公司工程师开展测试用例的评审..."],
                    ["SE00.005", "人力资源评估", "1. 配合开展人力资源满足度评价..."],
                    ["SE00.006", "需求计划评审", "1. 根据需求开发规范，制定需求管控计划..."],
                    ["SE00.007", "软件包交付评审", "1. 软件包的交付需要按照《软件三件套》要求开展评价..."],
                    ["SE00.008", "缺陷管理", "1. 对与重点缺陷问题需要开展端到端过程复盘..."],
                    ["SE00.009", "软件外包管理（如有）", "1. 软件模块是否有过外包（外包商名称）..."],
                    ["SE00.010", "支持与配合", "1. 配合项目开展所有资源的协调..."],
                    ["SE00.011", "软件度量指标", "1. 参考软件质量协议确定并签订指标..."],
                ],
                source_page=11,
                source_page_end=11,
                notes=[],
            )
        )

    def test_numbered_outline_slice_does_not_retry_into_clause_window(self) -> None:
        image = PILImage.new("RGB", (920, 140), "white")
        output = BytesIO()
        image.save(output, format="PNG")
        rendered = output.getvalue()

        self.assertFalse(
            ta._should_retry_source_snapshot_with_clause_window(
                rendered=rendered,
                render_meta={"page_span_count": 1},
                table_title="4 对插到位声响 Connector to Connector Audible Click",
                headers=["序号", "测试项目", "测试顺序", "性能要求", "阶段", "样本量"],
                rows=[
                    ["4", "对插到位声响", "外观", "无外观损伤", "PV", "16"],
                    ["4", "对插到位声响", "对插到位声响", "50dB 环境声音下", "PV", "16"],
                ],
                notes=[],
            )
        )

    def test_compact_labeled_info_table_does_not_retry_into_clause_window(self) -> None:
        image = PILImage.new("RGB", (920, 140), "white")
        output = BytesIO()
        image.save(output, format="PNG")
        rendered = output.getvalue()

        self.assertFalse(
            ta._should_retry_source_snapshot_with_clause_window(
                rendered=rendered,
                render_meta={"page_span_count": 1},
                table_title="整车应用基本信息",
                headers=["用电器端基本信息", "说明"],
                rows=[
                    ["布置位置", "前舱发动机本体 乘员舱仪表台"],
                    ["工作温度", "示例：-40-+105°C（T2乘员舱：-40°C-+100°C / T3前舱：-40°C-+125°C）"],
                    ["防水等级", "示例：S2(防水区域)/S3(防水区域并有直接水泼溅)"],
                ],
                notes=["整车用电器与线束匹配评估表"],
            )
        )

    def test_heading_anchor_context_preserves_numeric_section_code(self) -> None:
        self.assertEqual(
            ta._entry_anchor_context_text(
                {
                    "entry_kind": "HEADING",
                    "section_code": "46",
                    "section_title": "上支撑—橡胶衬套硬度",
                    "display_text": "上支撑—橡胶衬套硬度",
                },
                from_end=True,
            ),
            "46 上支撑—橡胶衬套硬度",
        )

    def test_axis_symbol_table_prefers_clause_window(self) -> None:
        self.assertTrue(
            ta._should_prefer_source_clause_window(
                table_title="8.1.3）在下列分析工况下，油泵总成质心位移满足下表中的要求；",
                render_mode="structured",
                headers=["总成自由度", "质心最大位移 mm/° 下限", "质心最大位移 mm/° 上限"],
                rows=[
                    ["X", "-4", "4"],
                    ["Y", "-2.5", "2.5"],
                    ["Z", "-4", "4"],
                    ["RX", "-0.5", "0.5"],
                    ["RY", "-2.5", "2.5"],
                    ["RZ", "-0.5", "0.5"],
                ],
                source_page=8,
                source_page_end=8,
                notes=["油泵总成质心位移控制要求\n（整车坐标系）"],
            )
        )

    def test_source_anchor_clause_title_is_used_for_table_only_entry(self) -> None:
        row = {
            "raw_text": "|模态|频率（Hz） 下限|频率（Hz） 上限|",
            "source_anchor": "Page 7 | Clause 8.1.2）产品各方向解耦率及各向频率应满足下表的要求 | Section 3.2.2 产品详细描述",
            "section_title": "3.2.2 产品详细描述",
        }
        self.assertEqual(
            ta._table_title_for_row(row),
            "8.1.2）产品各方向解耦率及各向频率应满足下表的要求",
        )

    def test_local_clause_anchor_prefix_stops_at_semicolon_before_table_band(self) -> None:
        self.assertEqual(
            ta._extract_local_clause_anchor_prefix(
                "8.1.3）在下列分析工况下，油泵总成质心位移满足下表中的要求； 油泵总成质心位移控制要求 （整车坐标系）"
            ),
            "8.1.3）在下列分析工况下，油泵总成质心位移满足下表中的要求",
        )

    def test_leading_table_title_block_is_added_for_table_only_entry_even_if_raw_text_contains_title(self) -> None:
        blocks = ta._ensure_leading_table_title_block(
            [{"kind": "table", "table_id": "tbl_123"}],
            row={
                "raw_text": "8.1.2）产品各方向解耦率及各向频率应满足下表的要求\n|模态|频率（Hz） 下限|频率（Hz） 上限|",
            },
            table_title="8.1.2）产品各方向解耦率及各向频率应满足下表的要求",
        )
        self.assertEqual(blocks[0], {"kind": "paragraph", "text": "8.1.2）产品各方向解耦率及各向频率应满足下表的要求"})
        self.assertEqual(blocks[1], {"kind": "table", "table_id": "tbl_123"})

    def test_environment_requirement_does_not_trigger_visual_snapshot_by_keywords_alone(self) -> None:
        text = "\n".join(
            [
                "1.1 环境适应性要求（必备）",
                "前油泵部件安装在前副车架位置，需满足工作温度-40℃～105℃，存储温度-40℃～105℃；后油泵安装在后轮转向和尾消之间，需满足工作温度-40℃～105℃，存储温度-40℃～105℃。",
                "低压电器件防护等级要求（湿区）：",
                "（1）离地高度≤VTS最高涉水深度+200mm，要求≥IP67；",
                "（2）离地高度＞VTS最高涉水深度+200mm，要求≥IP66；",
                "（3）尾门内腔冷凝水滴落区域，要求≥IP52；",
                "涉水要求：各零部件在整车涉水后不能出现渗水或者漏水，整车性能不能出现下降。",
            ]
        )
        self.assertFalse(ta._should_generate_visual_snapshot(text, title="1.1 环境适应性要求（必备）"))

    def test_snapshot_only_materialization_uses_title_surrogate_instead_of_pipe_text(self) -> None:
        self.assertEqual(
            ta._materialize_table_block_text(
                {
                    "table_title": "X 测试分组 / Test Groups",
                    "slice_render_mode": "snapshot_only",
                    "inherited_headers": ["测试项目 / Test Item", "测试分组 / Test Groups（推荐）", "Col3"],
                    "slice_rows": [],
                }
            ),
            "X 测试分组 / Test Groups",
        )

    def test_source_first_structured_table_materialization_uses_title_surrogate(self) -> None:
        self.assertEqual(
            ta._materialize_table_block_text(
                {
                    "table_title": "常见不安全密码算法",
                    "slice_render_mode": "structured",
                    "recommended_export_variant": "source",
                    "source_snapshot_path": "/tmp/source.png",
                    "inherited_headers": ["用途", "类别", "常见不安全密码算法", "可遗留使用密码算法", "推荐使用的强密码算法"],
                    "slice_rows": [["对称加密", "分组加密", "Blowfish", "", "AES-GCM"]],
                }
            ),
            "常见不安全密码算法",
        )

    def test_materialized_requirement_text_dedupes_repeated_title_surrogate(self) -> None:
        row = {
            "raw_text": "X 测试分组 / Test Groups",
            "content_blocks": [
                {"kind": "paragraph", "text": "X 测试分组 / Test Groups"},
                {"kind": "table", "table_id": "tbl-x"},
            ],
            "table_refs": [
                {
                    "table_id": "tbl-x",
                    "table_title": "X 测试分组 / Test Groups",
                    "slice_render_mode": "snapshot_only",
                }
            ],
        }
        self.assertEqual(ta._materialize_requirement_text(row), "X 测试分组 / Test Groups")

    def test_materialized_requirement_text_skips_source_first_table_after_paragraph(self) -> None:
        row = {
            "raw_text": "4.集成验证要求：",
            "content_blocks": [
                {"kind": "paragraph", "text": "4.集成验证要求：\n产品需满足由OEM组织的系统级和整车级试验要求。"},
                {"kind": "table", "table_id": "tbl-source"},
            ],
            "table_refs": [
                {
                    "table_id": "tbl-source",
                    "table_title": "4 集成验证要求",
                    "slice_render_mode": "snapshot_only",
                    "recommended_export_variant": "source",
                    "source_snapshot_path": "/tmp/source.png",
                }
            ],
        }
        self.assertEqual(
            ta._materialize_requirement_text(row),
            "4.集成验证要求：\n产品需满足由OEM组织的系统级和整车级试验要求。",
        )

    def test_materialized_requirement_text_skips_large_source_backed_slice_table_markdown(self) -> None:
        row = {
            "raw_text": "用电器端基本信息",
            "content_blocks": [
                {"kind": "paragraph", "text": "用电器端基本信息"},
                {"kind": "table", "table_id": "tbl-source-backed-slice"},
            ],
            "table_refs": [
                {
                    "table_id": "tbl-source-backed-slice",
                    "table_title": "用电器端基本信息",
                    "slice_render_mode": "structured",
                    "recommended_export_variant": "slice",
                    "source_snapshot_path": "/tmp/source.png",
                    "inherited_headers": ["模块侧接口类型", "项目", "要求", "数量", "风险评估"],
                    "slice_rows": [["自开模", "模具号", "提供模具号", "3pcs", ""]] * 8,
                }
            ],
        }
        self.assertEqual(ta._materialize_requirement_text(row), "用电器端基本信息")

    def test_split_embedded_anchor_line_fragments_breaks_merged_heading_line(self) -> None:
        fragments = ta._split_embedded_anchor_line_fragments(
            "1 盐雾腐蚀 3.样件数量要求： 为验证和考核部件的一致性 4.集成验证要求： 产品需满足系统级试验要求。",
            (0.0, 0.0, 100.0, 60.0),
        )
        self.assertEqual(len(fragments), 3)
        self.assertEqual(fragments[0]["text"], "1 盐雾腐蚀")
        self.assertTrue(fragments[1]["text"].startswith("3.样件数量要求"))
        self.assertTrue(fragments[2]["text"].startswith("4.集成验证要求"))

    def test_numbered_dot_heading_counts_as_headingish_single_cell(self) -> None:
        self.assertTrue(ta._looks_like_headingish_single_cell("4.集成验证要求："))

    def test_clean_paragraph_block_text_drops_trailing_table_intro_band(self) -> None:
        text = "\n".join(
            [
                "4.集成验证要求：",
                "产品需满足由OEM组织的系统级和整车级试验要求。",
                "序号测试类别测试小类测试项目",
                "1 产品SE或者项目组定义",
                "系统级测试",
                "Col1",
            ]
        )
        self.assertEqual(
            ta._clean_paragraph_block_text(text),
            "4.集成验证要求：\n产品需满足由OEM组织的系统级和整车级试验要求。",
        )

    def test_clean_paragraph_block_text_drops_reference_and_markdown_noise(self) -> None:
        text = "\n".join(
            [
                "Ref： ISO 21434 Annex C",
                "~~time~~ 1 总体要求 General Requirements",
                "项目开展前总体要求 General Requirements before the Project Launches",
            ]
        )
        self.assertEqual(
            ta._clean_paragraph_block_text(text),
            "1 总体要求 General Requirements\n项目开展前总体要求 General Requirements before the Project Launches",
        )

    def test_normalize_pdf_candidate_row_repairs_fragmented_cjk_label(self) -> None:
        row = ta._normalize_pdf_candidate_row(
            ["高 Pr压 es水 su喷 re射 p/ aH yigh S r", "产品无进水"]
        )

        self.assertIn("高压水喷射", row[0])
        self.assertEqual(row[1], "产品无进水")

    def test_normalize_pdf_candidate_row_repairs_fragmented_decimal_cells(self) -> None:
        row = ta._normalize_pdf_candidate_row(["-2 .5", "25 .", "-0 .5", "05 ."])

        self.assertEqual(row, ["-2.5", "2.5", "-0.5", "0.5"])

    def test_normalize_pdf_candidate_row_repairs_shifted_cjk_punctuation(self) -> None:
        row = ta._normalize_pdf_candidate_row(["7 附件目录 、"])

        self.assertEqual(row, ["7、附件目录"])

    def test_normalize_pdf_candidate_row_repairs_decimal_inside_mixed_text(self) -> None:
        row = ta._normalize_pdf_candidate_row(["长度L＜405mm，宽度b＜122mm，高度h＜158 .2mm"])

        self.assertEqual(row, ["长度L＜405mm，宽度b＜122mm，高度h＜158.2mm"])

    def test_normalize_pdf_candidate_row_repairs_fragmented_plusminus_decimals(self) -> None:
        row = ta._normalize_pdf_candidate_row(["±005 ±05 . 、 ."])

        self.assertEqual(row, ["±0.05、±0.5"])

    def test_resolve_table_block_title_prefers_neighbor_heading(self) -> None:
        self.assertEqual(
            ta._resolve_table_block_title(
                blocks=[
                    {
                        "kind": "paragraph",
                        "text": "整车用电器与线束匹配评估表\n目的说明\n整车应用基本信息\n项目",
                    },
                    {"kind": "table", "text": "|布置位置|前舱|"},
                ],
                block_index=1,
                fallback_title="W 整车用电器与线束匹配评估表",
            ),
            "整车应用基本信息",
        )
        self.assertEqual(
            ta._resolve_table_block_title(
                blocks=[
                    {"kind": "paragraph", "text": "Ref： ISO 21434 Annex C\nRASIC Definition RASIC 定义"},
                    {"kind": "table", "text": "|主责（R）|说明|"},
                ],
                block_index=1,
                fallback_title="常见不安全密码算法",
            ),
            "RASIC Definition RASIC 定义",
        )
        self.assertEqual(
            ta._resolve_table_block_title(
                blocks=[
                    {
                        "kind": "paragraph",
                        "text": "4.集成验证要求：\n产品需满足系统级试验要求。\n序号测试类别测试小类测试项目\n1 产品SE或者项目组定义\n系统级测试\nCol1",
                    },
                    {"kind": "table", "text": "|2|产品SE或者项目组定义|"},
                ],
                block_index=1,
                fallback_title="4 集成验证要求",
            ),
            "4.集成验证要求：",
        )

    def test_low_quality_structured_table_recommends_source(self) -> None:
        recommendation = ta._recommend_table_export_variant(
            {
                "source_snapshot_path": "/tmp/source.png",
                "slice_render_mode": "structured",
                "inherited_headers": ["1", "低温存储", "/"],
                "slice_rows": [
                    ["2", "高温存储", "/"],
                    ["3", "低温工作和唤醒", "/"],
                ],
                "source_page": 13,
                "source_page_end": 13,
                "structure_confidence": 0.88,
            }
        )
        self.assertEqual(recommendation["recommended_export_variant"], "source")
        self.assertIn("切片可读性差", recommendation["export_variant_reasons"])

    def test_two_column_pdf_title_band_rows_become_notes(self) -> None:
        recovered = _coerce_pdf_candidate_rows_to_table(
            [
                ["工作产出保密性级别 Confidentiality Level of WP", ""],
                ["高度机密highly confidential", "only the creator can access"],
                ["机密confidential", "customer and supplier can access"],
            ],
            current_headers=[],
        )
        self.assertIsNotNone(recovered)
        headers, rows, notes = recovered or ([], [], [])
        self.assertEqual(headers, ["项目", "内容"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "高度机密highly confidential")
        self.assertIn("Confidentiality Level", notes[0])

    def test_pdf_decoupling_table_reassigns_stacked_frequency_targets_to_following_rows(self) -> None:
        recovered = _coerce_pdf_candidate_rows_to_table(
            [
                ["油泵总成刚体模态分配（整车坐标系）", "", "", "", ""],
                ["", "频率（Hz）", "", "", "解耦率（%）"],
                ["模态", "下限", "上限", "目标", ""],
                ["For/Aft", "15", "30", "避开 40-60Hz 38-45Hz 70-95Hz 120-180Hz", "90"],
                ["Lateral", "15", "30", "", "90"],
                ["Bounce", "15", "25", "", "90"],
                ["Roll", "15", "45", "", "90"],
                ["Pitch", "45", "55", "", "90"],
                ["Yaw", "15", "45", "", "90"],
            ],
            current_headers=[],
        )
        self.assertIsNotNone(recovered)
        headers, rows, notes = recovered or ([], [], [])
        self.assertEqual(headers, ["模态", "频率（Hz） 下限", "频率（Hz） 上限", "频率（Hz） 目标", "解耦率（%）"])
        self.assertEqual(rows[0][3], "")
        self.assertEqual(rows[1][3], "避开 40-60Hz")
        self.assertEqual(rows[2][3], "38-45Hz")
        self.assertEqual(rows[3][3], "70-95Hz")
        self.assertEqual(rows[4][3], "120-180Hz")
        self.assertEqual(rows[5][3], "")
        self.assertEqual(notes, ["油泵总成刚体模态分配（整车坐标系）"])

    def test_noise_and_stacked_title_rows_do_not_pollute_recovered_pdf_headers(self) -> None:
        recovered = _coerce_pdf_candidate_rows_to_table(
            [
                ["…", "", "…", ""],
                ["6.4）低压电气接口", "", "", ""],
                ["（整车坐标系）", "", "", ""],
                ["序号", "定义", "描述", "备注"],
                ["1", "低压接口", "见接口定义", ""],
            ],
            current_headers=[],
        )
        self.assertIsNotNone(recovered)
        headers, rows, notes = recovered or ([], [], [])
        self.assertEqual(headers, ["序号", "定义", "描述", "备注"])
        self.assertEqual(rows[0], ["1", "低压接口", "见接口定义", ""])
        self.assertEqual(notes, ["6.4）低压电气接口", "（整车坐标系）"])

    def test_row_slice_bbox_starts_from_matched_table_header_not_previous_table(self) -> None:
        bbox = _match_row_slice_bbox(
            {
                "bbox": (10.0, 10.0, 300.0, 400.0),
                "rows_text": [
                    ["前一个表头", "值"],
                    ["1", "旧行"],
                    ["序号", "项目", "要求"],
                    ["1", "尺寸", "≤10"],
                    ["2", "重量", "≤20"],
                ],
                "row_bboxes": [
                    (10.0, 20.0, 300.0, 40.0),
                    (10.0, 40.0, 300.0, 60.0),
                    (10.0, 100.0, 300.0, 120.0),
                    (10.0, 120.0, 300.0, 140.0),
                    (10.0, 140.0, 300.0, 160.0),
                ],
            },
            headers=["序号", "项目", "要求"],
            slice_rows=[
                ["1", "尺寸", "≤10"],
                ["2", "重量", "≤20"],
            ],
        )
        self.assertEqual(bbox, (10.0, 100.0, 300.0, 160.0))

    def test_row_slice_bbox_prefers_latest_tail_match_when_last_row_text_repeats(self) -> None:
        bbox = _match_row_slice_bbox(
            {
                "bbox": (10.0, 10.0, 300.0, 400.0),
                "rows_text": [
                    ["序号", "项目"],
                    ["1", "前油泵总成"],
                    ["2", "前油泵本体"],
                    ["3", "油液支架及标准件"],
                    ["8", "后油泵总成"],
                    ["9", "后油泵本体"],
                    ["14", "油液支架及标准件"],
                ],
                "row_bboxes": [
                    (10.0, 20.0, 300.0, 40.0),
                    (10.0, 40.0, 300.0, 60.0),
                    (10.0, 60.0, 300.0, 80.0),
                    (10.0, 80.0, 300.0, 100.0),
                    (10.0, 100.0, 300.0, 120.0),
                    (10.0, 120.0, 300.0, 140.0),
                    (10.0, 140.0, 300.0, 160.0),
                ],
            },
            headers=["序号", "项目"],
            slice_rows=[
                ["1", "前油泵总成"],
                ["2", "前油泵本体"],
                ["3", "油液支架及标准件"],
                ["8", "后油泵总成"],
                ["9", "后油泵本体"],
                ["14", "油液支架及标准件"],
            ],
        )
        self.assertEqual(bbox, (10.0, 20.0, 300.0, 160.0))

    def test_compose_page_crop_bbox_keeps_row_slice_top_when_anchor_starts_lower(self) -> None:
        class _Rect:
            width = 400.0
            height = 500.0

        class _Page:
            rect = _Rect()

        bbox = _compose_page_crop_bbox(
            _Page(),
            chosen={
                "bbox": (10.0, 10.0, 300.0, 400.0),
                "rows_text": [
                    ["总标题", "说明"],
                    ["序号", "项目", "要求"],
                    ["1", "尺寸", "≤10"],
                    ["2", "重量", "≤20"],
                ],
                "row_bboxes": [
                    (10.0, 80.0, 300.0, 100.0),
                    (10.0, 100.0, 300.0, 120.0),
                    (10.0, 120.0, 300.0, 140.0),
                    (10.0, 140.0, 300.0, 160.0),
                ],
            },
            start_anchor={"bbox": (12.0, 112.0, 280.0, 124.0), "text": "8.1.2）产品各方向解耦率"},
            end_anchor=None,
            headers=["序号", "项目", "要求"],
            slice_rows=[
                ["1", "尺寸", "≤10"],
                ["2", "重量", "≤20"],
            ],
        )
        self.assertEqual(bbox[1], 92.0)

    def test_compose_page_crop_bbox_keeps_row_slice_tail_when_next_anchor_is_tight(self) -> None:
        class _Rect:
            width = 400.0
            height = 500.0

        class _Page:
            rect = _Rect()

        bbox = _compose_page_crop_bbox(
            _Page(),
            chosen={
                "bbox": (10.0, 10.0, 300.0, 400.0),
                "rows_text": [
                    ["序号", "项目", "要求"],
                    ["1", "尺寸", "≤10"],
                    ["2", "重量", "≤20"],
                    ["3", "爆破压力", "≥30"],
                ],
                "row_bboxes": [
                    (10.0, 100.0, 300.0, 120.0),
                    (10.0, 120.0, 300.0, 140.0),
                    (10.0, 140.0, 300.0, 160.0),
                    (10.0, 160.0, 300.0, 180.0),
                ],
            },
            start_anchor=None,
            end_anchor={"bbox": (12.0, 190.0, 280.0, 202.0), "text": "6）下一条要求"},
            headers=["序号", "项目", "要求"],
            slice_rows=[
                ["1", "尺寸", "≤10"],
                ["2", "重量", "≤20"],
                ["3", "爆破压力", "≥30"],
            ],
        )
        self.assertEqual(bbox[3], 184.0)

    def test_compose_page_crop_bbox_ignores_row_slice_that_starts_far_above_strong_anchor(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

        bbox = _compose_page_crop_bbox(
            _Page(),
            chosen={
                "bbox": (42.24, 60.12, 545.46, 777.80),
                "rows_text": [
                    ["序号", "项目", "要求"],
                    ["1", "低温存储", "/"],
                    ["2", "高温存储", "/"],
                ],
                "row_bboxes": [
                    (42.24, 72.12, 545.46, 120.00),
                    (42.24, 120.00, 545.46, 220.00),
                    (42.24, 220.00, 545.46, 499.80),
                ],
            },
            start_anchor={"bbox": (55.2, 500.0, 254.6, 507.6), "text": "2.主要设计和验证要求"},
            end_anchor={"bbox": (55.2, 703.0, 196.7, 711.8), "text": "3.样件数量要求："},
            headers=["序号", "项目", "要求"],
            slice_rows=[
                ["1", "低温存储", "/"],
                ["2", "高温存储", "/"],
            ],
        )
        self.assertGreaterEqual(bbox[1], 488.0)

    def test_compose_page_crop_bbox_allows_end_anchor_to_trim_clean_single_row_table_tail(self) -> None:
        class _Rect:
            width = 400.0
            height = 500.0

        class _Page:
            rect = _Rect()

        bbox = _compose_page_crop_bbox(
            _Page(),
            chosen={
                "bbox": (10.0, 10.0, 300.0, 400.0),
                "rows_text": [
                    ["3.1 车型信息"],
                    ["项目代号", "车型代号", "手工件到样时间"],
                    ["X2548/X2549", "X8D11/X8D01", "2026.03.30"],
                ],
                "row_bboxes": [
                    (10.0, 80.0, 300.0, 100.0),
                    (10.0, 100.0, 300.0, 120.0),
                    (10.0, 120.0, 300.0, 180.0),
                ],
            },
            start_anchor={"bbox": (12.0, 118.0, 280.0, 128.0), "text": "3.1 车型信息"},
            end_anchor={"bbox": (12.0, 170.0, 280.0, 182.0), "text": "3.2 产品技术要求及说明"},
            headers=["项目代号", "车型代号", "手工件到样时间"],
            slice_rows=[["X2548/X2549", "X8D11/X8D01", "2026.03.30"]],
        )
        self.assertLess(bbox[3], 160.0)

    def test_compose_page_crop_bbox_ignores_contaminated_single_row_slice_when_next_heading_leaks_into_row(self) -> None:
        class _Rect:
            width = 400.0
            height = 500.0

        class _Page:
            rect = _Rect()

        bbox = _compose_page_crop_bbox(
            _Page(),
            chosen={
                "bbox": (10.0, 10.0, 300.0, 400.0),
                "rows_text": [
                    ["3.1 车型信息"],
                    ["项目代号", "车型代号", "手工件到样时间"],
                    ["X2548/X2549 3.2 产品技术要求及说明 3.2.1 产品信息", "X8D11/X8D01", "2026.03.30"],
                ],
                "row_bboxes": [
                    (10.0, 80.0, 300.0, 100.0),
                    (10.0, 100.0, 300.0, 120.0),
                    (10.0, 120.0, 300.0, 180.0),
                ],
            },
            start_anchor={"bbox": (12.0, 118.0, 280.0, 128.0), "text": "3.1 车型信息"},
            end_anchor={"bbox": (12.0, 170.0, 280.0, 182.0), "text": "3.2 产品技术要求及说明"},
            headers=["项目代号", "车型代号", "手工件到样时间"],
            slice_rows=[["X2548/X2549 3.2 产品技术要求及说明 3.2.1 产品信息", "X8D11/X8D01", "2026.03.30"]],
        )
        self.assertEqual(bbox[1], 112.0)
        self.assertEqual(bbox[3], 408.0)

    def test_compose_page_crop_bbox_keeps_single_row_slice_when_end_anchor_is_inline_table_cell(self) -> None:
        class _Rect:
            width = 400.0
            height = 500.0

        class _Page:
            rect = _Rect()

        bbox = _compose_page_crop_bbox(
            _Page(),
            chosen={
                "bbox": (10.0, 10.0, 300.0, 400.0),
                "rows_text": [
                    ["序号", "项目", "DV/PV"],
                    ["4", "滑动摩擦力", "DV/PV"],
                    ["5", "抗泡沫性", "DV/PV"],
                ],
                "row_bboxes": [
                    (10.0, 100.0, 300.0, 120.0),
                    (10.0, 120.0, 300.0, 180.0),
                    (10.0, 180.0, 300.0, 240.0),
                ],
            },
            start_anchor={"bbox": (12.0, 118.0, 280.0, 128.0), "text": "滑动摩擦力"},
            end_anchor={"bbox": (250.0, 145.0, 280.0, 156.0), "text": "DV/PV"},
            headers=["序号", "项目", "DV/PV"],
            slice_rows=[["4", "滑动摩擦力", "DV/PV"]],
        )
        self.assertEqual(bbox[1], 92.0)
        self.assertEqual(bbox[3], 188.0)

    def test_compose_page_crop_bbox_tightens_shallow_single_row_slice_horizontally(self) -> None:
        class _Rect:
            width = 600.0
            height = 500.0

        class _Page:
            rect = _Rect()

            def get_text(self, mode: str) -> dict:
                if mode != "dict":
                    raise AssertionError(mode)
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {"bbox": (59.2, 121.2, 63.3, 125.3), "spans": [{"text": "96"}]},
                                {"bbox": (68.1, 121.1, 89.8, 125.4), "spans": [{"text": "防尘罩拔脱"}]},
                                {"bbox": (112.0, 118.4, 220.3, 122.8), "spans": [{"text": "固定上支撑一端，使用拉力机拉动防尘罩，方向：上支撑"}]},
                                {"bbox": (112.0, 123.8, 188.0, 128.2), "spans": [{"text": "轴向方向,加载速度30mm/min,温度23℃,"}]},
                                {"bbox": (223.5, 121.1, 249.7, 125.4), "spans": [{"text": "拔脱力≥100N"}]},
                                {"bbox": (364.3, 121.2, 374.6, 125.3), "spans": [{"text": "DV/PV"}]},
                                {"bbox": (386.1, 121.1, 388.4, 125.4), "spans": [{"text": "3"}]},
                                {"bbox": (406.3, 121.2, 420.8, 125.3), "spans": [{"text": "3件/OTS"}]},
                                {"bbox": (436.0, 121.2, 442.2, 125.3), "spans": [{"text": "1天"}]},
                            ],
                        }
                    ]
                }

            def get_drawings(self) -> list[dict]:
                return [
                    {"rect": type("Rect", (), {"x0": 42.8, "y0": 108.9, "x1": 546.0, "y1": 138.4})()},
                ]

        bbox = _compose_page_crop_bbox(
            _Page(),
            chosen={
                "bbox": (42.8, 60.9, 546.0, 156.0),
                "rows_text": [
                    ["95", "吊环衬套-高温耐久试验"],
                    ["96", "防尘罩拔脱", "-", "固定上支撑一端，使用拉力机拉动防尘罩，方向：上支撑 轴向方向,加载速度30mm/min,温度23℃,", "拔脱力≥100N", "DV/PV", "3", "3件/OTS", "1天"],
                    ["63", "EMC"],
                ],
                "row_bboxes": [
                    (54.8, 72.9, 534.0, 108.9),
                    (54.8, 108.9, 534.0, 138.4),
                    (54.8, 138.4, 534.0, 144.0),
                ],
            },
            start_anchor=None,
            end_anchor=None,
            headers=["序号", "项目", "参考标准", "试验方法", "要求", "DV/PV", "责任方", "样件", "周期"],
            slice_rows=[["96", "防尘罩拔脱", "-", "固定上支撑一端，使用拉力机拉动防尘罩，方向：上支撑 轴向方向,加载速度30mm/min,温度23℃,", "拔脱力≥100N", "DV/PV", "3", "3件/OTS", "1天"]],
        )
        self.assertAlmostEqual(bbox[1], 100.9, places=1)
        self.assertAlmostEqual(bbox[3], 146.4, places=1)
        self.assertGreater(bbox[0], 45.0)
        self.assertLess(bbox[2], 470.0)

    def test_compose_page_crop_bbox_ignores_late_generic_start_anchor_below_row_slice(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

        bbox = _compose_page_crop_bbox(
            _Page(),
            chosen={
                "bbox": (42.4, 60.3, 788.8, 477.5),
                "rows_text": [
                    ["整车用电器与线束匹配评估表", "", ""],
                    ["整车应用基本信息", "", ""],
                    ["项目", "", ""],
                    ["布置位置", "前舱发动机本体 乘员舱仪表台"],
                    ["工作温度", "示例：-40-+105°C"],
                    ["防水等级", "示例：S2/S3"],
                ],
                "row_bboxes": [
                    (42.4, 80.0, 788.8, 120.0),
                    (42.4, 180.0, 788.8, 205.0),
                    (42.4, 205.0, 788.8, 220.0),
                    (42.4, 220.3, 788.8, 236.0),
                    (42.4, 236.0, 788.8, 252.0),
                    (42.4, 252.0, 788.8, 269.7),
                ],
            },
            start_anchor={"bbox": (285.5, 535.9, 295.9, 557.0), "text": "项目"},
            end_anchor=None,
            headers=["用电器端基本信息", "说明"],
            slice_rows=[
                ["布置位置", "前舱发动机本体 乘员舱仪表台"],
                ["工作温度", "示例：-40-+105°C"],
                ["防水等级", "示例：S2/S3"],
            ],
        )
        self.assertEqual(bbox, (42.4, 197.0, 600.0, 277.7))

    def test_compose_page_crop_bbox_ignores_stale_page_floor_when_anchor_starts_above_it(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

        bbox = _compose_page_crop_bbox(
            _Page(),
            chosen=None,
            start_anchor={"bbox": (51.2, 387.9, 115.6, 396.8), "text": "3.2.1 产品信息"},
            end_anchor=None,
            headers=[],
            slice_rows=[],
            page_floor=545.4,
        )
        self.assertLess(bbox[1], 400.0)

    def test_compose_page_crop_bbox_applies_page_floor_when_anchor_starts_after_it(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

        bbox = _compose_page_crop_bbox(
            _Page(),
            chosen=None,
            start_anchor={"bbox": (51.2, 560.0, 115.6, 568.8), "text": "4）集成验证要求"},
            end_anchor=None,
            headers=[],
            slice_rows=[],
            page_floor=545.4,
        )
        self.assertGreaterEqual(bbox[1], 551.4)

    def test_anchor_window_row_slice_hint_applies_for_repeated_title_cell(self) -> None:
        self.assertTrue(
            ta._should_use_anchor_window_row_slice_hint(
                {"bbox": (84.4, 456.9, 137.9, 461.8), "text": "耐化学试液（仅防水件）"},
                (42.1, 364.0, 548.8, 560.4),
            )
        )

    def test_anchor_window_row_slice_hint_skips_far_above_candidate_span(self) -> None:
        self.assertFalse(
            ta._should_use_anchor_window_row_slice_hint(
                {"bbox": (55.2, 519.6, 77.1, 525.0), "text": "气候性能"},
                (42.2, 72.1, 545.4, 499.8),
            )
        )

    def test_tighten_anchor_window_crop_bbox_expands_to_last_content_before_next_heading(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        bbox = _tighten_anchor_window_crop_bbox(
            _Rect(),
            current_bbox=(24.0, 326.7, 571.2, 509.7),
            content_bboxes=[
                (51.2, 332.7, 115.6, 341.5),
                (52.2, 363.5, 179.4, 427.1),
                (51.2, 510.6, 375.8, 519.4),
            ],
            start_anchor={"bbox": (51.2, 332.7, 115.6, 341.5), "text": "7.2）环境适应性要求"},
            end_anchor={"bbox": (51.2, 521.7, 110.8, 530.6), "text": "7.3）机械性能要求"},
        )
        self.assertEqual(bbox, (39.2, 332.7, 387.8, 519.7))

    def test_tighten_anchor_window_crop_bbox_keeps_clause_heading_top_tight(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        bbox = _tighten_anchor_window_crop_bbox(
            _Rect(),
            current_bbox=(24.0, 406.3, 571.2, 490.8),
            content_bboxes=[
                (51.2, 414.3, 95.1, 423.1),
                (71.3, 450.0, 520.0, 812.4),
            ],
            start_anchor={"bbox": (51.2, 414.3, 95.1, 423.1), "text": "6.2）功能框图"},
            end_anchor=None,
        )
        self.assertEqual(bbox[1], 414.3)

    def test_collect_anchor_window_content_bboxes_includes_table_drawings(self) -> None:
        class _Rect:
            def __init__(self, x0: float, y0: float, x1: float, y1: float) -> None:
                self.x0 = x0
                self.y0 = y0
                self.x1 = x1
                self.y1 = y1

        class _Page:
            def get_text(self, mode: str) -> dict:
                if mode != "dict":
                    raise AssertionError(mode)
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {
                                    "bbox": (55.2, 500.0, 254.6, 507.6),
                                    "spans": [{"text": "2.主要设计和验证要求"}],
                                }
                            ],
                        }
                    ]
                }

            def get_drawings(self) -> list[dict]:
                return [{"rect": _Rect(54.0, 508.8, 533.52, 517.8)}]

        boxes = ta._collect_anchor_window_content_bboxes(
            _Page(),
            window_top=492.0,
            window_bottom=704.0,
        )

        self.assertEqual(len(boxes), 2)
        self.assertAlmostEqual(max(bbox[2] for bbox in boxes), 533.52)

    def test_collect_anchor_window_content_bboxes_clips_overlapping_image_blocks_to_window(self) -> None:
        class _Page:
            def get_text(self, mode: str) -> dict:
                if mode != "dict":
                    raise AssertionError(mode)
                return {
                    "blocks": [
                        {"type": 1, "bbox": (40.0, 32.0, 560.0, 290.0)},
                        {
                            "type": 0,
                            "lines": [
                                {
                                    "bbox": (51.2, 256.8, 196.7, 265.6),
                                    "spans": [{"text": "8.2.5.3）衬套径向动刚度曲线平滑，动静比≦2.5;"}],
                                }
                            ],
                        },
                    ]
                }

            def get_drawings(self) -> list[dict]:
                return []

        boxes = ta._collect_anchor_window_content_bboxes(
            _Page(),
            window_top=256.0,
            window_bottom=304.0,
        )

        self.assertTrue(boxes)
        self.assertGreaterEqual(min(bbox[1] for bbox in boxes), 256.0)

    def test_prefer_last_before_context_anchor_returns_false_for_heading_only_context(self) -> None:
        self.assertFalse(_prefer_last_before_context_anchor("诊断设计要求"))
        self.assertTrue(_prefer_last_before_context_anchor("产品需满足由OEM组织的系统级和整车级试验要求。"))

    def test_trailing_table_context_noise_detects_compact_directory_band(self) -> None:
        self.assertTrue(
            _looks_like_trailing_table_context_noise(
                "PDS表位置\n内容",
                next_entry_text="附件4--诊断、OTA相关标准",
            )
        )
        self.assertTrue(
            _looks_like_trailing_table_context_noise(
                "#REF!",
                next_entry_text="2.3 特殊特性控制要求",
            )
        )
        self.assertFalse(
            _looks_like_trailing_table_context_noise(
                "工作产出保密性级别 Confidentiality Level",
                next_entry_text="RASIC Definition RASIC 定义",
            )
        )

    def test_promote_anchor_to_compact_band_start_moves_to_band_header(self) -> None:
        class _Document:
            pass

        source_key = "sample"
        promoted = _promote_anchor_to_compact_band_start(
            _Document(),
            source_key=source_key,
            anchor={
                "page_index": 0,
                "bbox": (83.7, 201.3, 166.5, 208.2),
                "score": 20,
                "text": "附件4--诊断、OTA相关标准",
            },
            detection_cache={
                (source_key, 0): [
                    {"text": "PDS表位置", "norm": "pds表位置", "bbox": (97.5, 97.5, 140.0, 104.4)},
                    {"text": "内容", "norm": "内容", "bbox": (200.0, 97.5, 240.0, 104.4)},
                    {"text": "软件刷写签名规范", "norm": "软件刷写签名规范", "bbox": (130.0, 134.3, 220.0, 141.1)},
                    {"text": "刷写规范", "norm": "刷写规范", "bbox": (130.0, 142.7, 180.0, 149.5)},
                    {"text": "诊断规范-UDS规范", "norm": "诊断规范uds规范", "bbox": (130.0, 151.1, 240.0, 157.9)},
                    {"text": "CANCANFD规范", "norm": "cancanfd规范", "bbox": (130.0, 159.5, 220.0, 166.3)},
                    {"text": "DOIP规范", "norm": "doip规范", "bbox": (130.0, 167.9, 200.0, 174.7)},
                    {"text": "诊断路由规范", "norm": "诊断路由规范", "bbox": (130.0, 176.3, 220.0, 183.1)},
                    {"text": "LIN规范", "norm": "lin规范", "bbox": (130.0, 184.7, 180.0, 191.5)},
                    {"text": "附件4--诊断、OTA相关标准", "norm": "附件4诊断ota相关标准", "bbox": (83.7, 201.3, 166.5, 208.2)},
                ]
            },
        )
        self.assertEqual(promoted["bbox"][1], 97.5)

    def test_find_context_anchor_window_does_not_promote_real_local_clause_anchor(self) -> None:
        class _Document:
            pass

        source_key = "sample"
        start_anchor, end_anchor = _find_context_anchor_window(
            _Document(),
            source_key=source_key,
            page_indices=[0],
            before_candidates=[("主要设计和验证要求", 17)],
            after_candidates=[("样件数量要求", 17)],
            self_candidates=[("2主要设计和验证要求", 22)],
            detection_cache={
                (source_key, 0): [
                    {"text": "2.主要设计和验证要求", "norm": "2主要设计和验证要求", "bbox": (50.0, 500.0, 180.0, 510.0)},
                    {"text": "序号", "norm": "序号", "bbox": (90.0, 510.0, 110.0, 518.0)},
                    {"text": "气候性能", "norm": "气候性能", "bbox": (50.0, 520.0, 120.0, 528.0)},
                    {"text": "1", "norm": "1", "bbox": (50.0, 529.0, 56.0, 537.0)},
                    {"text": "低温存储", "norm": "低温存储", "bbox": (90.0, 529.0, 140.0, 537.0)},
                    {"text": "2", "norm": "2", "bbox": (50.0, 538.0, 56.0, 546.0)},
                    {"text": "高温存储", "norm": "高温存储", "bbox": (90.0, 538.0, 140.0, 546.0)},
                    {"text": "寿命耐久", "norm": "寿命耐久", "bbox": (50.0, 580.0, 110.0, 588.0)},
                    {"text": "1", "norm": "1", "bbox": (50.0, 589.0, 56.0, 597.0)},
                    {"text": "高温耐久", "norm": "高温耐久", "bbox": (90.0, 589.0, 140.0, 597.0)},
                    {"text": "化学性能", "norm": "化学性能", "bbox": (50.0, 688.0, 110.0, 696.0)},
                    {"text": "1", "norm": "1", "bbox": (50.0, 697.0, 56.0, 705.0)},
                    {"text": "盐雾腐蚀", "norm": "盐雾腐蚀", "bbox": (90.0, 697.0, 140.0, 705.0)},
                    {"text": "3.样件数量要求：", "norm": "3样件数量要求", "bbox": (50.0, 703.0, 140.0, 713.0)},
                ]
            },
            prefer_last_before=False,
            promote_after_band_start=True,
        )
        self.assertEqual(start_anchor["text"], "2.主要设计和验证要求")
        self.assertEqual(end_anchor["text"], "3.样件数量要求：")

    def test_numbered_dot_heading_counts_as_local_clause_heading(self) -> None:
        self.assertTrue(ta._looks_like_local_clause_heading("3.样件数量要求："))
        self.assertTrue(ta._looks_like_local_clause_heading("4.集成验证要求："))

    def test_entry_anchor_context_text_prefers_neighbor_paragraph_over_hybrid_table_body(self) -> None:
        row = {
            "display_text": "1）油泵性能要求\n|序号|项目|要求|\n|1|总成重量|前＜16.1kg|",
            "content_blocks": [
                {"kind": "paragraph", "text": "1）油泵性能要求"},
                {"kind": "table", "table_id": "tbl-hybrid"},
            ],
            "table_refs": [
                {
                    "table_id": "tbl-hybrid",
                    "table_title": "1）油泵性能要求",
                    "slice_render_mode": "structured",
                    "slice_rows": [["1", "总成重量", "前＜16.1kg"]],
                }
            ],
        }
        self.assertEqual(ta._entry_anchor_context_text(row, from_end=False), "1）油泵性能要求")
        self.assertEqual(ta._entry_anchor_context_text(row, from_end=True), "1）油泵性能要求")

    def test_prefer_prior_generic_compact_band_anchor_uses_top_directory_band(self) -> None:
        class _Document:
            pass

        source_key = "sample"
        promoted = ta._prefer_prior_generic_compact_band_anchor(
            _Document(),
            source_key=source_key,
            anchor={
                "page_index": 0,
                "bbox": (196.6, 134.3, 250.8, 141.1),
                "score": 1,
                "text": "软件刷写签名规范",
            },
            detection_cache={
                (source_key, 0): [
                    {"text": "PDS表位置", "norm": "pds表位置", "bbox": (109.5, 97.5, 140.6, 104.4)},
                    {"text": "内容", "norm": "内容", "bbox": (482.2, 97.5, 495.9, 104.4)},
                    {"text": "软件刷写签名规范", "norm": "软件刷写签名规范", "bbox": (196.6, 134.3, 250.8, 141.1)},
                ]
            },
        )
        self.assertEqual(promoted["text"], "PDS表位置")

    def test_find_next_local_clause_anchor_returns_following_clause_heading(self) -> None:
        class _Document:
            pass

        source_key = "sample"
        anchor = _find_next_local_clause_anchor(
            _Document(),
            source_key=source_key,
            page_indices=[0],
            start_anchor={"page_index": 0, "bbox": (51.2, 332.7, 115.6, 341.5), "text": "7.2）环境适应性要求"},
            detection_cache={
                (source_key, 0): [
                    {"text": "7.2）环境适应性要求", "norm": "72环境适应性要求", "bbox": (51.2, 332.7, 115.6, 341.5)},
                    {"text": "油泵总成在400h盐雾腐蚀测试后能正常工作", "norm": "油泵总成在400h盐雾腐蚀测试后能正常工作", "bbox": (51.2, 510.6, 375.8, 519.4)},
                    {"text": "7.3）机械性能要求", "norm": "73机械性能要求", "bbox": (51.2, 521.7, 110.8, 530.6)},
                ]
            },
        )
        self.assertEqual(anchor, {"page_index": 0, "bbox": (51.2, 521.7, 110.8, 530.6), "score": 1, "text": "7.3）机械性能要求"})

    def test_find_next_local_clause_anchor_ignores_sentence_with_attachment_keyword(self) -> None:
        class _Document:
            pass

        source_key = "sample"
        anchor = _find_next_local_clause_anchor(
            _Document(),
            source_key=source_key,
            page_indices=[0],
            start_anchor={"page_index": 0, "bbox": (55.6, 449.0, 109.4, 458.6), "text": "2.1 特殊特性清单"},
            detection_cache={
                (source_key, 0): [
                    {"text": "2.1 特殊特性清单", "norm": "21特殊特性清单", "bbox": (55.6, 449.0, 109.4, 458.6)},
                    {"text": "本清单中特殊特性是我司基于风险分析的结果，必要时供应商可提供特性的稳健可靠的证据或数据", "norm": "本清单中特殊特性是我司基于风险分析的结果必要时供应商可提供特性的稳健可靠的证据或数据", "bbox": (55.6, 467.5, 429.9, 474.7)},
                    {"text": "2.2 特殊特性分类与标识", "norm": "22特殊特性分类与标识", "bbox": (55.6, 549.3, 131.0, 559.0)},
                ]
            },
        )
        self.assertEqual(anchor, {"page_index": 0, "bbox": (55.6, 549.3, 131.0, 559.0), "score": 1, "text": "2.2 特殊特性分类与标识"})

    def test_find_next_local_clause_anchor_ignores_numbered_sentence_inside_table(self) -> None:
        class _Document:
            pass

        source_key = "sample"
        anchor = _find_next_local_clause_anchor(
            _Document(),
            source_key=source_key,
            page_indices=[0],
            start_anchor={"page_index": 0, "bbox": (223.3, 78.6, 312.2, 93.3), "text": "软件开发要求"},
            detection_cache={
                (source_key, 0): [
                    {"text": "软件开发要求", "norm": "软件开发要求", "bbox": (223.3, 78.6, 312.2, 93.3)},
                    {
                        "text": "1. 取得软件认证ASPICE，CMMI(考察项)，或有软件质量体系搭建计划，保障软件开发过程",
                        "norm": "1取得软件认证aspicecmmi考察项或有软件质量体系搭建计划保障软件开发过程",
                        "bbox": (155.7, 143.7, 477.6, 151.6),
                    },
                    {"text": "软件度量指标", "norm": "软件度量指标", "bbox": (51.2, 690.0, 123.6, 701.4)},
                ]
            },
            table_detection_cache={
                (source_key, 0): [
                    {
                        "bbox": (42.1, 86.1, 494.0, 674.3),
                        "text": "软件开发要求 full table",
                        "area": 265764.0,
                        "rows_text": [],
                        "row_bboxes": [],
                    }
                ]
            },
        )
        self.assertIsNone(anchor)

    def test_dot_style_numbered_sentence_is_not_heading(self) -> None:
        self.assertFalse(
            ta._looks_like_local_clause_heading(
                "1. 取得软件认证ASPICE，CMMI(考察项)，或有软件质量体系搭建计划，保障软件开发过程"
            )
        )

    def test_candidate_row_section_number_accepts_optional_label_suffix(self) -> None:
        self.assertEqual(ta._candidate_row_section_number(["4（可选）", "对插到位声响", "USCAR-2 Rev 6 5.4.7"]), "4")

    def test_best_context_anchor_prefers_left_aligned_heading_when_score_close(self) -> None:
        candidates = _build_context_anchor_fragments("连接器防错结构 Polarization Feature Effectiveness", from_end=False)
        anchor = ta._best_context_anchor_in_page(
            [
                {"text": "连接器防错结构", "norm": "连接器防错结构", "bbox": (255.3, 320.3, 289.4, 325.2)},
                {"text": "连接器防错结构", "norm": "连接器防错结构", "bbox": (84.5, 326.3, 118.5, 331.2)},
                {"text": "Polarization Feature Effectiveness", "norm": "polarizationfeatureeffectiveness", "bbox": (84.5, 332.3, 167.8, 337.2)},
            ],
            candidates,
            min_y=226.6,
            prefer_last=False,
        )
        self.assertEqual(anchor, {"text": "连接器防错结构", "bbox": (84.5, 326.3, 118.5, 331.2), "score": 21})

    @unittest.skipIf(_OIL_PUMP_SAMPLE_PDF is None, "oil pump sample pdf not available")
    def test_snapshot_before_heading_anchor_prefers_current_sibling_heading(self) -> None:
        source = _OIL_PUMP_SAMPLE_PDF
        assert source is not None
        with ta.fitz.open(str(source)) as pdf:
            start_anchor, _ = _find_context_anchor_window(
                pdf,
                source_key=str(source.resolve()),
                page_indices=[14],
                before_candidates=_build_context_anchor_fragments("诊断设计要求", from_end=True),
                after_candidates=_build_context_anchor_fragments("智能诊断设计要求", from_end=False),
                self_candidates=_build_current_entry_anchor_fragments(
                    table_text="诊断开发设计需遵循UDS诊断相关规范要求：\n（1）部件诊断服务应遵循《UDS诊断规范》要求；",
                    table_title="诊断开发设计需遵循UDS诊断相关规范要求：",
                    before_context_text="诊断设计要求",
                ),
                detection_cache={},
                prefer_last_before=_prefer_last_before_context_anchor("诊断设计要求"),
                promote_after_band_start=False,
            )
        self.assertIsNotNone(start_anchor)
        self.assertIn("诊断设计要求", str(start_anchor.get("text") if start_anchor else ""))
        self.assertNotIn("3.2", str(start_anchor.get("text") if start_anchor else ""))

    @unittest.skipIf(_OIL_PUMP_SAMPLE_PDF is None, "oil pump sample pdf not available")
    def test_snapshot_crop_skips_attachment_directory_page_when_next_heading_starts_band(self) -> None:
        source = _OIL_PUMP_SAMPLE_PDF
        assert source is not None
        with ta.fitz.open(str(source)) as pdf:
            table_text = (
                "产品需满足由OEM组织的系统级和整车级试验要求，系统级试验由SE或项目组定义；整车级试验包括但不限于三高一湿适应性试验、环境可靠性试验、社会道路试验。\n"
                "序号测试类别测试小类测试项目\n1 产品SE或者项目组定义"
            )
            start_anchor, end_anchor = _find_context_anchor_window(
                pdf,
                source_key=str(source.resolve()),
                page_indices=[12, 13, 14],
                before_candidates=_build_context_anchor_fragments("集成验证要求", from_end=True),
                after_candidates=_build_context_anchor_fragments("附件4--诊断、OTA相关标准", from_end=False),
                self_candidates=_build_current_entry_anchor_fragments(
                    table_text=table_text,
                    table_title="产品需满足由OEM组织的系统级和整车级试验要求",
                    before_context_text="集成验证要求",
                ),
                detection_cache={},
                prefer_last_before=_prefer_last_before_context_anchor("集成验证要求"),
                promote_after_band_start=True,
            )
            plan = _build_pdf_crop_plan(
                pdf,
                source_key=str(source.resolve()),
                page_indices=[12, 13, 14],
                match_fragments=_build_table_match_fragments(
                    table_text=table_text,
                    table_title="产品需满足由OEM组织的系统级和整车级试验要求",
                    headers=[],
                    rows=[],
                    notes=[],
                ),
                headers=[],
                slice_rows=[],
                start_anchor=start_anchor,
                end_anchor=end_anchor,
                detection_cache={},
                source_page_floor_offsets={},
                prefer_anchor_window=True,
            )
        self.assertEqual([page for page, _ in plan], [12, 13])

    def test_build_pdf_crop_plan_skips_end_page_with_only_weak_note_match(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

        class _Document:
            def load_page(self, _page_index: int) -> _Page:
                return _Page()

        source_key = "sample"
        plan = _build_pdf_crop_plan(
            _Document(),
            source_key=source_key,
            page_indices=[0, 1],
            match_fragments=[("needle", 5), ("note", 1)],
            headers=["序号", "项目", "要求"],
            slice_rows=[["1", "尺寸", "≤10"]],
            start_anchor={"page_index": 0, "bbox": (50.0, 80.0, 120.0, 92.0), "text": "附件条目"},
            end_anchor={"page_index": 1, "bbox": (52.0, 84.0, 160.0, 96.0), "text": "工作产出保密性级别"},
            detection_cache={
                (source_key, 0): [
                    {
                        "bbox": (10.0, 100.0, 300.0, 280.0),
                        "text": "needle main table",
                        "area": 52200.0,
                        "rows_text": [["序号", "项目", "要求"], ["1", "尺寸", "≤10"]],
                        "row_bboxes": [(10.0, 100.0, 300.0, 120.0), (10.0, 120.0, 300.0, 140.0)],
                    }
                ],
                (source_key, 1): [
                    {
                        "bbox": (10.0, 68.0, 300.0, 220.0),
                        "text": "note heading only",
                        "area": 44080.0,
                        "rows_text": [["工作产出保密性级别", "说明"], ["公开", "共享"]],
                        "row_bboxes": [(10.0, 68.0, 300.0, 88.0), (10.0, 88.0, 300.0, 108.0)],
                    }
                ],
            },
            source_page_floor_offsets={},
            prefer_anchor_window=False,
        )
        self.assertEqual([page_index for page_index, _ in plan], [0])

    def test_build_pdf_crop_plan_snapshot_end_anchor_ignores_table_candidate_cache(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

            def get_text(self, _mode: str) -> dict:
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {"spans": [{"text": "7.2）环境适应性要求"}], "bbox": (51.2, 332.7, 115.6, 341.5)},
                                {"spans": [{"text": "油泵总成在400h盐雾腐蚀测试后能正常工作"}], "bbox": (51.2, 510.6, 375.8, 519.4)},
                                {"spans": [{"text": "7.3）机械性能要求"}], "bbox": (51.2, 521.7, 110.8, 530.6)},
                            ],
                        }
                    ]
                }

        class _Document:
            def load_page(self, _page_index: int) -> _Page:
                return _Page()

        source_key = "sample"
        plan = _build_pdf_crop_plan(
            _Document(),
            source_key=source_key,
            page_indices=[0],
            match_fragments=[],
            headers=[],
            slice_rows=[],
            start_anchor={"page_index": 0, "bbox": (51.2, 332.7, 115.6, 341.5), "text": "7.2）环境适应性要求"},
            end_anchor=None,
            detection_cache={
                (source_key, 0): [
                    {
                        "bbox": (10.0, 60.0, 300.0, 220.0),
                        "text": "stale table candidate cache",
                        "area": 46400.0,
                        "rows_text": [],
                        "row_bboxes": [],
                    }
                ]
            },
            source_page_floor_offsets={},
            prefer_anchor_window=True,
        )
        self.assertEqual(plan, [(0, (39.2, 332.7, 387.8, 519.7))])

    def test_build_pdf_crop_plan_snapshot_prefers_next_clause_heading_over_late_content_anchor(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

            def get_text(self, _mode: str) -> dict:
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {"spans": [{"text": "7.2）环境适应性要求"}], "bbox": (51.2, 332.7, 115.6, 341.5)},
                                {"spans": [{"text": "油泵总成在400h盐雾腐蚀测试后能正常工作"}], "bbox": (51.2, 510.6, 375.8, 519.4)},
                                {"spans": [{"text": "7.3）机械性能要求"}], "bbox": (51.2, 521.7, 110.8, 530.6)},
                                {"spans": [{"text": "油泵总成在随机振动和扫频振动测试后能正常工作"}], "bbox": (51.2, 532.9, 332.2, 541.7)},
                            ],
                        }
                    ]
                }

        class _Document:
            def load_page(self, _page_index: int) -> _Page:
                return _Page()

        plan = _build_pdf_crop_plan(
            _Document(),
            source_key="sample",
            page_indices=[0],
            match_fragments=[],
            headers=[],
            slice_rows=[],
            start_anchor={"page_index": 0, "bbox": (51.2, 332.7, 115.6, 341.5), "text": "7.2）环境适应性要求"},
            end_anchor={"page_index": 0, "bbox": (51.2, 532.9, 332.2, 541.7), "text": "油泵总成在随机振动和扫频振动测试后能正常工作"},
            detection_cache={},
            source_page_floor_offsets={},
            prefer_anchor_window=True,
        )
        self.assertEqual(plan, [(0, (39.2, 332.7, 387.8, 519.7))])

    def test_build_pdf_crop_plan_tightens_contaminated_single_row_table_to_anchor_window(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

            def get_text(self, _mode: str) -> dict:
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {"spans": [{"text": "3.1 车型信息"}], "bbox": (51.2, 118.0, 115.6, 128.0)},
                                {"spans": [{"text": "项目代号 车型代号 手工件到样时间"}], "bbox": (51.2, 134.0, 360.0, 144.0)},
                                {"spans": [{"text": "X2548/X2549 X8D11/X8D01 2026.03.30"}], "bbox": (51.2, 146.0, 360.0, 156.0)},
                                {"spans": [{"text": "3.2 产品技术要求及说明"}], "bbox": (51.2, 170.0, 190.0, 180.0)},
                            ],
                        }
                    ]
                }

            def get_drawings(self) -> list[dict]:
                return []

        class _Document:
            def load_page(self, _page_index: int) -> _Page:
                return _Page()

        plan = _build_pdf_crop_plan(
            _Document(),
            source_key="sample",
            page_indices=[0],
            match_fragments=[("车型信息", 14)],
            headers=["项目代号", "车型代号", "手工件到样时间"],
            slice_rows=[["X2548/X2549", "X8D11/X8D01", "2026.03.30"]],
            start_anchor={"page_index": 0, "bbox": (51.2, 118.0, 115.6, 128.0), "text": "3.1 车型信息"},
            end_anchor={"page_index": 0, "bbox": (51.2, 170.0, 190.0, 180.0), "text": "3.2 产品技术要求及说明"},
            detection_cache={
                ("sample", 0): [
                    {
                        "bbox": (10.0, 80.0, 300.0, 400.0),
                        "text": "车型信息 main table",
                        "area": 92800.0,
                        "rows_text": [
                            ["3.1 车型信息"],
                            ["项目代号", "车型代号", "手工件到样时间"],
                            ["X2548/X2549 3.2 产品技术要求及说明", "X8D11/X8D01", "2026.03.30"],
                        ],
                        "row_bboxes": [
                            (10.0, 100.0, 300.0, 120.0),
                            (10.0, 120.0, 300.0, 140.0),
                            (10.0, 140.0, 300.0, 180.0),
                        ],
                    }
                ]
            },
            source_page_floor_offsets={},
            prefer_anchor_window=False,
        )

        self.assertEqual(len(plan), 1)
        self.assertLess(plan[0][1][3], 170.0)
        self.assertLessEqual(plan[0][1][1], 118.0)

    def test_build_pdf_crop_plan_ignores_next_entry_anchor_inside_wide_current_table(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

        class _Document:
            def load_page(self, _page_index: int) -> _Page:
                return _Page()

        plan = _build_pdf_crop_plan(
            _Document(),
            source_key="sample",
            page_indices=[0],
            match_fragments=[("测试分组", 18), ("testgroups", 14)],
            headers=["测试项目", "组1", "组2", "组3", "组4", "组5"],
            slice_rows=[
                ["样品预处理", "1", "1", "1", "1", "1"],
                ["外观及尺寸", "2,4", "2,5", "", "", "2,10"],
                ["连接器插拔", "", "3", "", "", "3"],
                ["电路连续性", "", "", "", "", "5"],
                ["绝缘电阻", "", "", "8", "", ""],
                ["高压水喷射", "", "", "", "", "13"],
            ],
            start_anchor={"page_index": 0, "bbox": (296.8, 75.3, 401.3, 82.1), "text": "测试分组 / Test Groups（推荐）"},
            end_anchor={"page_index": 0, "bbox": (55.4, 169.2, 143.7, 176.1), "text": "端子至端子的插入力和拔出力"},
            detection_cache={
                ("sample", 0): [
                    {
                        "bbox": (42.0, 60.0, 548.1, 451.9),
                        "text": "测试分组 full page",
                        "area": 198391.0,
                        "rows_text": [],
                        "row_bboxes": [],
                    }
                ]
            },
            source_page_floor_offsets={},
            prefer_anchor_window=False,
        )
        self.assertEqual(len(plan), 1)
        self.assertGreater(plan[0][1][3], 430.0)

    def test_build_pdf_crop_plan_ignores_immediate_next_numbered_anchor_for_clean_single_row_table(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

            def get_text(self, _mode: str) -> dict:
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {"spans": [{"text": "96 防尘罩拔脱"}], "bbox": (54.8, 121.1, 118.0, 125.4)},
                                {"spans": [{"text": "63 EMC"}], "bbox": (54.8, 138.4, 96.0, 144.0)},
                            ],
                        }
                    ]
                }

            def get_drawings(self) -> list[dict]:
                return [
                    {"rect": type("Rect", (), {"x0": 42.8, "y0": 108.9, "x1": 546.0, "y1": 138.4})()},
                ]

        class _Document:
            def load_page(self, _page_index: int) -> _Page:
                return _Page()

        plan = _build_pdf_crop_plan(
            _Document(),
            source_key="sample",
            page_indices=[0],
            match_fragments=[("防尘罩拔脱", 18)],
            headers=["序号", "项目", "参考标准", "试验方法", "要求", "DV/PV", "责任方", "样件", "周期"],
            slice_rows=[
                [
                    "96",
                    "防尘罩拔脱",
                    "-",
                    "固定上支撑一端，使用拉力机拉动防尘罩，方向：上支撑 轴向方向,加载速度30mm/min,温度23℃,",
                    "拔脱力≥100N",
                    "DV/PV",
                    "3",
                    "3件/OTS",
                    "1天",
                ]
            ],
            start_anchor=None,
            end_anchor=None,
            detection_cache={
                ("sample", 0): [
                    {
                        "bbox": (42.8, 60.9, 546.0, 156.0),
                        "text": "主动后减振器试验大纲",
                        "area": 47913.51,
                        "rows_text": [
                            ["95", "吊环衬套-高温耐久试验"],
                            [
                                "96",
                                "防尘罩拔脱",
                                "-",
                                "固定上支撑一端，使用拉力机拉动防尘罩，方向：上支撑 轴向方向,加载速度30mm/min,温度23℃,",
                                "拔脱力≥100N",
                                "DV/PV",
                                "3",
                                "3件/OTS",
                                "1天",
                            ],
                            ["63", "EMC"],
                        ],
                        "row_bboxes": [
                            (54.8, 72.9, 534.0, 108.9),
                            (54.8, 108.9, 534.0, 138.4),
                            (54.8, 138.4, 534.0, 144.0),
                        ],
                    }
                ]
            },
            source_page_floor_offsets={},
            prefer_anchor_window=False,
        )

        self.assertEqual(len(plan), 1)
        self.assertAlmostEqual(plan[0][1][1], 100.9, places=1)
        self.assertAlmostEqual(plan[0][1][3], 146.4, places=1)
        self.assertGreater(plan[0][1][3] - plan[0][1][1], 40.0)

    def test_build_pdf_crop_plan_keeps_large_row_slice_when_inline_numbered_sentence_exists(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

            def get_text(self, _mode: str) -> dict:
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {"spans": [{"text": "软件开发要求"}], "bbox": (223.3, 78.6, 312.2, 93.3)},
                                {
                                    "spans": [{"text": "1. 组织与甲方公司工程师开展测试用例的评审"}],
                                    "bbox": (155.7, 290.8, 477.6, 298.7),
                                },
                            ],
                        }
                    ]
                }

            def get_drawings(self) -> list[dict]:
                return []

        class _Document:
            def load_page(self, _page_index: int) -> _Page:
                return _Page()

        plan = _build_pdf_crop_plan(
            _Document(),
            source_key="sample",
            page_indices=[0],
            match_fragments=[("软件开发要求", 14), ("se00001", 8)],
            headers=["条款编号", "要求项目", "要求描述"],
            slice_rows=[
                ["SE00.001", "供应商软件质量资质评估", "1. 取得软件认证ASPICE"],
                ["SE00.002", "有软件开发流程按照ASPICE管控", "1. 组织与甲方公司工程师开展测试用例的评审"],
                ["SE00.003", "对阶段软件度量指标管理与评审", "1. 满足质量目标"],
                ["SE00.004", "测试用例联合评审", "1. 测试用例满足需求"],
                ["SE00.005", "人力资源评估", "1. 配合开展人力资源满足度评价"],
                ["SE00.006", "需求计划评审", "1. 根据需求开发规范制定需求管控计划"],
            ],
            start_anchor={"page_index": 0, "bbox": (223.3, 78.6, 312.2, 93.3), "text": "软件开发要求"},
            end_anchor=None,
            detection_cache={
                ("sample", 0): [
                    {
                        "bbox": (42.1, 86.1, 494.0, 674.3),
                        "text": "软件开发要求 full table",
                        "area": 265764.0,
                        "rows_text": [
                            ["条款编号", "要求项目", "要求描述"],
                            ["SE00.001", "供应商软件质量资质评估", "1. 取得软件认证ASPICE"],
                            ["SE00.002", "有软件开发流程按照ASPICE管控", "1. 组织与甲方公司工程师开展测试用例的评审"],
                            ["SE00.003", "对阶段软件度量指标管理与评审", "1. 满足质量目标"],
                            ["SE00.004", "测试用例联合评审", "1. 测试用例满足需求"],
                            ["SE00.005", "人力资源评估", "1. 配合开展人力资源满足度评价"],
                            ["SE00.006", "需求计划评审", "1. 根据需求开发规范制定需求管控计划"],
                        ],
                        "row_bboxes": [
                            (42.1, 98.1, 494.0, 140.0),
                            (42.1, 140.0, 494.0, 220.0),
                            (42.1, 220.0, 494.0, 300.0),
                            (42.1, 300.0, 494.0, 380.0),
                            (42.1, 380.0, 494.0, 460.0),
                            (42.1, 460.0, 494.0, 540.0),
                            (42.1, 540.0, 494.0, 662.3),
                        ],
                    }
                ]
            },
            source_page_floor_offsets={},
            prefer_anchor_window=False,
        )

        self.assertEqual(len(plan), 1)
        self.assertLessEqual(plan[0][1][1], 100.0)
        self.assertGreaterEqual(plan[0][1][3], 660.0)

    def test_build_pdf_crop_plan_preserves_numbered_section_row_slice_for_long_table(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

            def get_text(self, _mode: str) -> dict:
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {"spans": [{"text": "11 温湿度循环(电性能) Temperature and Humidity Cycling"}], "bbox": (42.0, 72.0, 320.0, 90.0)},
                                {"spans": [{"text": "连接器或端子插拔 Connector and/or Terminal Cycling"}], "bbox": (160.0, 102.0, 360.0, 116.0)},
                                {"spans": [{"text": "绝缘电阻 Insulation Resistance"}], "bbox": (160.0, 182.0, 300.0, 196.0)},
                                {"spans": [{"text": "12 温湿度循环(仅防水件) Temperature and Humidity Cycling"}], "bbox": (42.0, 232.0, 334.0, 250.0)},
                                {"spans": [{"text": "连接器或端子插拔 Connector and/or Terminal Cycling"}], "bbox": (160.0, 260.0, 360.0, 274.0)},
                            ],
                        }
                    ]
                }

            def get_drawings(self) -> list[dict]:
                return []

        class _Document:
            def load_page(self, _page_index: int) -> _Page:
                return _Page()

        plan = _build_pdf_crop_plan(
            _Document(),
            source_key="sample",
            page_indices=[0],
            match_fragments=[("温湿度循环", 18), ("temperatureandhumiditycycling", 12)],
            headers=["序号", "项目", "测试顺序"],
            slice_rows=[
                ["11", "温湿度循环(电性能) Temperature and Humidity Cycling", "连接器或端子插拔 Connector and/or Terminal Cycling"],
                ["11", "温湿度循环(电性能) Temperature and Humidity Cycling", "绝缘电阻 Insulation Resistance"],
            ],
            start_anchor=None,
            end_anchor=None,
            detection_cache={
                ("sample", 0): [
                    {
                        "bbox": (42.0, 60.0, 548.0, 780.0),
                        "text": "trial outline long table",
                        "area": 364320.0,
                        "rows_text": [
                            ["11", "温湿度循环(电性能) Temperature and Humidity Cycling", "外观 Visual Inspection"],
                            [None, None, "连接器或端子插拔 Connector and/or Terminal Cycling"],
                            [None, None, "绝缘电阻 Insulation Resistance"],
                            ["12", "温湿度循环(仅防水件) Temperature and Humidity Cycling", "外观 Visual Inspection"],
                            [None, None, "连接器或端子插拔 Connector and/or Terminal Cycling"],
                        ],
                        "row_bboxes": [
                            (42.0, 70.0, 548.0, 110.0),
                            (42.0, 110.0, 548.0, 180.0),
                            (42.0, 180.0, 548.0, 240.0),
                            (42.0, 240.0, 548.0, 320.0),
                            (42.0, 320.0, 548.0, 380.0),
                        ],
                    }
                ]
            },
            source_page_floor_offsets={},
            line_detection_cache={},
            prefer_anchor_window=False,
        )

        self.assertEqual(len(plan), 1)
        self.assertGreaterEqual(plan[0][1][1], 109.0)
        self.assertLessEqual(plan[0][1][1], 111.0)
        self.assertGreaterEqual(plan[0][1][3], 239.0)
        self.assertLessEqual(plan[0][1][3], 241.0)

    def test_build_pdf_crop_plan_preserves_second_numbered_section_row_slice_for_long_table(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

            def get_text(self, _mode: str) -> dict:
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {"spans": [{"text": "11 温湿度循环(电性能) Temperature and Humidity Cycling"}], "bbox": (42.0, 72.0, 320.0, 90.0)},
                                {"spans": [{"text": "12 温湿度循环(仅防水件) Temperature and Humidity Cycling"}], "bbox": (42.0, 232.0, 334.0, 250.0)},
                            ],
                        }
                    ]
                }

            def get_drawings(self) -> list[dict]:
                return []

        class _Document:
            def load_page(self, _page_index: int) -> _Page:
                return _Page()

        plan = _build_pdf_crop_plan(
            _Document(),
            source_key="sample",
            page_indices=[0],
            match_fragments=[("温湿度循环", 18), ("temperatureandhumiditycycling", 12)],
            headers=["序号", "项目", "测试顺序"],
            slice_rows=[
                ["12", "温湿度循环(仅防水件) Temperature and Humidity Cycling", "连接器或端子插拔 Connector and/or Terminal Cycling"],
                ["12", "温湿度循环(仅防水件) Temperature and Humidity Cycling", "绝缘电阻 Insulation Resistance"],
            ],
            start_anchor=None,
            end_anchor=None,
            detection_cache={
                ("sample", 0): [
                    {
                        "bbox": (42.0, 60.0, 548.0, 780.0),
                        "text": "trial outline long table",
                        "area": 364320.0,
                        "rows_text": [
                            ["11", "温湿度循环(电性能) Temperature and Humidity Cycling", "外观 Visual Inspection"],
                            [None, None, "连接器或端子插拔 Connector and/or Terminal Cycling"],
                            [None, None, "绝缘电阻 Insulation Resistance"],
                            ["12", "温湿度循环(仅防水件) Temperature and Humidity Cycling", "外观 Visual Inspection"],
                            [None, None, "连接器或端子插拔 Connector and/or Terminal Cycling"],
                            [None, None, "绝缘电阻 Insulation Resistance"],
                        ],
                        "row_bboxes": [
                            (42.0, 70.0, 548.0, 110.0),
                            (42.0, 110.0, 548.0, 180.0),
                            (42.0, 180.0, 548.0, 240.0),
                            (42.0, 240.0, 548.0, 320.0),
                            (42.0, 320.0, 548.0, 380.0),
                            (42.0, 380.0, 548.0, 440.0),
                        ],
                    }
                ]
            },
            source_page_floor_offsets={},
            line_detection_cache={},
            prefer_anchor_window=False,
        )

        self.assertEqual(len(plan), 1)
        self.assertGreaterEqual(plan[0][1][1], 319.0)
        self.assertLessEqual(plan[0][1][1], 321.0)
        self.assertGreaterEqual(plan[0][1][3], 439.0)
        self.assertLessEqual(plan[0][1][3], 441.0)

    def test_match_row_slice_bbox_prefers_continuation_segment_before_next_numbered_section(self) -> None:
        candidate = {
            "bbox": (42.0, 60.0, 548.0, 572.0),
            "rows_text": [
                [None, None, "高压水喷射 / High Pressure Spray", "USCAR-2 Rev 6 5.6.7", "测试说明", "测试条件", "PV", "10", "C Sample"],
                [None, None, "绝缘电阻 Insulation Resistance", "USCAR-2 Rev 6 5.5.1", "端子与端子之间", "环境前后", "PV", "10", "C Sample"],
                [None, None, "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤,产品无进水", "PV", "10", "C Sample"],
                ["16", "盐雾试验 Salt Spray Test", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤", "PV", "5", "C Sample"],
                [None, None, "连接器或端子插拔 Connector and/or Terminal Cycling", "USCAR-2 Rev 6 5.1.7", "10次插拔", "连接器无变形", "PV", "5", "C Sample"],
                ["17", "耐化学试液（仅防水件） Fluid Resistance", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤", "PV", "13", "C Sample"],
            ],
            "row_bboxes": [
                (54.0, 72.0, 536.0, 140.0),
                (254.0, 140.0, 536.0, 174.0),
                (254.0, 174.0, 536.0, 192.0),
                (54.0, 192.0, 536.0, 364.0),
                (254.0, 210.0, 536.0, 244.0),
                (54.0, 364.0, 536.0, 560.0),
            ],
        }

        bbox = ta._match_row_slice_bbox(
            candidate,
            headers=["15", "高温寿命(仅防水件) High Temperature Exposure", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤", "PV", "10", "C Sample"],
            slice_rows=[
                ["15", "高温寿命(仅防水件) High Temperature Exposure", "高压水喷射 / High Pressure Spray", "USCAR-2 Rev 6 5.6.7", "测试说明", "测试条件", "PV", "10", "C Sample"],
                ["15", "高温寿命(仅防水件) High Temperature Exposure", "绝缘电阻 Insulation Resistance", "USCAR-2 Rev 6 5.5.1", "端子与端子之间", "环境前后", "PV", "10", "C Sample"],
                ["15", "高温寿命(仅防水件) High Temperature Exposure", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤,产品无进水", "PV", "10", "C Sample"],
            ],
        )

        self.assertEqual(bbox, (42.0, 72.0, 548.0, 192.0))

    def test_build_pdf_crop_plan_trims_numbered_continuation_page_before_next_section(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

            def get_text(self, _mode: str) -> dict:
                return {"blocks": []}

            def get_drawings(self) -> list[dict]:
                return []

        class _Document:
            def load_page(self, _page_index: int) -> _Page:
                return _Page()

        plan = _build_pdf_crop_plan(
            _Document(),
            source_key="sample",
            page_indices=[0],
            match_fragments=[("高温寿命", 18), ("hightemperatureexposure", 12)],
            headers=["15", "高温寿命(仅防水件) High Temperature Exposure", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤", "PV", "10", "C Sample"],
            slice_rows=[
                ["15", "高温寿命(仅防水件) High Temperature Exposure", "高压水喷射 / High Pressure Spray", "USCAR-2 Rev 6 5.6.7", "测试说明", "测试条件", "PV", "10", "C Sample"],
                ["15", "高温寿命(仅防水件) High Temperature Exposure", "绝缘电阻 Insulation Resistance", "USCAR-2 Rev 6 5.5.1", "端子与端子之间", "环境前后", "PV", "10", "C Sample"],
                ["15", "高温寿命(仅防水件) High Temperature Exposure", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤,产品无进水", "PV", "10", "C Sample"],
            ],
            start_anchor=None,
            end_anchor=None,
            detection_cache={
                ("sample", 0): [
                    {
                        "bbox": (42.0, 60.0, 548.0, 572.0),
                        "text": "trial outline continuation page",
                        "area": 259072.0,
                        "rows_text": [
                            [None, None, "高压水喷射 / High Pressure Spray", "USCAR-2 Rev 6 5.6.7", "测试说明", "测试条件", "PV", "10", "C Sample"],
                            [None, None, "绝缘电阻 Insulation Resistance", "USCAR-2 Rev 6 5.5.1", "端子与端子之间", "环境前后", "PV", "10", "C Sample"],
                            [None, None, "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤,产品无进水", "PV", "10", "C Sample"],
                            ["16", "盐雾试验 Salt Spray Test", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤", "PV", "5", "C Sample"],
                            [None, None, "连接器或端子插拔 Connector and/or Terminal Cycling", "USCAR-2 Rev 6 5.1.7", "10次插拔", "连接器无变形", "PV", "5", "C Sample"],
                            ["17", "耐化学试液（仅防水件） Fluid Resistance", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤", "PV", "13", "C Sample"],
                        ],
                        "row_bboxes": [
                            (54.0, 72.0, 536.0, 140.0),
                            (254.0, 140.0, 536.0, 174.0),
                            (254.0, 174.0, 536.0, 192.0),
                            (54.0, 192.0, 536.0, 364.0),
                            (254.0, 210.0, 536.0, 244.0),
                            (54.0, 364.0, 536.0, 560.0),
                        ],
                    }
                ]
            },
            source_page_floor_offsets={},
            line_detection_cache={},
            prefer_anchor_window=False,
        )

        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][1], (42.0, 72.0, 548.0, 192.0))

    def test_build_pdf_crop_plan_does_not_pad_upward_into_previous_numbered_section(self) -> None:
        class _Rect:
            width = 600.0
            height = 900.0

        class _Page:
            rect = _Rect()

            def get_text(self, _mode: str) -> dict:
                return {"blocks": []}

            def get_drawings(self) -> list[dict]:
                return []

        class _Document:
            def load_page(self, _page_index: int) -> _Page:
                return _Page()

        plan = _build_pdf_crop_plan(
            _Document(),
            source_key="sample",
            page_indices=[0],
            match_fragments=[("高温寿命", 18), ("hightemperatureexposure", 12)],
            headers=["15", "高温寿命(仅防水件) High Temperature Exposure", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤", "PV", "10", "C Sample"],
            slice_rows=[
                ["15", "高温寿命(仅防水件) High Temperature Exposure", "连接器或端子插拔 Connector and/or Terminal Cycling", "USCAR-2 Rev 6 5.1.7", "完成一对连接器或端子的10次插拔；", "连接器无变形等外观损伤", "PV", "10", "C Sample"],
                ["15", "高温寿命(仅防水件) High Temperature Exposure", "绝缘电阻 Insulation Resistance", "USCAR-2 Rev 6 5.5.1", "端子与端子之间,端子与塑壳之间", "1）环境前后", "PV", "10", "C Sample"],
            ],
            start_anchor=None,
            end_anchor=None,
            detection_cache={
                ("sample", 0): [
                    {
                        "bbox": (42.0, 60.0, 548.0, 780.0),
                        "text": "numbered section with previous sibling",
                        "area": 364320.0,
                        "rows_text": [
                            ["14", "高温寿命(电性能) High Temperature Exposure", "外观 Visual Inspection"],
                            [None, None, "连接器或端子插拔 Connector and/or Terminal Cycling"],
                            [None, None, "绝缘电阻 Insulation Resistance"],
                            ["15", "高温寿命(仅防水件) High Temperature Exposure", "外观 Visual Inspection"],
                            [None, None, "连接器或端子插拔 Connector and/or Terminal Cycling"],
                            [None, None, "绝缘电阻 Insulation Resistance"],
                        ],
                        "row_bboxes": [
                            (42.0, 70.0, 548.0, 220.0),
                            (42.0, 110.0, 548.0, 180.0),
                            (42.0, 180.0, 548.0, 220.0),
                            (42.0, 320.0, 548.0, 400.0),
                            (42.0, 400.0, 548.0, 470.0),
                            (42.0, 470.0, 548.0, 540.0),
                        ],
                    }
                ]
            },
            source_page_floor_offsets={},
            line_detection_cache={},
            prefer_anchor_window=False,
        )

        self.assertEqual(len(plan), 1)
        self.assertGreaterEqual(plan[0][1][1], 320.0)
        self.assertLessEqual(plan[0][1][1], 320.5)

    def test_visual_snapshot_heuristic_accepts_image_or_diagram_like_clause(self) -> None:
        text = "\n".join(
            [
                "6.6.2）上下电流程（前控制器和后控制器响应VIU的高压上下电和使能指令）",
                "通讯基本原理如下图；",
            ]
        )
        self.assertTrue(_should_generate_visual_snapshot(text))

    def test_visual_snapshot_heuristic_accepts_environment_clause_with_embedded_visual_band(self) -> None:
        text = "\n".join(
            [
                "7.2）环境适应性要求",
                "油泵总成工作环境温度：-40℃~120℃；总成在温度梯度、温度循环、温度冲击测试中与测试后，均能正常工作。",
                "油泵总成要求整车等级IP68 & IP6K9K。",
            ]
        )
        self.assertTrue(_should_generate_visual_snapshot(text))

    def test_visual_snapshot_heuristic_accepts_attachment_table_page_by_title(self) -> None:
        text = (
            "PDS表位置内容 软件刷写签名规范 刷写规范 诊断规范-UDS规范 CANCANFD规范 "
            "DOIP规范 诊断路由规范 LIN规范 27服务认证设计规范 OTA开发技术要求 "
            "OTA技术调查表 OTA解决方案设计规范"
        )
        self.assertTrue(
            _should_generate_visual_snapshot(
                text,
                title="附件4--诊断、OTA相关标准接入认证设计规范",
            )
        )

    def test_visual_snapshot_heuristic_rejects_short_plain_text_clause(self) -> None:
        text = "\n".join(
            [
                "11.2）明确所选用标准件的规格，以及合理设定打紧力矩；",
                "供应商需确认。",
            ]
        )
        self.assertFalse(_should_generate_visual_snapshot(text))

    def test_context_anchor_fragments_keep_local_clause_prefix_for_long_heading(self) -> None:
        candidates = _build_context_anchor_fragments(
            "4）外观要求 —阳极氧化外观要求（若无阳极氧化可忽略）：",
            from_end=False,
        )
        self.assertIn(("4外观要求", 20), candidates)

    def test_context_anchor_fragments_split_mixed_bilingual_heading(self) -> None:
        candidates = _build_context_anchor_fragments(
            "工作产出保密性级别 Confidentiality Level",
            from_end=False,
        )
        self.assertIn(("工作产出保密性级别", 17), candidates)
        self.assertIn(("confidentialitylevel", 17), candidates)

    def test_context_anchor_fragments_skip_reference_only_tail_for_before_context(self) -> None:
        candidates = _build_context_anchor_fragments(
            "RASIC Definition RASIC 定义\nRef： ISO 21434 Annex C",
            from_end=True,
        )
        self.assertFalse(any(fragment == "refiso21434annexc" for fragment, _ in candidates))

    def test_context_anchor_fragments_keep_plain_numbered_heading_specific(self) -> None:
        candidates = _build_context_anchor_fragments(
            "46 上支撑—橡胶衬套硬度",
            from_end=True,
        )
        fragments = {fragment for fragment, _ in candidates}
        self.assertIn("46上支撑橡胶衬套硬度", fragments)
        self.assertNotIn("上支撑橡胶衬套硬度", fragments)
        self.assertFalse(any(fragment.startswith("上支撑橡胶") for fragment in fragments))

    def test_sparse_matrix_rows_can_use_inherited_wide_header(self) -> None:
        header = [
            "序号 No.",
            "网络安全活动 Cybersecurity Activity",
            "活动主要内容描述 Activity Description",
            "是否适用于本项目？ Applicable for this Project?",
            "工作产出 Work Product(WP)",
            "工作产出保密性级别 Confidentiality Level",
            "主责 R",
            "批准 A",
            "支持 S",
            "通知 I",
            "咨询 C",
        ]
        headers, rows, mode = _extract_structured_table(
            [
                "|-1|制定总体网络安全计划|供应商需制定总体计划|适用 Applicable|总体网络安全计划|",
                "|-2|零部件TARA与需求识别|适用 Applicable|零部件TARA表|",
                "|●|Col2|●|Col8|",
            ],
            explicit_header=header,
        )
        self.assertEqual(mode, "structured")
        self.assertEqual(headers, header)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][:5], ["-1", "制定总体网络安全计划", "供应商需制定总体计划", "适用 Applicable", "总体网络安全计划"])

    def test_enrich_entries_reuses_recent_matrix_header_for_followup_slice(self) -> None:
        entries = [
            {
                "entry_id": "REQ-A",
                "appendix_scope": "SUB1",
                "section_key": "SUB1:@network",
                "section_instance_key": "SUB1:@network",
                "section_title": "网络安全活动 Cybersecurity Activity",
                "source_page": 23,
                "source_page_end": 23,
                "source_anchor": "Page 23",
                "display_text": "\n".join(
                    [
                        "|序号 No.|网络安全活动 Cybersecurity Activity|活动主要内容描述 Activity Description|是否适用于本项目？ Applicable for this Project?|工作产出 Work Product(WP)|工作产出保密性级别 Confidentiality Level|主责 R|批准 A|支持 S|通知 I|咨询 C|",
                        "|-1|签署本协议|供应商需签署本协议|适用 Applicable|协议签署版|机密confidential|●|●|-|-|-|",
                    ]
                ),
            },
            {
                "entry_id": "REQ-B",
                "appendix_scope": "SUB1",
                "section_key": "SUB1:2",
                "section_instance_key": "SUB1:2",
                "section_title": "产品网络安全生命周期-概念设计阶段",
                "source_page": 24,
                "source_page_end": 24,
                "source_anchor": "Page 24",
                "display_text": "\n".join(
                    [
                        "2.产品网络安全生命周期-概念设计阶段",
                        "|-1|制定总体网络安全计划|供应商需制定总体网络安全计划|适用 Applicable|总体网络安全计划|",
                        "|-2|网络安全相关功能筛选和功能定义|适用 Applicable|功能设计说明书|",
                        "|●|Col2|●|Col8|",
                    ]
                ),
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            enriched, assets = enrich_entries_with_table_assets(
                entries,
                document_id="diag",
                snapshot_root=Path(tmpdir),
            )
        self.assertEqual(enriched[1]["table_render_mode"], "structured")
        followup_asset = next(asset for asset in assets if asset.get("source_entry_id") == "REQ-B")
        self.assertEqual(followup_asset["render_mode"], "structured")
        self.assertEqual(len(followup_asset["rows"]), 2)
        self.assertEqual(followup_asset["headers"][0], "序号 No.")

    def test_enrich_entry_keeps_followup_two_column_subtables_out_of_inherited_wide_header(self) -> None:
        inherited_header = [
            "序号 No.",
            "网络安全活动 Cybersecurity Activity",
            "活动主要内容描述 Activity Description",
            "是否适用于本项目？ Applicable for this Project?",
            "工作产出 Work Product(WP)",
            "工作产出保密性级别 Confidentiality Level",
            "主责 R",
            "批准 A",
            "支持 S",
            "通知 I",
            "咨询 C",
        ]
        row = {
            "entry_id": "REQ-C",
            "appendix_scope": "SUB1",
            "section_key": "SUB1:@crypto",
            "section_instance_key": "SUB1:@crypto",
            "section_title": "常见不安全密码算法",
            "source_page": 26,
            "source_page_end": 27,
            "source_anchor": "Page 26 | Appendix SUB1 | Heading 常见不安全密码算法",
            "display_text": "\n".join(
                [
                    "|用途|Col2|常见不安全密码算法|可遗留使用密码算法|推荐使用的强密码算法|",
                    "|对称加密|分组加密|Blowfish, DES, DESX, RC2, Skipjack, 2TDEA, TEA、3DES|AES-GCM(≥128 bits)|",
                    "|对称加密|流加密|SEAL, CYLINK_MEK, RC4|AES-CTR(≥128 bits)AES- OFB(≥128 bits) chacha20- poly1305|",
                    "|密钥协商|ECDH|ECDH(< 224bits)|ECDH(<256bits)|ECDH(≥256 bits)|",
                    "工作产出保密性级别 Confidentiality Level",
                    "|高度机密highly confidential|only the organization who created the work product allowed access|",
                    "|机密confidential|Both customer and supplier are allowed access the work product|",
                    "Ref： ISO 21434 Annex C",
                    "RASIC Definition RASIC 定义",
                    "|主责（R）|该方负责完成此项活动。|",
                    "|批准(A)|该方负责在此项活动完成时进行审批同意或拒绝。|",
                ]
            ),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            enriched, assets = enrich_entries_with_table_assets(
                [row],
                document_id="diag",
                snapshot_root=Path(tmpdir),
            )
            direct, direct_assets = enrich_entry_with_table_assets(
                row,
                document_id="diag",
                snapshot_root=Path(tmpdir),
                inherited_header=inherited_header,
            )
        self.assertEqual(enriched[0]["table_render_mode"], "structured")
        refs = direct["table_refs"]
        self.assertEqual(len(refs), 3)
        self.assertEqual(refs[1]["inherited_headers"], ["工作产出保密性级别", "说明"])
        self.assertEqual(refs[2]["inherited_headers"], ["RASIC 定义", "说明"])
        self.assertEqual(refs[1]["slice_rows"][0][0], "高度机密highly confidential")
        self.assertEqual(refs[2]["slice_rows"][0][0], "主责（R）")
        self.assertEqual(len(direct_assets[1]["headers"]), 2)
        self.assertEqual(len(direct_assets[2]["headers"]), 2)

    def test_enrich_entry_drops_trailing_directory_band_after_last_table(self) -> None:
        row = {
            "entry_id": "REQ-Z",
            "section_key": "SUB1:4",
            "section_instance_key": "SUB1:4",
            "section_title": "集成验证要求",
            "source_page": 13,
            "source_page_end": 15,
            "source_anchor": "Page 13 | Appendix SUB1 | Section 4 集成验证要求",
            "display_text": "\n".join(
                [
                    "4.集成验证要求：",
                    "产品需满足由OEM组织的系统级和整车级试验要求。",
                    "序号测试类别测试小类测试项目",
                    "1 产品SE或者项目组定义",
                    "系统级测试",
                    "Col1",
                    "|2|整车环境适应性|三高测试|高温适应性|",
                    "|3|高原适应性| |",
                    "PDS表位置",
                    "内容",
                ]
            ),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            enriched, assets = enrich_entries_with_table_assets(
                [
                    {"entry_id": "REQ-Y", "section_title": "集成验证要求", "display_text": "集成验证要求"},
                    row,
                    {"entry_id": "REQ-X", "section_title": "附件4--诊断、OTA相关标准", "display_text": "附件4--诊断、OTA相关标准"},
                ],
                document_id="diag",
                snapshot_root=Path(tmpdir),
            )
        current = enriched[1]
        blocks = current.get("content_blocks") or []
        self.assertEqual([block.get("kind") for block in blocks], ["paragraph", "table"])
        self.assertFalse(any("PDS表位置" in str(block.get("text") or "") for block in blocks))
        self.assertEqual(len(current.get("table_refs") or []), 1)

    def test_enrich_entry_uses_clause_window_when_snapshot_only_table_has_trailing_context_band(self) -> None:
        row = {
            "entry_id": "REQ-Z2",
            "section_key": "SUB1:4",
            "section_instance_key": "SUB1:4",
            "section_title": "集成验证要求",
            "source_page": 13,
            "source_page_end": 15,
            "source_anchor": "Page 13 | Appendix SUB1 | Section 4 集成验证要求",
            "display_text": "\n".join(
                [
                    "4.集成验证要求：",
                    "产品需满足由OEM组织的系统级和整车级试验要求。",
                    "序号测试类别测试小类测试项目",
                    "1 产品SE或者项目组定义",
                    "系统级测试",
                    "Col1",
                    "|2|整车环境适应性|三高测试|高温适应性|",
                    "|3|高原适应性| |",
                    "PDS表位置",
                    "内容",
                ]
            ),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.services.table_assets._write_source_snapshot", return_value=(None, {})) as mocked:
                enrich_entry_with_table_assets(
                    row,
                    document_id="diag",
                    snapshot_root=Path(tmpdir),
                    previous_entry_text="集成验证要求",
                    next_entry_text="附件4--诊断、OTA相关标准",
                )
        self.assertTrue(mocked.called)
        self.assertTrue(any(bool(call.kwargs.get("prefer_clause_window")) for call in mocked.call_args_list))

    def test_prune_redundant_lead_source_table_drops_header_stub(self) -> None:
        content_blocks = [
            {"kind": "paragraph", "text": "网络安全活动 Cybersecurity Activity"},
            {"kind": "table", "table_id": "tbl-head"},
            {"kind": "paragraph", "text": "~~time~~ 1 总体要求 General Requirements"},
            {"kind": "table", "table_id": "tbl-main"},
        ]
        table_refs = [
            {
                "table_id": "tbl-head",
                "table_title": "网络安全活动 Cybersecurity Activity",
                "slice_render_mode": "structured",
                "recommended_export_variant": "source",
                "inherited_headers": ["序号", "活动", "Col3"],
                "slice_rows": [["序号", "活动", "描述"]],
            },
            {
                "table_id": "tbl-main",
                "table_title": "网络安全活动 Cybersecurity Activity",
                "slice_render_mode": "snapshot_only",
                "recommended_export_variant": "source",
                "slice_rows": [],
            },
        ]
        blocks, refs, dropped = ta._prune_redundant_lead_source_tables(content_blocks, table_refs)
        self.assertEqual(dropped, {"tbl-head"})
        self.assertEqual([block.get("table_id") for block in blocks if block.get("kind") == "table"], ["tbl-main"])
        self.assertEqual([ref.get("table_id") for ref in refs], ["tbl-main"])

    def test_enrich_entry_uses_clause_window_for_cross_page_local_clause_table(self) -> None:
        row = {
            "entry_id": "REQ-Z3",
            "section_key": "6.4",
            "section_instance_key": "6.4",
            "section_title": "产品详细描述",
            "source_page": 5,
            "source_page_end": 6,
            "source_anchor": "Page 5 | Section 6.4）低压电气接口",
            "display_text": "\n".join(
                [
                    "6.4）低压电气接口",
                    "|序号|定义|描述|备注|",
                    "|1|12V|12V低压电源正| |",
                    "|2|GND|12V低压电源负| |",
                    "|3|KL15|ASC硬线唤醒| |",
                    "|4|驱动CAN H|驱动CAN高信号|双绞线（线径≥0.5mm²）|",
                    "|5|驱动CAN L|驱动CAN低信号|双绞线（线径≥0.5mm²）|",
                ]
            ),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.services.table_assets._write_source_snapshot", return_value=(None, {})) as mocked:
                enrich_entry_with_table_assets(
                    row,
                    document_id="diag",
                    snapshot_root=Path(tmpdir),
                    previous_entry_text="6.4）低压电气接口",
                    next_entry_text="6.5）电气性能要求",
                )
        self.assertTrue(any(bool(call.kwargs.get("prefer_clause_window")) for call in mocked.call_args_list))

    def test_enrich_entry_uses_clause_window_for_small_heading_table(self) -> None:
        row = {
            "entry_id": "REQ-Z4",
            "section_key": "2.3",
            "section_instance_key": "2.3",
            "section_title": "特殊特性控制要求",
            "source_page": 12,
            "source_page_end": 12,
            "source_anchor": "Page 12 | Section 2.3 特殊特性控制要求",
            "display_text": "\n".join(
                [
                    "2.3 特殊特性控制要求",
                    "针对特殊特性常见的控制方法有防错 、SPC与能力分析和100%检验等，如对特殊特性进行测量或能力分析 ，需满足以下",
                    "|测量系统分析|初始过程能力|量产过程能力|",
                    "|Cgk≥1.33 GRR%≦10%（公差法）|Ppk≥1.67|关键特性Cpk≥1.67|",
                    "当能力不足时，应对发货的产品的进行 100%测量。",
                ]
            ),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.services.table_assets._write_source_snapshot", return_value=(None, {})) as mocked:
                enrich_entry_with_table_assets(
                    row,
                    document_id="diag",
                    snapshot_root=Path(tmpdir),
                    previous_entry_text="特殊特性控制要求",
                    next_entry_text="3 其他要求",
                )
        self.assertTrue(any(bool(call.kwargs.get("prefer_clause_window")) for call in mocked.call_args_list))

    def test_enrich_entry_clause_window_uses_next_entry_anchor_for_followup_paragraph(self) -> None:
        row = {
            "entry_id": "REQ-Z4B",
            "section_key": "2.3",
            "section_instance_key": "2.3",
            "section_title": "特殊特性控制要求",
            "source_page": 12,
            "source_page_end": 12,
            "source_anchor": "Page 12 | Section 2.3 特殊特性控制要求",
            "display_text": "\n".join(
                [
                    "2.3 特殊特性控制要求",
                    "针对特殊特性常见的控制方法有防错 、SPC与能力分析和100%检验等，如对特殊特性进行测量或能力分析 ，需满足以下",
                    "|测量系统分析|初始过程能力|量产过程能力|",
                    "|Cgk≥1.33 GRR%≦10%（公差法）|Ppk≥1.67|关键特性Cpk≥1.67 重要特性Cpk≥1.33 过程不稳定时使用Ppk|",
                    "当能力不足时，应对发货的产品的进行 100%测量。",
                ]
            ),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("app.services.table_assets._write_source_snapshot", return_value=(None, {})) as mocked:
                enrich_entry_with_table_assets(
                    row,
                    document_id="diag",
                    snapshot_root=Path(tmpdir),
                    previous_entry_text="2.2 特殊特性分类与标识",
                    next_entry_text="可靠性设计目标",
                )
        self.assertTrue(mocked.called)
        first_call = mocked.call_args_list[0]
        self.assertTrue(bool(first_call.kwargs.get("prefer_clause_window")))
        self.assertEqual(first_call.kwargs.get("after_context_text"), "可靠性设计目标")

    def test_enrich_entries_uses_neighbor_paragraph_anchor_for_recovered_table(self) -> None:
        current = {
            "entry_id": "REQ-Z4C",
            "section_key": "3.2.1",
            "section_instance_key": "3.2.1",
            "section_title": "产品信息",
            "source_page": 2,
            "source_page_end": 2,
            "display_text": "3.2.1 产品信息\n备注：1、请供应商根据技术数模提供轻量化方案。",
        }
        next_row = {
            "entry_id": "REQ-Z4D",
            "section_key": "1",
            "section_instance_key": "1",
            "section_title": "油泵性能要求",
            "source_page": 3,
            "source_page_end": 3,
            "display_text": "1）油泵性能要求\n|序号|项目|要求|\n|1|总成重量|前＜16.1kg|",
            "content_blocks": [
                {"kind": "paragraph", "text": "1）油泵性能要求"},
                {"kind": "table", "table_id": "tbl-next"},
            ],
            "table_refs": [
                {
                    "table_id": "tbl-next",
                    "table_title": "1）油泵性能要求",
                    "slice_render_mode": "structured",
                    "slice_rows": [["1", "总成重量", "前＜16.1kg"]],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "app.services.table_assets._recover_table_from_pdf_source",
                return_value=(["序号", "项目"], [["1", "前油泵总成"]], []),
            ), patch("app.services.table_assets._looks_like_source_table_candidate_text", return_value=True), patch(
                "app.services.table_assets._write_source_snapshot",
                return_value=(None, {}),
            ) as mocked:
                ta.enrich_entries_with_table_assets(
                    [current, next_row],
                    document_id="diag",
                    snapshot_root=Path(tmpdir),
                    source_file_path="sample.pdf",
                )
        self.assertTrue(mocked.called)
        self.assertTrue(
            any(call.kwargs.get("after_context_text") == "1）油泵性能要求" for call in mocked.call_args_list)
        )

    def test_paragraph_only_blocks_drop_repeated_trailing_merged_line(self) -> None:
        blocks = ta._paragraph_only_blocks(
            [
                {
                    "kind": "paragraph",
                    "text": "\n".join(
                        [
                            "4）外观要求",
                            "4、此外观标准中只要求不可接受的项目，对其状态进行定义及量化，未提及到的项目均默认为合格。",
                            "—表面处理方式：零件中各个铸铝壳体外观颜色应保持一致，整体为暗黑色。",
                            "—硬管无变形，涂层完好，无焊接缺陷；",
                            "4、此外观标准中只要求不可接受的项目，对其状态进行定义及量化，未提及到的项目均默认为合格。 "
                            "—表面处理方式：零件中各个铸铝壳体外观颜色应保持一致，整体为暗黑色。 "
                            "—硬管无变形，涂层完好，无焊接缺陷；",
                        ]
                    ),
                }
            ]
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(str(blocks[0]["text"]).count("4、此外观标准中只要求不可接受的项目"), 1)

    def test_refresh_enriched_entry_text_fields_rebuilds_clean_text_from_blocks(self) -> None:
        dirty_text = "\n".join(
            [
                "4）外观要求",
                "4、此外观标准中只要求不可接受的项目，对其状态进行定义及量化，未提及到的项目均默认为合格。",
                "—表面处理方式：零件中各个铸铝壳体外观颜色应保持一致，整体为暗黑色。",
                "—硬管无变形，涂层完好，无焊接缺陷；",
                "4、此外观标准中只要求不可接受的项目，对其状态进行定义及量化，未提及到的项目均默认为合格。 "
                "—表面处理方式：零件中各个铸铝壳体外观颜色应保持一致，整体为暗黑色。 "
                "—硬管无变形，涂层完好，无焊接缺陷；",
            ]
        )
        row = {
            "display_text": dirty_text,
            "raw_text": dirty_text,
            "requirement_text": dirty_text,
            "content_blocks": [
                {"kind": "paragraph", "text": dirty_text},
                {"kind": "table", "table_id": "tbl-clean"},
            ],
            "table_refs": [
                {
                    "table_id": "tbl-clean",
                    "inherited_headers": ["序号", "定义", "描述", "备注"],
                    "slice_rows": [
                        ["1", "12V", "12V低压电源正", "Col4"],
                        ["#REF!", "#REF!", "#REF!", "#REF!"],
                    ],
                    "slice_render_mode": "structured",
                    "source_snapshot_path": "/tmp/source.png",
                }
            ],
        }
        cleaned = ta._refresh_enriched_entry_text_fields(row)
        self.assertEqual(cleaned["display_text"].count("4、此外观标准中只要求不可接受的项目"), 1)
        self.assertIn("序号 / 定义 / 描述", cleaned["display_text"])
        self.assertNotIn("Col4", cleaned["display_text"])
        self.assertNotIn("#REF!", cleaned["display_text"])

    def test_recommend_table_export_variant_prefers_source_for_axis_symbol_table(self) -> None:
        recommendation = ta._recommend_table_export_variant(
            {
                "inherited_headers": ["总成自由度", "质心最大位移下限", "质心最大位移上限"],
                "slice_rows": [
                    ["X", "-4", "4"],
                    ["Y", "-2.5", "2.5"],
                    ["Z", "-4", "4"],
                    ["RX", "-0.5", "0.5"],
                    ["RY", "-2.5", "2.5"],
                    ["RZ", "-0.5", "0.5"],
                ],
                "slice_render_mode": "structured",
                "source_snapshot_path": "/tmp/source.png",
            }
        )
        self.assertEqual(recommendation["recommended_export_variant"], "source")
        self.assertIn("切片可读性差", recommendation["export_variant_reasons"])

    def test_refresh_enriched_entry_text_fields_drops_header_echo_row_from_two_column_subtable(self) -> None:
        row = {
            "display_text": "工作产出保密性级别 Confidentiality Level",
            "raw_text": "工作产出保密性级别 Confidentiality Level",
            "requirement_text": "工作产出保密性级别 Confidentiality Level",
            "content_blocks": [
                {"kind": "paragraph", "text": "工作产出保密性级别 Confidentiality Level"},
                {"kind": "table", "table_id": "tbl-sub"},
            ],
            "table_refs": [
                {
                    "table_id": "tbl-sub",
                    "inherited_headers": ["工作产出保密性级别", "说明"],
                    "slice_rows": [
                        ["工作产出保密性级别", "说明"],
                        ["高度机密", "仅创建方可访问"],
                    ],
                    "slice_render_mode": "structured",
                }
            ],
        }
        cleaned = ta._refresh_enriched_entry_text_fields(row)
        self.assertEqual(cleaned["display_text"].count("|工作产出保密性级别|说明|"), 1)
        self.assertIn("|高度机密|仅创建方可访问|", cleaned["display_text"])

    def test_recommend_table_export_variant_prefers_source_for_compact_threshold_table(self) -> None:
        recommendation = ta._recommend_table_export_variant(
            {
                "inherited_headers": ["测量系统分析", "初始过程能力", "量产过程能力"],
                "slice_rows": [
                    [
                        "Cgk≥1.33 GRR%≦10%（公差法）",
                        "Ppk≥1.67",
                        "关键特性Cpk≥1.67 重要特性Cpk≥1.33 过程不稳定时使用Ppk",
                    ]
                ],
                "inherited_notes": [
                    "2.3 特殊特性控制要求",
                    "当能力不足时，应对发货的产品的进行 100%测量。",
                ],
                "slice_render_mode": "structured",
                "source_snapshot_path": "/tmp/source.png",
            }
        )
        self.assertEqual(recommendation["recommended_export_variant"], "source")
        self.assertIn("切片可读性差", recommendation["export_variant_reasons"])

    def test_recommend_table_export_variant_prefers_source_when_pdf_shows_more_columns_than_slice(self) -> None:
        recommendation = ta._recommend_table_export_variant(
            {
                "inherited_headers": ["序号", "项目", "要求"],
                "slice_rows": [
                    ["1", "低温存储", "/"],
                    ["2", "高温存储", "/"],
                    ["3", "低温工作和唤醒", "/"],
                ],
                "slice_render_mode": "structured",
                "source_snapshot_path": "/tmp/source.png",
                "source_structure_mismatch": True,
                "source_column_count_hint": 4,
            }
        )
        self.assertEqual(recommendation["recommended_export_variant"], "source")
        self.assertIn("切片可读性差", recommendation["export_variant_reasons"])

    def test_normalize_table_ref_keeps_complex_rows_source_locked(self) -> None:
        ref = ta._normalize_table_ref(
            {
                "table_id": "tbl_complex",
                "source_snapshot_path": "/tmp/source.png",
                "slice_snapshot_path": "/tmp/slice.png",
                "slice_render_mode": "structured",
                "inherited_headers": ["Col1", "Col2"],
                "slice_rows": [["a", "b"]],
                "complex_table_mode": True,
                "complex_zone_id": "cx-merged-requirement-matrix-p17-p22",
                "complex_family": "merged_requirement_matrix",
                "complex_split_level": "group",
                "export_variant_locked": True,
                "export_variant": "slice",
            }
        )

        self.assertTrue(ref["complex_table_mode"])
        self.assertTrue(ref["export_variant_locked"])
        self.assertEqual(ref["recommended_export_variant"], "source")
        self.assertEqual(ref["export_variant"], "source")
        self.assertFalse(ref["export_variant_overridden"])

    def test_enrich_complex_table_row_with_existing_source_snapshot_still_returns_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "complex-source.png"
            PILImage.new("RGB", (320, 180), "white").save(snapshot_path)

            row = {
                "entry_id": "REQ-CX-group",
                "req_code": "REQ-CX-group",
                "entry_kind": "REQUIREMENT",
                "display_text": "网络安全",
                "raw_text": "网络安全",
                "requirement_text": "网络安全",
                "source_page": 17,
                "source_page_end": 20,
                "complex_table_mode": True,
                "complex_zone_id": "cx-merged-requirement-matrix-p17-p22",
                "complex_family": "merged_requirement_matrix",
                "complex_role": "group",
                "complex_group_label": "网络安全",
                "complex_warnings": ["跨页复杂表"],
                "complex_shadow_signatures": ["sig-1"],
                "table_refs": [
                    {
                        "table_id": "tbl_complex_group",
                        "table_title": "网络安全",
                        "source_page": 17,
                        "source_page_end": 20,
                        "source_snapshot_path": str(snapshot_path),
                        "slice_snapshot_path": str(snapshot_path),
                        "slice_render_mode": "snapshot_only",
                        "complex_table_mode": True,
                        "complex_zone_id": "cx-merged-requirement-matrix-p17-p22",
                        "complex_family": "merged_requirement_matrix",
                        "complex_split_level": "group",
                        "complex_group_label": "网络安全",
                        "complex_shadow_signatures": ["sig-1"],
                        "export_variant": "source",
                        "recommended_export_variant": "source",
                        "export_variant_locked": True,
                    }
                ],
                "content_blocks": [{"kind": "table", "table_id": "tbl_complex_group"}],
                "has_table_asset": True,
                "table_render_mode": "snapshot_only",
            }

            enriched, assets = ta._enrich_complex_table_row(
                row,
                document_id="doc-complex",
                snapshot_root=Path(tmpdir),
                source_file_path="",
                source_snapshot_cache={},
                source_table_detection_cache={},
                source_line_detection_cache={},
            )

        self.assertTrue(enriched["has_table_asset"])
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["table_id"], "tbl_complex_group")
        self.assertEqual(assets[0]["source_snapshot_path"], str(snapshot_path))
        self.assertEqual(assets[0]["complex_shadow_signatures"], ["sig-1"])

    def test_merged_requirement_complex_full_page_snapshot_uses_higher_resolution_budget(self) -> None:
        width = ta._complex_full_page_snapshot_width_px(
            {"complex_family": "merged_requirement_matrix"}
        )
        self.assertGreater(width, 2160)

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_enrich_complex_full_page_group_keeps_readable_snapshot_height(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            row = {
                "entry_id": "REQ-CX-merged-network",
                "req_code": "REQ-CX-merged-network",
                "entry_kind": "REQUIREMENT",
                "display_text": "网络安全",
                "raw_text": "网络安全",
                "requirement_text": "网络安全",
                "source_page": 17,
                "source_page_end": 20,
                "complex_table_mode": True,
                "complex_zone_id": "cx-merged-requirement-matrix-p17-p22",
                "complex_family": "merged_requirement_matrix",
                "complex_role": "group",
                "complex_group_label": "网络安全",
                "complex_split_level": "group",
                "complex_warnings": ["跨页复杂表"],
                "complex_row_policy": "exportable_group",
                "complex_snapshot_mode": "full_page_span",
                "complex_snapshot_title": "网络安全",
                "complex_after_label_hint": "",
                "complex_shadow_signatures": ["sig-1"],
                "table_refs": [],
                "content_blocks": [],
                "has_table_asset": False,
                "table_render_mode": "",
            }

            enriched, assets = ta._enrich_complex_table_row(
                row,
                document_id="doc-complex-full-span",
                snapshot_root=Path(tmpdir),
                source_file_path=str(_OIL_PUMP_SAMPLE_PDF),
                source_snapshot_cache={},
                source_table_detection_cache={},
                source_line_detection_cache={},
            )

            self.assertTrue(enriched["has_table_asset"])
            self.assertEqual(len(assets), 1)
            source_snapshot_path = Path(enriched["table_refs"][0]["source_snapshot_path"])
            with PILImage.open(source_snapshot_path) as image:
                self.assertGreaterEqual(image.width, 1800)
                self.assertGreater(image.height, 1800)
            self.assertEqual(enriched["table_refs"][0]["complex_snapshot_mode"], "full_page_span")

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_enrich_complex_anchor_window_group_keeps_full_band_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            row = {
                "entry_id": "REQ-CX-lifecycle-general",
                "req_code": "REQ-CX-lifecycle-general",
                "entry_kind": "REQUIREMENT",
                "display_text": "1 总体要求",
                "raw_text": "1 总体要求",
                "requirement_text": "1 总体要求",
                "source_page": 23,
                "source_page_end": 23,
                "complex_table_mode": True,
                "complex_zone_id": "cx-lifecycle-matrix-p23-p25",
                "complex_family": "lifecycle_matrix",
                "complex_role": "group",
                "complex_group_label": "1 总体要求",
                "complex_split_level": "group",
                "complex_warnings": ["跨页宽矩阵，按阶段 band 做高置信拆分"],
                "complex_row_policy": "exportable_group",
                "complex_snapshot_mode": "anchor_window",
                "complex_snapshot_title": "1 总体要求",
                "complex_after_label_hint": "2 概念设计",
                "complex_shadow_signatures": ["sig-1"],
                "table_refs": [],
                "content_blocks": [],
                "has_table_asset": False,
                "table_render_mode": "",
            }

            enriched, assets = ta._enrich_complex_table_row(
                row,
                document_id="doc-complex-band",
                snapshot_root=Path(tmpdir),
                source_file_path=str(_OIL_PUMP_SAMPLE_PDF),
                source_snapshot_cache={},
                source_table_detection_cache={},
                source_line_detection_cache={},
            )

            self.assertTrue(enriched["has_table_asset"])
            self.assertEqual(len(assets), 1)
            source_snapshot_path = Path(enriched["table_refs"][0]["source_snapshot_path"])
            with PILImage.open(source_snapshot_path) as image:
                self.assertEqual(image.width, 920)
                self.assertGreater(image.height, 320)

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_source_recovery_prefers_actual_delivery_table_over_merged_page_candidate(self) -> None:
        recovered = _recover_table_from_pdf_source(
            source_file_path=str(_OIL_PUMP_SAMPLE_PDF),
            source_page=10,
            source_page_end=10,
            table_text=(
                "5.2 交付物要求\n"
                "为保证项目顺利进行，供应商需提供以下交付物。\n"
                "交付物清单\n交付时间\n备注\n"
                "1\n设计方案说明书\n/\nTR阶段\n"
                "2\n初版3D数模\n/\nTR阶段\n"
                "3\n初版2D总成图纸\n/\n软模阶段交样前\n"
                "4\nBOM\n/\n软模阶段交样前\n"
                "5\nDFMEA\n/\n"
                "6\n尺寸检测报告\n/\n"
                "7\n性能检测报告\n/\n"
                "8\nDVP测试报告\n/\n"
                "9\n产品描述书\n/\n"
                "对OTS提供文件应符合Q/JQ 190规定"
            ),
            table_title="5.2 交付物要求",
            current_headers=[],
            current_rows=[],
            notes=[],
            before_context_text="5.1 开发计划",
            after_context_text="6、投标说明",
            source_table_detection_cache={},
            source_line_detection_cache={},
        )
        self.assertIsNotNone(recovered)
        headers, rows, notes = recovered or ([], [], [])
        self.assertEqual(headers[:4], ["交付物清单", "类别", "交付时间", "备注"])
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[0], ["1", "设计方案说明书", "/", "TR阶段"])
        self.assertEqual(rows[-1], ["9", "产品描述书", "/", ""])
        self.assertEqual(notes, [])

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_source_recovery_keeps_full_attachment_directory(self) -> None:
        recovered = _recover_table_from_pdf_source(
            source_file_path=str(_OIL_PUMP_SAMPLE_PDF),
            source_page=10,
            source_page_end=10,
            table_text=(
                "7、附件目录\n"
                "序号\n附件名称\n备注\n"
                "1\n软件开发质量要求\n"
                "2\nFMEA\n"
                "3\n可靠性设计与验证需求\n"
                "4\n诊断、OTA相关标准\n"
                "5\n技术状态对比表\n"
                "6\n信息安装需求\n"
                "7\n网络安全接口协议\n"
                "8\n密码算法表\n"
                "9\n连接器-用电器与线束匹配基本信息\n"
                "10\n连接器-测试分组\n"
                "11\n连接器-测试计划\n"
                "12\nAppendix"
            ),
            table_title="7、附件目录",
            current_headers=[],
            current_rows=[],
            notes=[],
            before_context_text="10）商务报价应至少包含设计开发费用...",
            after_context_text="Appendix SUB1",
            source_table_detection_cache={},
            source_line_detection_cache={},
        )
        self.assertIsNotNone(recovered)
        headers, rows, notes = recovered or ([], [], [])
        self.assertEqual(headers, ["序号", "附件名称", "备注"])
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0], ["1", "软件开发质量要求", ""])
        self.assertEqual(rows[-1], ["12", "Appendix", ""])
        self.assertEqual(notes, ["7、附件目录"])

    @unittest.skipUnless(_DAMPER_SAMPLE_PDF and _DAMPER_SAMPLE_PDF.exists(), "damper sample pdf not available")
    def test_source_recovery_keeps_numbered_test_outline_row_when_context_is_heading_only(self) -> None:
        row = {
            "entry_id": "REQ-SUB1-3",
            "req_code": "REQ-SUB1-3",
            "entry_kind": "REQUIREMENT",
            "appendix_scope": "SUB1",
            "appendix_title": "主动前减振器试验大纲",
            "section_code": "3",
            "section_key": "SUB1:3",
            "section_instance_key": "SUB1:3",
            "section_title": "减振器的阻尼力与气体反弹力",
            "source_page": 10,
            "source_page_end": 10,
            "source_anchor": "Page 10 | Appendix SUB1 | Section 3 减振器的阻尼力与气体反弹力 | 主动前减振器试验大纲",
            "requirement_text": (
                "|3|减振器的阻尼力与气体反弹力|-|试验行程：±50mm（若行程不足，则可减少行程进行检测，如 ±40mm、±25mm） "
                "环境温度：在（23±3）℃的恒温箱保持不少于6h； 检测速度：0.05m/s, 0.1m/s, 0.2m/s, 0.3m/s, 0.6m/s, "
                "1.0m/s,1.5m/s 电流加载：0.3A/1.0A/1.6A（两电磁阀同时） 试验方向：垂向预行程：在测量相应电流前，"
                "进行±50mm，0.3m/s，5个循环 （0A）对充气式减振器，按以下条件先测量气体反弹力： 测量位置：Ldesign "
                "振幅：±5mm 活塞速度：0.4mm/s 同时记录F-V和F-S曲线。 电流：0.3A|气反力公差±29N; 0.3A，1.0A，"
                "1.6A电流下阻尼力公差为下表所示，或35N，两者取大值； 1.0m/s以上的速度点，在0.3A电流下，"
                "可能会切换至失效模式，此时阻尼力上限需基于结果调整速度（m 0.05 0.1 0.2 0.3 0.6 1 1.5 公差 "
                "±17.5% ±15% ±10% ±10% ±10% ±10% ±7.5% 阻尼力公差气反力公差|DV/PV|供应商|6件/OTS|1天|"
                "JAC：阻尼力、阻尼力公差、气返力、气返力公差根据调校结果更新、确定|"
            ),
            "display_text": "",
            "raw_text": "",
        }
        enriched, _ = enrich_entry_with_table_assets(
            row,
            document_id="damper-doc",
            snapshot_root=Path(tempfile.mkdtemp()),
            source_file_path=str(_DAMPER_SAMPLE_PDF),
            source_snapshot_cache={},
            source_table_detection_cache={},
            source_line_detection_cache={},
            previous_entry_text="减振器的阻尼力与气体反弹力",
            next_entry_text="滑动摩擦力",
            inherited_header=None,
        )
        ref = (enriched.get("table_refs") or [None])[0]
        self.assertIsNotNone(ref)
        self.assertGreaterEqual(len(ref.get("inherited_headers") or []), 9)
        self.assertEqual((ref.get("slice_rows") or [[]])[0][0], "3")
        self.assertIn("减振器的阻尼力与气体反弹力", (ref.get("slice_rows") or [[]])[0][1])

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_source_recovery_uses_current_clause_anchor_for_small_local_table(self) -> None:
        recovered = _recover_table_from_pdf_source(
            source_file_path=str(_OIL_PUMP_SAMPLE_PDF),
            source_page=8,
            source_page_end=8,
            table_text=(
                "8.2.5.3）衬套径向动刚度曲线平滑，动静比≦2.5;\n"
                "动刚度低频测试条件\n"
                "频率（Hz）\n"
                "频差（Hz）\n"
                "振幅（mm）\n"
                "10～500\n10\n±0.05、±0.5"
            ),
            table_title="8.2.5.3）衬套径向动刚度曲线平滑，动静比≦2.5;",
            current_headers=[],
            current_rows=[],
            notes=[],
            before_context_text="8.2.5.2）衬套Z向15Hz时动静比...",
            after_context_text="8.2.5.4）衬套与泵盖吊耳之间的压出力要求",
            source_table_detection_cache={},
            source_line_detection_cache={},
        )
        self.assertIsNotNone(recovered)
        headers, rows, notes = recovered or ([], [], [])
        self.assertEqual(headers, ["频率（Hz）", "频差（Hz）", "振幅（mm）"])
        self.assertEqual(rows, [["10～500", "10", "±0.05、±0.5"]])
        self.assertEqual(notes, ["动刚度低频测试条件"])

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_source_recovery_handles_stacked_title_and_unit_rows_for_displacement_table(self) -> None:
        recovered = _recover_table_from_pdf_source(
            source_file_path=str(_OIL_PUMP_SAMPLE_PDF),
            source_page=8,
            source_page_end=8,
            table_text=(
                "8.1.3）在下列分析工况下，油泵总成质心位移满足下表中的要求；\n"
                "油泵总成质心位移控制要求\n"
                "（整车坐标系）\n"
                "总成自由度\n质心最大位移\nmm/°\n"
                "X\n-4\n4\nY\n-2.5\n2.5\nZ\n-4\n4\nRX\n-0.5\n0.5\nRY\n-2.5\n2.5\nRZ\n-0.5\n0.5"
            ),
            table_title="8.1.3）在下列分析工况下，油泵总成质心位移满足下表中的要求；",
            current_headers=[],
            current_rows=[],
            notes=[],
            before_context_text="8.1.2）xxx",
            after_context_text="8.1.4）xxx",
            source_table_detection_cache={},
            source_line_detection_cache={},
        )
        self.assertIsNotNone(recovered)
        headers, rows, notes = recovered or ([], [], [])
        self.assertEqual(headers[0], "总成自由度")
        self.assertEqual(rows[0], ["X", "-4", "4"])
        self.assertEqual(rows[-1], ["RZ", "-0.5", "0.5"])
        self.assertEqual(notes, ["油泵总成质心位移控制要求", "（整车坐标系）"])

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_source_recovery_restores_graphic_column_for_local_interface_table(self) -> None:
        recovered = _recover_table_from_pdf_source(
            source_file_path=str(_OIL_PUMP_SAMPLE_PDF),
            source_page=4,
            source_page_end=4,
            table_text=(
                "3）油泵总成接口要求（油泵总成接口需满足整车布置要求）\n"
                "序号\n项目\n要求\n"
                "1\n轮廓尺寸要求\n长度L＜405mm，宽度b＜122mm，高度h＜158.2mm\n"
                "2\n油口接头\n接口根据需求进行定义\n"
                "3\n前油泵总成与副车架接口\nZ向布置，四点连接，M10螺栓"
            ),
            table_title="3）油泵总成接口要求（油泵总成接口需满足整车布置要求）",
            current_headers=["序号", "项目", "要求"],
            current_rows=[],
            notes=[],
            before_context_text="2）控制响应性能要求",
            after_context_text="4）外观要求",
            source_table_detection_cache={},
            source_line_detection_cache={},
        )
        self.assertIsNotNone(recovered)
        headers, rows, notes = recovered or ([], [], [])
        self.assertEqual(headers, ["序号", "项目", "图示", "要求"])
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0][1], "轮廓尺寸要求")
        self.assertEqual(rows[0][3], "长度L＜405mm，宽度b＜122mm，高度h＜158.2mm")
        self.assertEqual(notes, ["3）油泵总成接口要求（油泵总成接口需满足整车布置要求）"])

    def test_row_slice_start_anchor_accepts_embedded_numeric_marker(self) -> None:
        self.assertTrue(
            ta._row_slice_bbox_is_start_anchor_compatible(
                start_anchor={
                    "text": "12",
                    "bbox": (66.72, 529.68, 71.62, 534.58),
                },
                row_slice_bbox=(42.11, 296.40, 548.88, 767.51),
            )
        )

    def test_row_slice_start_anchor_accepts_embedded_short_heading_marker(self) -> None:
        self.assertTrue(
            ta._row_slice_bbox_is_start_anchor_compatible(
                start_anchor={
                    "text": "温湿度循环(仅防水件)",
                    "bbox": (84.47, 526.68, 133.12, 531.58),
                },
                row_slice_bbox=(42.11, 296.40, 548.88, 767.51),
            )
        )

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_refine_table_page_indices_prunes_unrelated_following_page_for_common_algorithm_table(self) -> None:
        import fitz

        headers = [
            "用途",
            "Col2",
            "常见不安全密码算法",
            "可遗留使用密码算法",
            "推荐使用的强密码算法",
        ]
        rows = [
            ["对称加密", "分组加密", "Blowfish, DES, DESX, RC2, Skipjack, 2TDEA, TEA、3DES", "", "AES-GCM(≥128 bits)"],
            ["对称加密", "流加密", "SEAL, CYLINK MEK, RC4", "", "AES-CTR(≥128 bits) AES-OFB(≥128 bits) chacha20-poly1305"],
            ["哈希算法", "", "SHA0, MD2, MD4, MD5, SHA1, RIPEMD, RIPEMD-128", "SHA-1, SHA224, SHA512/224, SHA3-224", "SHA256或以上 SHA3-256或以上"],
        ]
        match_fragments = ta._build_table_match_fragments(
            table_text="",
            table_title="常见不安全密码算法",
            headers=headers,
            rows=rows,
            notes=[],
        )
        with fitz.open(str(_OIL_PUMP_SAMPLE_PDF)) as document:
            refined = ta._refine_table_page_indices(
                document,
                source_key=str(_OIL_PUMP_SAMPLE_PDF.resolve()),
                page_indices=[25, 26],
                match_fragments=match_fragments,
                headers=headers,
                slice_rows=rows,
                detection_cache={},
            )
        self.assertEqual(refined, [25])

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_refine_table_page_indices_prunes_leading_page_for_confidentiality_table(self) -> None:
        import fitz

        headers = ["工作产出保密性级别 Confidentiality Level of WP", ""]
        rows = [
            ["高度机密 highly confidential", "only the organization who created the work product is allowed to access it"],
            ["机密 confidential", "Both customer and supplier are allowed to access the work product"],
            ["可与第三方共享 shared with third parties", "This work product is allowed to be shared with external parties"],
        ]
        match_fragments = ta._build_table_match_fragments(
            table_text="",
            table_title="工作产出保密性级别 Confidentiality Level",
            headers=headers,
            rows=rows,
            notes=[],
        )
        with fitz.open(str(_OIL_PUMP_SAMPLE_PDF)) as document:
            refined = ta._refine_table_page_indices(
                document,
                source_key=str(_OIL_PUMP_SAMPLE_PDF.resolve()),
                page_indices=[25, 26],
                match_fragments=match_fragments,
                headers=headers,
                slice_rows=rows,
                detection_cache={},
            )
        self.assertEqual(refined, [26])

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_refine_table_page_indices_extends_numbered_section_tail_to_next_page(self) -> None:
        import fitz

        headers = [
            "15",
            "高温寿命(仅防水件) High Temperature Exposure",
            "外观 Visual Inspection",
            "USCAR-2 Rev 6 5.1.8",
            "目视检查",
            "无外观损伤",
            "PV",
            "10",
            "C Sample",
        ]
        rows = [
            ["15", "高温寿命(仅防水件) High Temperature Exposure", "连接器或端子插拔 Connector and/or Terminal Cycling", "USCAR-2 Rev 6 5.1.7", "完成一对连接器或端子的10次插拔；", "连接器无变形等外观损伤", "PV", "10", "C Sample"],
            ["15", "高温寿命(仅防水件) High Temperature Exposure", "绝缘电阻 Insulation Resistance", "USCAR-2 Rev 6 5.5.1", "端子与端子之间,端子与塑壳之间", "1）环境前：100MΩ @ 500V 60s 2）环境后：100MΩ @ 500V 60s", "PV", "10", "C Sample"],
        ]
        match_fragments = ta._build_table_match_fragments(
            table_text="",
            table_title="15 高温寿命(仅防水件) High Temperature Exposure",
            headers=headers,
            rows=rows,
            notes=[],
        )
        with fitz.open(str(_OIL_PUMP_SAMPLE_PDF)) as document:
            refined = ta._refine_table_page_indices(
                document,
                source_key=str(_OIL_PUMP_SAMPLE_PDF.resolve()),
                page_indices=[32],
                match_fragments=match_fragments,
                headers=headers,
                slice_rows=rows,
                detection_cache={},
            )
        self.assertEqual(refined, [32, 33])

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_append_numbered_section_tail_from_following_page(self) -> None:
        import fitz

        headers = [
            "15",
            "高温寿命(仅防水件) High Temperature Exposure",
            "外观 Visual Inspection",
            "USCAR-2 Rev 6 5.1.8",
            "目视检查",
            "无外观损伤",
            "PV",
            "10",
            "C Sample",
        ]
        rows = [
            ["15", "高温寿命(仅防水件) High Temperature Exposure", "连接器或端子插拔 Connector and/or Terminal Cycling", "USCAR-2 Rev 6 5.1.7", "完成一对连接器或端子的10次插拔；", "连接器无变形等外观损伤", "PV", "10", "C Sample"],
            ["15", "高温寿命(仅防水件) High Temperature Exposure", "绝缘电阻 Insulation Resistance", "USCAR-2 Rev 6 5.5.1", "端子与端子之间,端子与塑壳之间", "1）环境前：100MΩ @ 500V 60s 2）环境后：100MΩ @ 500V 60s", "PV", "10", "C Sample"],
            ["15", "高温寿命(仅防水件) High Temperature Exposure", "浸没 Submersion", "USCAR-2 Rev 6 5.6.5", "按照浸泡模式条件测试", "测试条件", "PV", "10", "C Sample"],
        ]
        with fitz.open(str(_OIL_PUMP_SAMPLE_PDF)) as document:
            extended = ta._append_numbered_section_continuation_rows(
                document,
                source_key=str(_OIL_PUMP_SAMPLE_PDF.resolve()),
                page_indices=[32, 33],
                headers=headers,
                rows=rows,
                detection_cache={},
            )
        self.assertTrue(any("高压水喷射" in str(row[2]) for row in extended))
        self.assertTrue(any("产品无进水" in str(row[5]) for row in extended))

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_extend_structured_table_rows_from_source_updates_page_span_for_numbered_tail(self) -> None:
        headers = [
            "15",
            "高温寿命(仅防水件) High Temperature Exposure",
            "外观 Visual Inspection",
            "USCAR-2 Rev 6 5.1.8",
            "目视检查",
            "无外观损伤",
            "PV",
            "10",
            "C Sample",
        ]
        rows = [
            ["15", "高温寿命(仅防水件) High Temperature Exposure", "连接器或端子插拔 Connector and/or Terminal Cycling", "USCAR-2 Rev 6 5.1.7", "完成一对连接器或端子的10次插拔；", "连接器无变形等外观损伤", "PV", "10", "C Sample"],
            ["15", "高温寿命(仅防水件) High Temperature Exposure", "绝缘电阻 Insulation Resistance", "USCAR-2 Rev 6 5.5.1", "端子与端子之间,端子与塑壳之间", "1）环境前：100MΩ @ 500V 60s 2）环境后：100MΩ @ 500V 60s", "PV", "10", "C Sample"],
            ["15", "高温寿命(仅防水件) High Temperature Exposure", "浸没 Submersion", "USCAR-2 Rev 6 5.6.5", "按照浸泡模式条件测试", "测试条件", "PV", "10", "C Sample"],
        ]
        extended, start_page, end_page = ta._extend_structured_table_rows_from_source(
            source_file_path=str(_OIL_PUMP_SAMPLE_PDF),
            source_page=33,
            source_page_end=33,
            table_text="15 高温寿命(仅防水件) High Temperature Exposure",
            table_title="15 高温寿命(仅防水件) High Temperature Exposure",
            headers=headers,
            rows=rows,
            notes=[],
            source_table_detection_cache={},
        )
        self.assertEqual(start_page, 33)
        self.assertEqual(end_page, 34)
        self.assertTrue(any("高压水喷射" in str(row[2]) for row in extended))

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_select_best_pdf_crop_candidate_prefers_compact_delivery_table_before_next_heading(self) -> None:
        import fitz

        headers = ["交付物清单", "类别", "交付时间", "备注"]
        rows = [
            ["1", "设计方案说明书", "/", "TR阶段"],
            ["2", "初版3D数模", "/", "TR阶段"],
            ["3", "初版2D总成图纸", "/", "软模阶段交样前"],
            ["4", "BOM", "/", "软模阶段交样前"],
            ["5", "DFMEA", "/", ""],
            ["6", "尺寸检测报告", "/", ""],
            ["7", "性能检测报告", "/", ""],
            ["8", "DVP测试报告", "/", ""],
            ["9", "产品描述书", "/", ""],
        ]
        match_fragments = ta._build_table_match_fragments(
            table_text="5.2 交付物要求",
            table_title="5.2 交付物要求",
            headers=headers,
            rows=rows,
            notes=[],
        )
        with fitz.open(str(_OIL_PUMP_SAMPLE_PDF)) as document:
            end_anchor = {
                "page_index": 9,
                "bbox": (51.192, 316.997, 103.973, 325.828),
                "text": "5.3 数据开发阶段",
            }
            candidates = ta._load_pdf_table_candidates(
                document,
                source_key=str(_OIL_PUMP_SAMPLE_PDF.resolve()),
                page_index=9,
                detection_cache={},
            )
            chosen = ta._select_best_pdf_crop_candidate(
                candidates,
                match_fragments=match_fragments,
                headers=headers,
                slice_rows=rows,
                end_anchor=end_anchor,
            )
        self.assertIsNotNone(chosen)
        self.assertAlmostEqual(float(chosen["bbox"][2]), 492.78001403808594, places=1)

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_source_recovery_handles_decoupling_table_with_local_clause_boundary(self) -> None:
        recovered = _recover_table_from_pdf_source(
            source_file_path=str(_OIL_PUMP_SAMPLE_PDF),
            source_page=7,
            source_page_end=7,
            table_text=(
                "8.1.2）产品各方向解耦率及各向频率应满足下表的要求（油泵支架隔振衬套如存在小衬套，需要考虑二级隔振），油泵的刚体模态\n"
                "油泵总成刚体模态分配（整车坐标系）\n"
                "模态\n下限\n上限\n目标\n解耦率（%）\n"
                "For/Aft\n15\n30\n避开40-60Hz 38-45Hz 70-95Hz 120-180Hz\n90"
            ),
            table_title="8.1.2）产品各方向解耦率及各向频率应满足下表的要求（油泵支架隔振衬套如存在小衬套，需要考虑二级隔振），油泵的刚体模态",
            current_headers=[],
            current_rows=[],
            notes=[],
            before_context_text="8.1.1）油泵支架隔振衬套设计时应充分考虑周边相关部件情况",
            after_context_text="8.1.3）在下列分析工况下，油泵总成质心位移满足下表中的要求；",
            source_table_detection_cache={},
            source_line_detection_cache={},
        )
        self.assertIsNotNone(recovered)
        headers, rows, notes = recovered or ([], [], [])
        self.assertEqual(headers, ["模态", "频率（Hz） 下限", "频率（Hz） 上限", "频率（Hz） 目标", "解耦率（%）"])
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0][0], "For/Aft")
        self.assertEqual(rows[0][3], "")
        self.assertEqual(rows[1][3], "避开 40-60Hz")
        self.assertEqual(rows[2][3], "38-45Hz")
        self.assertEqual(rows[3][3], "70-95Hz")
        self.assertEqual(rows[4][3], "120-180Hz")
        self.assertEqual(rows[5][3], "")
        self.assertEqual(rows[-1][0], "Yaw")
        self.assertEqual(notes, ["油泵总成刚体模态分配（整车坐标系）"])

    @unittest.skipUnless(_OIL_PUMP_SAMPLE_PDF and _OIL_PUMP_SAMPLE_PDF.exists(), "oil pump sample pdf not available")
    def test_source_recovery_handles_functional_safety_goal_table_without_placeholder_column(self) -> None:
        recovered = _recover_table_from_pdf_source(
            source_file_path=str(_OIL_PUMP_SAMPLE_PDF),
            source_page=7,
            source_page_end=7,
            table_text=(
                "6.7）功能安全要求\n"
                "—ASC的功能安全等级应达到最高ASIL C，最终功能安全要求以双方讨论为准。\n"
                "—供应商须按照ISO26262:2018功能安全标准进行开发,功能安全设计需满足如下安全目标:\n"
                "编号\n安全目标\nMax\n安全状态\n故障容错时间间隔FTTI\n备注\n"
                "ASC_01\n全主动悬架系统需要避免错误的车身高度调节导致车辆偏离车道线\nC\n禁止悬架主动调节（禁止主动力输出），告警驾驶员\n200ms"
            ),
            table_title="6.7）功能安全要求",
            current_headers=[],
            current_rows=[],
            notes=[],
            before_context_text="6.6.5）故障诊断要求 —参考附件4。",
            after_context_text="7）可靠性要求",
            source_table_detection_cache={},
            source_line_detection_cache={},
        )
        self.assertIsNotNone(recovered)
        headers, rows, notes = recovered or ([], [], [])
        self.assertEqual(headers, ["编号", "安全目标", "Max", "安全状态", "故障容错时间间隔FTTI", "备注"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], "ASC 01")
        self.assertEqual(rows[-1][0], "ASC 03")
        self.assertGreaterEqual(len(notes), 2)


if __name__ == "__main__":
    unittest.main()
