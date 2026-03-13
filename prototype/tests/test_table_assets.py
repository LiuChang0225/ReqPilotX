import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype")

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

_UPLOADS_DIR = Path(__file__).resolve().parents[1] / "storage" / "uploads"
_OIL_PUMP_SAMPLE_PDF = next(iter(sorted(_UPLOADS_DIR.glob("*前后油泵总成*.pdf"))), None)


class TableAssetsParsingTest(unittest.TestCase):
    def test_single_row_pipe_table_does_not_infer_fake_headers(self) -> None:
        headers, rows, mode = _extract_structured_table([
            "|1|外观检查|USCAR-2|按要求执行|PV|10|C Sample|",
        ])
        self.assertEqual(mode, "snapshot_only")
        self.assertEqual(headers, [])
        self.assertEqual(rows, [])

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
        self.assertIn("|序号|定义|描述|备注|", cleaned["display_text"])
        self.assertIn("|1|12V|12V低压电源正|", cleaned["display_text"])
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
