import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.extractor import (
    _build_line_entries,
    _extract_section_blocks,
    _extract_test_outline_rows,
    _is_bom_bridge_artifact,
    _is_line_noise,
    _is_plausible_section_transition,
    _parse_section_header,
    _consolidate_section_blocks,
    _is_serial_table_row_candidate,
    _normalize_block_text,
    _normalize_table_lines,
    _shape_subblock,
    _split_embedded_local_clause_line,
    _split_block_by_bullets,
    analyze_section_consistency,
)
from app.services.extraction_preview import _build_complete_requirement_entries
from app.services.extraction_preview import _build_canonical_section_rows
from app.services.extraction_preview import _build_scope_catalog
from app.services.extraction_preview import _attach_requirement_section_context
from app.services.extraction_preview import _assign_section_instance_keys
from app.services.extraction_preview import _collect_test_outline_markers
from app.services.extraction_preview import _collect_section_instance_groups
from app.services.extraction_preview import counts_toward_fidelity_section
from app.services.extraction_preview import _extract_appendix_scope
from app.services.extraction_preview import _extract_explicit_section_code
from app.services.extraction_preview import _extract_section_code
from app.services.extraction_preview import _extract_section_title
from app.services.extraction_preview import _join_title_section_key
from app.services.extraction_preview import _preview_section_row_sort_key
from app.services.extraction_preview import _resolve_scope_info
from app.services.extraction_preview import _resolve_outline_section_instance_key
from app.services.extraction_preview import _split_requirement_preview_rows


def _block(code: str, title: str, page_start: int, page_end: int | None = None, text: str | None = None) -> dict:
    page_end = page_end if page_end is not None else page_start
    line_text = text or f"{code} {title}"
    return {
        "appendix_scope": "",
        "section_code": code,
        "section_title": title,
        "section_level": code.count(".") + 1,
        "_discovery_order": page_start,
        "source_page": page_start,
        "source_page_end": page_end,
        "source_line_index": 1,
        "source_line_index_end": 1,
        "source_anchor": f"Page {page_start} | Section {code} {title}",
        "entries": [
            {
                "text": line_text,
                "source_page": page_start,
                "source_line_index": 1,
                "source_anchor": f"Page {page_start}",
            }
        ],
    }


class ExtractorSectionConsolidationTest(unittest.TestCase):
    def test_major_requirement_heading_survives_after_deep_subsection(self) -> None:
        self.assertTrue(
            _is_plausible_section_transition(
                "5.6.3",
                "6",
                "法规需求",
                appendix_scope="",
                previous_title="下线配置",
                previous_page=26,
                candidate_page=27,
                previous_table_count=0,
                candidate_table_count=1,
            )
        )
        self.assertTrue(
            _is_plausible_section_transition(
                "6",
                "7",
                "功能安全需求",
                appendix_scope="",
                previous_title="法规需求",
                previous_page=27,
                candidate_page=27,
                previous_table_count=1,
                candidate_table_count=1,
            )
        )
        self.assertTrue(
            _is_plausible_section_transition(
                "1.6.3",
                "2",
                "功能分配",
                appendix_scope="",
                previous_title="文件命名",
                previous_page=7,
                candidate_page=7,
                previous_table_count=2,
                candidate_table_count=2,
                has_immediate_child=True,
            )
        )

    def test_short_series_clause_with_sentence_punctuation_is_not_dropped(self) -> None:
        self.assertEqual(
            _normalize_block_text(["10）表面处理要求以DVP为准。"]),
            "10）表面处理要求以DVP为准。",
        )

    def test_short_heading_plus_single_line_body_is_preserved(self) -> None:
        self.assertEqual(
            _normalize_block_text(["5.4 环境要求", "环境温度≤110℃。"]),
            "5.4 环境要求\n环境温度≤110℃。",
        )

    def test_table_title_rows_recover_missing_child_sections(self) -> None:
        entries = _build_line_entries(
            [
                {
                    "source_page": 4,
                    "source_anchor": "Page 4",
                    "text": (
                        "1 文档范围及控制方法\n"
                        "|版本历史|Col2|Col3|Col4|Col5|\n"
                        "|版本|更改内容|更改人|审核|日期|\n"
                        "|V1.0.0|初版|李方龙||20250415|\n"
                        "|缩写|Col2|Col3|\n"
                        "|缩写|英文描述|中文描述|\n"
                        "|CTS|Component Technology Specification|零部件技术规范|\n"
                        "1.3 图例\n"
                        "|图例|说明|图例|说明|"
                    ),
                    "table_count": 3,
                }
            ]
        )

        blocks = _consolidate_section_blocks(_extract_section_blocks(entries))

        self.assertEqual(
            [(row["section_code"], row["section_title"]) for row in blocks],
            [
                ("1", "文档范围及控制方法"),
                ("1.1", "版本历史"),
                ("1.2", "缩写"),
                ("1.3", "图例"),
            ],
        )

    def test_table_spillover_title_does_not_preempt_explicit_same_code_header(self) -> None:
        entries = _build_line_entries(
            [
                {
                    "source_page": 25,
                    "source_anchor": "Page 25",
                    "text": "7.2 诊断接口\n表10 连接器PIN 脚定义",
                    "table_count": 1,
                },
                {
                    "source_page": 26,
                    "source_anchor": "Page 26",
                    "text": "7.3 零部件诊断规范",
                    "table_count": 0,
                },
            ]
        )

        blocks = _extract_section_blocks(entries)

        self.assertEqual(
            [(row["section_code"], row["section_title"]) for row in blocks],
            [
                ("7.2", "诊断接口"),
                ("7.3", "零部件诊断规范"),
            ],
        )
        self.assertEqual(
            [entry["text"] for entry in blocks[0]["entries"]],
            ["7.2 诊断接口", "表10 连接器PIN 脚定义"],
        )
        self.assertEqual(
            [entry["text"] for entry in blocks[1]["entries"]],
            ["7.3 零部件诊断规范"],
        )

    def test_flowchart_node_title_does_not_preempt_explicit_same_code_header(self) -> None:
        entries = _build_line_entries(
            [
                {
                    "source_page": 69,
                    "source_anchor": "Page 69",
                    "text": "8.5 一般 UDS 服务否定应答判断流程\n子功能是否支持",
                    "table_count": 1,
                },
                {
                    "source_page": 70,
                    "source_anchor": "Page 70",
                    "text": "8.6 诊断会话说明",
                    "table_count": 0,
                },
            ]
        )

        blocks = _extract_section_blocks(entries)

        self.assertEqual(
            [(row["section_code"], row["section_title"]) for row in blocks],
            [
                ("8.5", "一般 UDS 服务否定应答判断流程"),
                ("8.6", "诊断会话说明"),
            ],
        )
        self.assertEqual(
            [entry["text"] for entry in blocks[0]["entries"]],
            ["8.5 一般 UDS 服务否定应答判断流程", "子功能是否支持"],
        )
        self.assertEqual(
            [entry["text"] for entry in blocks[1]["entries"]],
            ["8.6 诊断会话说明"],
        )

    def test_bom_bridge_artifact_does_not_swallow_real_section_headers(self) -> None:
        self.assertFalse(_is_bom_bridge_artifact("1.3 图例"))
        self.assertFalse(_is_bom_bridge_artifact("2 功能分配"))
        self.assertTrue(_is_bom_bridge_artifact("0.0 左后蓄能器"))

    def test_terminal_sentence_top_level_clause_still_splits_large_block(self) -> None:
        block = _block("3.2.2", "产品详细描述", 8, text="3.2.2 产品详细描述")
        block["entries"] = [
            {"text": "3.2.2 产品详细描述", "source_page": 8, "source_line_index": 1, "source_anchor": "Page 8"},
            {"text": "9.7）供应商在虚拟评审前两周提供仿真报告及模型", "source_page": 8, "source_line_index": 2, "source_anchor": "Page 8"},
            {"text": "10）表面处理要求以DVP为准。", "source_page": 8, "source_line_index": 3, "source_anchor": "Page 8"},
            {"text": "11）自带标准件应满足相关标准要求：", "source_page": 8, "source_line_index": 4, "source_anchor": "Page 8"},
        ]
        block["source_anchor"] = "Page 8 | Section 3.2.2 产品详细描述"

        subblocks = _split_block_by_bullets(block)

        self.assertEqual([item["lines"][0] for item in subblocks[1:]], ["9.7）供应商在虚拟评审前两周提供仿真报告及模型", "10）表面处理要求以DVP为准。", "11）自带标准件应满足相关标准要求："])
        self.assertIn("Clause 10）表面处理要求以DVP为准。", subblocks[2]["source_anchor"])

    def test_contextual_dvp_reset_clauses_split_after_deep_clause(self) -> None:
        block = _block("3.2.2", "产品详细描述", 6, text="3.2.2 产品详细描述")
        block["entries"] = [
            {"text": "3.2.2 产品详细描述", "source_page": 6, "source_line_index": 1, "source_anchor": "Page 6"},
            {"text": "11.3）冰冻要求：", "source_page": 6, "source_line_index": 46, "source_anchor": "Page 6"},
            {"text": "零部件在整车安装姿态下，100bar水压冲洗5个循环后，进入-30℃低温舱8小时，冰冻后零部件无功能损失及性能下降；", "source_page": 6, "source_line_index": 47, "source_anchor": "Page 6"},
            {"text": "12、零部件需满足GB/T 26988 汽车部件可回收利用性标识要求；", "source_page": 6, "source_line_index": 48, "source_anchor": "Page 6"},
            {"text": "13、试验大纲内涉及强化耐久项目，每轮虚拟评审前两周，供应商需提供仿真报告及模型。", "source_page": 6, "source_line_index": 49, "source_anchor": "Page 6"},
            {"text": "14、电气部件低压连接器需满足Q/JQ4009 车用电线束连接器技术要求，或USCAR-2、LV214汽车连接器规范，如果采用其他车规", "source_page": 6, "source_line_index": 50, "source_anchor": "Page 6"},
            {"text": "连接器标准，需提供相关规范标准进行评审。", "source_page": 6, "source_line_index": 51, "source_anchor": "Page 6"},
            {"text": "15、供应商应配合完成减振器能耗优化的工作。", "source_page": 6, "source_line_index": 52, "source_anchor": "Page 6"},
        ]
        block["source_anchor"] = "Page 6 | Section 3.2.2 产品详细描述"

        subblocks = _split_block_by_bullets(block)

        self.assertEqual(
            [item["lines"][0] for item in subblocks],
            [
                "3.2.2 产品详细描述",
                "11.3）冰冻要求：",
                "12、零部件需满足GB/T 26988 汽车部件可回收利用性标识要求；",
                "13、试验大纲内涉及强化耐久项目，每轮虚拟评审前两周，供应商需提供仿真报告及模型。",
                "14、电气部件低压连接器需满足Q/JQ4009 车用电线束连接器技术要求，或USCAR-2、LV214汽车连接器规范，如果采用其他车规",
                "15、供应商应配合完成减振器能耗优化的工作。",
            ],
        )
        self.assertIn("Clause 12 零部件需满足GB/T 26988 汽车部件可回收利用性标识要求；", subblocks[2]["source_anchor"])

    def test_comma_style_major_headings_do_not_split_without_local_clause_context(self) -> None:
        block = _block("4", "标准法规要求", 6, text="4、标准法规要求")
        block["entries"] = [
            {"text": "4、标准法规要求", "source_page": 6, "source_line_index": 1, "source_anchor": "Page 6"},
            {"text": "4.1 法规与认证", "source_page": 7, "source_line_index": 1, "source_anchor": "Page 7"},
            {"text": "5、可靠性设计与验证需求", "source_page": 8, "source_line_index": 1, "source_anchor": "Page 8"},
        ]
        block["source_anchor"] = "Page 6 | Section 4 标准法规要求"

        subblocks = _split_block_by_bullets(block)

        self.assertEqual(len(subblocks), 1)
        self.assertEqual(subblocks[0]["lines"][0], "4、标准法规要求")

    def test_preview_scopes_clause_from_contextual_anchor_without_global_comma_regex(self) -> None:
        anchor = "Page 6 | Clause 12 零部件需满足GB/T 26988 汽车部件可回收利用性标识要求； | Section 3.2.2 产品详细描述"
        rows = _split_requirement_preview_rows(
            item={
                "req_code": "REQ-0100",
                "requirement_type": "TEST",
                "priority": "LOW",
                "source_anchor": anchor,
                "source_line_index": 48,
                "source_line_index_end": 48,
            },
            source_page=6,
            source_page_end=6,
            appendix_scope=_extract_appendix_scope(anchor),
            section_code=_extract_section_code(anchor),
            section_title_hint=_extract_section_title(anchor),
            explicit_section_code=_extract_explicit_section_code(anchor),
            text="12、零部件需满足GB/T 26988 汽车部件可回收利用性标识要求；",
            req_order_start=99,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["section_code"], "12")
        self.assertEqual(rows[0]["section_key"], "3.2.2::12")
        self.assertEqual(rows[0]["section_title"], "零部件需满足GB/T 26988 汽车部件可回收利用性标识要求；")

    def test_embedded_local_clause_headers_are_split_before_section_blocking(self) -> None:
        entries = _build_line_entries(
            [
                {
                    "source_page": 13,
                    "source_anchor": "Page 13 | Appendix SUB1 | Section 1.3 化学性能要求（必备）",
                    "text": "1.3 化学性能要求（必备） 1.3.1 电子电气部件，前油泵部件安装在前副车架的前转和电池包之间位置，后油泵安装在后轮转向和尾消之间，需满足GB/T 2423.18盐雾腐蚀要求。",
                    "table_count": 0,
                }
            ]
        )
        self.assertGreaterEqual(len(entries), 2)
        self.assertEqual(entries[0]["text"], "1.3 化学性能要求（必备）")
        self.assertTrue(entries[1]["text"].startswith("1.3.1 电子电气部件"))

    def test_contextual_child_section_header_recovers_long_prefixed_body_line(self) -> None:
        entries = _build_line_entries(
            [
                {
                    "source_page": 13,
                    "source_anchor": "Page 13",
                    "text": "1.3 化学性能要求（必备）\n1.3.1 电子电气部件，前油泵部件安装在前副车架的前转和电池包之间位置，后油泵安装在后轮转向和尾消之间，需满足GB/T 2423.18盐雾腐蚀要求（舱外：严酷度4（14天）；舱\n内：严酷度3（7天）），《GBT 28046.5-2013 道路车辆电气及电子设备的环境条件和试验第5部分：化学负荷》对应安装位置的要求。\n1.3.2 结构件",
                    "table_count": 0,
                }
            ]
        )

        blocks = _extract_section_blocks(entries)

        self.assertEqual(
            [(row["section_code"], row["section_title"]) for row in blocks],
            [
                ("1.3", "化学性能要求（必备）"),
                ("1.3.1", "电子电气部件"),
                ("1.3.2", "结构件"),
            ],
        )

    def test_contextual_child_section_header_requires_direct_parent_context(self) -> None:
        entries = _build_line_entries(
            [
                {
                    "source_page": 13,
                    "source_anchor": "Page 13",
                    "text": "1.2 机械性能要求\n1.3.1 电子电气部件，前油泵部件安装在前副车架的前转和电池包之间位置，后油泵安装在后轮转向和尾消之间，需满足GB/T 2423.18盐雾腐蚀要求（舱外：严酷度4（14天）；舱\n内：严酷度3（7天）），《GBT 28046.5-2013 道路车辆电气及电子设备的环境条件和试验第5部分：化学负荷》对应安装位置的要求。",
                    "table_count": 0,
                }
            ]
        )

        blocks = _extract_section_blocks(entries)

        self.assertEqual([(row["section_code"], row["section_title"]) for row in blocks], [("1.2", "机械性能要求")])

    def test_parse_section_header_strips_parenthesized_prefix_for_deep_children(self) -> None:
        header = _parse_section_header("3.1.2 （b）版本文件已签名")

        self.assertIsNotNone(header)
        self.assertEqual(header["code"], "3.1.2")
        self.assertEqual(header["title"], "版本文件已签名")

    def test_parenthesized_child_sequence_recovers_all_sibling_headers(self) -> None:
        entries = _build_line_entries(
            [
                {
                    "source_page": 6,
                    "source_anchor": "Page 6",
                    "text": (
                        "3.1 软件包签名方案简介\n"
                        "3.1.1 （a）版本文件未签名\n"
                        "未签名的版本文件，由各个ECU通过编译源代码生成原始的安装包和软件包版本。\n"
                        "3.1.2 （b）版本文件已签名\n"
                        "各ECU供应商自己生成非对称密钥或者对称密钥后，使用密钥对软件进行安全签名得到SigVal。\n"
                        "3.1.3 （c）已签名版本数据下载\n"
                        "已签名的版本文件的下载流程，参考《刷写规范》把已签名的文件数据下载到ECU内部。\n"
                        "3.1.4 （d）安全签名验证\n"
                        "版本文件的安全签名验证在《刷写规范》刷写流程中进行。"
                    ),
                    "table_count": 0,
                }
            ]
        )

        blocks = _extract_section_blocks(entries)

        self.assertEqual(
            [(row["section_code"], row["section_title"]) for row in blocks],
            [
                ("3.1", "软件包签名方案简介"),
                ("3.1.1", "版本文件未签名"),
                ("3.1.2", "版本文件已签名"),
                ("3.1.3", "已签名版本数据下载"),
                ("3.1.4", "安全签名验证"),
            ],
        )

    def test_modal_top_level_dot_sentence_is_not_promoted_to_section(self) -> None:
        header = _parse_section_header("5. ASC_F 需实时反馈当前油压及其有效位（ASCFL_UpPSnsr、ASCFR_UpPSnsr）")
        self.assertIsNone(header)

    def test_glossary_like_acronym_rows_stay_inside_parent_section(self) -> None:
        entries = _build_line_entries(
            [
                {
                    "source_page": 11,
                    "source_anchor": "Page 11",
                    "text": (
                        "4 缩略语\n"
                        "以下缩略语适用于本文件。\n"
                        "4.1 UDS\n"
                        "Unified Diagnostic Service（统一诊断服务）。\n"
                        "4.2 ECU\n"
                        "Electronic Control Unit（电子控制单元）。\n"
                        "5 总体要求"
                    ),
                    "table_count": 0,
                }
            ]
        )

        blocks = _extract_section_blocks(entries)

        self.assertEqual(
            [(row["section_code"], row["section_title"]) for row in blocks],
            [
                ("4", "缩略语"),
                ("5", "总体要求"),
            ],
        )

    def test_title_only_major_headings_recover_sequential_main_sections(self) -> None:
        entries = _build_line_entries(
            [
                {
                    "source_page": 10,
                    "source_anchor": "Page 10",
                    "text": (
                        "2 规范性引用文件\n"
                        "引用文件说明。\n"
                        "术语和定义\n"
                        "本文件没有需要界定的术语和定义。\n"
                        "缩略语\n"
                        "以下缩略语适用于本文件。\n"
                        "4.9 STmin\n"
                        "Minimum Separation Time（最小连续帧时间间隔）。\n"
                        "5 总体要求"
                    ),
                    "table_count": 0,
                }
            ]
        )

        blocks = _extract_section_blocks(entries)

        self.assertEqual(
            [(row["section_code"], row["section_title"]) for row in blocks],
            [
                ("2", "规范性引用文件"),
                ("3", "术语和定义"),
                ("4", "缩略语"),
                ("5", "总体要求"),
            ],
        )

    def test_preview_extracts_real_appendix_title_scope_from_anchor(self) -> None:
        anchor = "Page 29 | Appendix 测试分组 / Test Groups（推荐） | Section 1 组1"
        self.assertEqual(_extract_appendix_scope(anchor), "测试分组 / Test Groups（推荐）")

    def test_scope_catalog_maps_test_outline_titles_to_canonical_sub_ids(self) -> None:
        catalog = _build_scope_catalog(
            section_outline=[
                {
                    "appendix_scope": "",
                    "section_code": "1",
                    "section_title": "概述",
                    "source_page_start": 1,
                },
                {
                    "appendix_scope": "SUB1",
                    "section_code": "",
                    "section_title": "附件1 软件开发要求",
                    "source_page_start": 10,
                },
            ],
            appendix_markers=[],
            test_outline_markers=[
                {
                    "appendix_scope": "主动前减振器试验大纲",
                    "section_title": "主动前减振器试验大纲",
                    "source_page_start": 20,
                },
                {
                    "appendix_scope": "主动后减振器试验大纲",
                    "section_title": "主动后减振器试验大纲",
                    "source_page_start": 30,
                },
            ],
            requirements=[],
        )

        self.assertEqual(_resolve_scope_info(catalog, "")["scope_id"], "MAIN")
        self.assertEqual(_resolve_scope_info(catalog, "主动前减振器试验大纲")["scope_id"], "SUB2")
        self.assertEqual(_resolve_scope_info(catalog, "T")["scope_id"], "SUB2")
        self.assertEqual(_resolve_scope_info(catalog, "主动后减振器试验大纲")["scope_id"], "SUB3")

    def test_canonical_section_rows_add_scope_roots_and_structural_levels(self) -> None:
        sections = [
            {
                "appendix_scope": "MAIN",
                "appendix_title": "正文（Main）",
                "section_code": "1",
                "section_key": "MAIN:1",
                "section_instance_key": "MAIN:1",
                "section_title": "概述",
                "section_level": 1,
                "parent_section_code": "",
                "parent_section_key": "",
                "outline_order": 0,
                "source_page_start": 1,
                "source_page_end": 1,
                "requirement_count": 1,
            },
            {
                "appendix_scope": "SUB1",
                "appendix_title": "主动前减振器试验大纲",
                "section_code": "1",
                "section_key": "SUB1:1",
                "section_instance_key": "SUB1:1",
                "section_title": "外观检查",
                "section_level": 1,
                "parent_section_code": "",
                "parent_section_key": "",
                "outline_order": 1,
                "source_page_start": 20,
                "source_page_end": 20,
                "requirement_count": 1,
            },
        ]

        ordered, _ = _build_canonical_section_rows(sections, include_scope_roots=True)

        main_root = next(row for row in ordered if row.get("is_appendix_scope_root") and row.get("appendix_scope") == "MAIN")
        sub_root = next(row for row in ordered if row.get("is_appendix_scope_root") and row.get("appendix_scope") == "SUB1")
        main_child = next(row for row in ordered if row.get("section_key") == "MAIN:1")
        sub_child = next(row for row in ordered if row.get("section_key") == "SUB1:1")

        self.assertEqual(main_root["structural_level"], 0)
        self.assertEqual(sub_root["structural_level"], 0)
        self.assertEqual(main_child["structural_level"], 1)
        self.assertEqual(sub_child["structural_level"], 1)
        self.assertEqual(main_child["numbering_level"], 1)
        self.assertEqual(sub_child["structural_parent_section_key"], "SUB1:ROOT")

    def test_main_synthetic_sections_do_not_count_toward_fidelity(self) -> None:
        self.assertFalse(
            counts_toward_fidelity_section(
                {
                    "appendix_scope": "MAIN",
                    "section_code": "12",
                    "structural_kind": "synthetic",
                }
            )
        )
        self.assertTrue(
            counts_toward_fidelity_section(
                {
                    "appendix_scope": "SUB1",
                    "section_code": "12",
                    "structural_kind": "synthetic",
                }
            )
        )

    def test_embedded_numeric_table_values_do_not_split_into_fake_local_clauses(self) -> None:
        line = (
            "8.1.3）在下列分析工况下，油泵总成质心位移满足下表中的要求； "
            "2.5 RX -0.5 0.5 RY -2.5 2.5 RZ -0.5 0.5"
        )
        self.assertEqual(_split_embedded_local_clause_line(line), [line])

    def test_embedded_version_example_values_do_not_split_into_fake_local_clauses(self) -> None:
        line = (
            "5.1 标识内容 ECU硬件外部应对版本号进行标识。"
            " 示例：空调控制器的一个版本号标识为： SW:AC. "
            "1.16.23 ECU软件版本号 HW:C. "
            "1.3 ECU硬件版本号 1.08.A_220120_2223_00 ECU序号代码"
        )

        self.assertEqual(_split_embedded_local_clause_line(line), [line])

    def test_embedded_section_reference_values_do_not_split_into_fake_local_clauses(self) -> None:
        line = "7.6.3.2 OBD 布局 OBD 的PIN脚分布见 7.2 章节。"

        self.assertEqual(_split_embedded_local_clause_line(line), [line])

    def test_profile_heading_is_not_filtered_as_measurement_title(self) -> None:
        header = _parse_section_header("5.2 Profile 1A")

        self.assertIsNotNone(header)
        self.assertEqual(header["code"], "5.2")
        self.assertEqual(header["title"], "Profile 1A")

    def test_distant_duplicate_codes_are_kept_as_independent_instances(self) -> None:
        blocks = [
            _block("1", "概述", 1),
            _block("2", "供应商能力要求及设计责任", 2),
            _block("6", "投标说明", 10),
            _block("1", "可靠性设计目标", 13),
            _block("2", "主要设计和验证要求", 14),
        ]

        consolidated = _consolidate_section_blocks(blocks)

        self.assertEqual(len(consolidated), 5)
        self.assertEqual(
            [(row["section_code"], row["section_title"], row["source_page"], row["source_page_end"]) for row in consolidated],
            [
                ("1", "概述", 1, 1),
                ("2", "供应商能力要求及设计责任", 2, 2),
                ("6", "投标说明", 10, 10),
                ("1", "可靠性设计目标", 13, 13),
                ("2", "主要设计和验证要求", 14, 14),
            ],
        )

    def test_adjacent_duplicate_codes_with_same_title_still_merge(self) -> None:
        blocks = [
            _block("3.2.2", "产品详细描述", 3, text="3.2.2 产品详细描述"),
            _block("3.2.2", "产品详细描述", 4, text="3.2.2 产品详细描述"),
        ]

        consolidated = _consolidate_section_blocks(blocks)

        self.assertEqual(len(consolidated), 1)
        self.assertEqual(consolidated[0]["source_page"], 3)
        self.assertEqual(consolidated[0]["source_page_end"], 4)

    def test_serial_table_item_with_definition_like_title_is_not_promoted_to_section(self) -> None:
        header = {
            "code": "2",
            "title": "油口接头接口根据需求进行定义",
            "level": 1,
            "punct": "",
        }
        self.assertTrue(
            _is_serial_table_row_candidate("2 油口接头接口根据需求进行定义", header)
        )

    def test_duplicate_outline_codes_attach_requirement_group_only_once(self) -> None:
        requirements = [
            {
                "req_code": "REQ-0032",
                "entry_id": "REQ-0032",
                "entry_kind": "REQUIREMENT",
                "appendix_scope": "",
                "section_code": "2.1",
                "section_key": "2.1",
                "section_instance_key": "2.1",
                "section_level": 2,
                "parent_section_code": "2",
                "source_page": 12,
                "source_page_end": 12,
                "source_line_index": 1,
                "source_line_index_end": 1,
                "source_anchor": "Page 12 | Section 2.1 特殊特性清单",
                "section_title": "特殊特性清单",
                "requirement_text": "2.1 特殊特性清单\n|序号|特性名称|特性类型|特性规范|",
                "req_order": 32,
            }
        ]
        sections = [
            {
                "appendix_scope": "",
                "section_code": "2.1",
                "section_key": "2.1",
                "section_instance_key": "2.1",
                "section_title": "供应商能力要求",
                "section_level": 2,
                "parent_section_code": "2",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 1,
                "source_page_end": 1,
                "source_line_index_start": 1,
                "source_line_index_end": 1,
                "is_appendix_marker": False,
                "outline_order": 8,
            },
            {
                "appendix_scope": "",
                "section_code": "2.1",
                "section_key": "2.1",
                "section_instance_key": "2.1",
                "section_title": "特殊特性清单",
                "section_level": 2,
                "parent_section_code": "2",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 12,
                "source_page_end": 12,
                "source_line_index_start": 1,
                "source_line_index_end": 1,
                "is_appendix_marker": False,
                "outline_order": 43,
            },
        ]

        entries = _build_complete_requirement_entries(requirements=requirements, sections=sections)
        req_entries = [item for item in entries if item.get("req_code") == "REQ-0032"]
        self.assertEqual(len(req_entries), 1)
        self.assertEqual(req_entries[0].get("section_title"), "特殊特性清单")

    def test_preview_section_sort_keeps_unoutlined_split_clauses_in_document_order(self) -> None:
        sections = [
            {
                "appendix_scope": "SUB1",
                "section_code": "",
                "section_key": "SUB1:@技术要求内容",
                "section_instance_key": "SUB1:@技术要求内容",
                "section_title": "技术要求内容",
                "section_level": 1,
                "parent_section_code": "",
                "has_children": True,
                "is_leaf": False,
                "source_page_start": 17,
                "source_page_end": 18,
                "source_line_index_start": 0,
                "source_line_index_end": 21,
                "is_appendix_marker": False,
                "outline_order": 69,
                "first_req_order": 135,
            },
            {
                "appendix_scope": "",
                "section_code": "7.5.2",
                "section_key": "7.5.2",
                "section_instance_key": "7.5.2",
                "section_title": "橡塑件表示按照QC/T797执行，并满足GB/T26988要求。",
                "section_level": 3,
                "parent_section_code": "7.5",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 7,
                "source_page_end": 7,
                "source_line_index_start": None,
                "source_line_index_end": None,
                "is_appendix_marker": False,
                "outline_order": None,
                "first_req_order": 40,
            },
            {
                "appendix_scope": "",
                "section_code": "8.1",
                "section_key": "8.1",
                "section_instance_key": "8.1",
                "section_title": "油泵支架隔振衬套要求",
                "section_level": 2,
                "parent_section_code": "8",
                "has_children": True,
                "is_leaf": False,
                "source_page_start": 7,
                "source_page_end": 7,
                "source_line_index_start": None,
                "source_line_index_end": None,
                "is_appendix_marker": False,
                "outline_order": None,
                "first_req_order": 42,
            },
        ]

        ordered_keys = [row["section_instance_key"] for row in sorted(sections, key=_preview_section_row_sort_key)]
        self.assertEqual(ordered_keys, ["7.5.2", "8.1", "SUB1:@技术要求内容"])

    def test_build_complete_entries_aligns_large_spanning_heading_to_first_requirement_line(self) -> None:
        sections = [
            {
                "appendix_scope": "SUB1",
                "section_code": "3",
                "section_key": "SUB1:3",
                "section_instance_key": "SUB1:3#样件数量要求",
                "section_title": "样件数量要求",
                "section_level": 1,
                "parent_section_code": "",
                "has_children": True,
                "is_leaf": False,
                "source_page_start": 13,
                "source_page_end": 13,
                "source_line_index_start": 77,
                "source_line_index_end": 78,
                "line_count": 2,
                "is_appendix_marker": False,
                "outline_order": 58,
            },
            {
                "appendix_scope": "SUB1",
                "section_code": "4",
                "section_key": "SUB1:4",
                "section_instance_key": "SUB1:4#集成验证要求",
                "section_title": "集成验证要求",
                "section_level": 1,
                "parent_section_code": "",
                "has_children": True,
                "is_leaf": False,
                "source_page_start": 13,
                "source_page_end": 15,
                "source_line_index_start": 0,
                "source_line_index_end": 84,
                "line_count": 32,
                "is_appendix_marker": False,
                "outline_order": 59,
            },
        ]
        requirements = [
            {
                "req_code": "REQ-0124",
                "entry_id": "REQ-0124",
                "appendix_scope": "SUB1",
                "section_code": "3",
                "section_key": "SUB1:3",
                "section_instance_key": "SUB1:3#样件数量要求",
                "section_title": "样件数量要求",
                "section_level": 1,
                "parent_section_code": "",
                "source_page": 13,
                "source_page_end": 13,
                "source_line_index": 77,
                "source_line_index_end": 78,
                "requirement_text": "3.样件数量要求",
                "req_order": 123,
            },
            {
                "req_code": "REQ-0125",
                "entry_id": "REQ-0125",
                "appendix_scope": "SUB1",
                "section_code": "4",
                "section_key": "SUB1:4",
                "section_instance_key": "SUB1:4#集成验证要求",
                "section_title": "集成验证要求",
                "section_level": 1,
                "parent_section_code": "",
                "source_page": 13,
                "source_page_end": 15,
                "source_line_index": 79,
                "source_line_index_end": 84,
                "requirement_text": "4.集成验证要求",
                "req_order": 124,
            },
        ]

        entries = _build_complete_requirement_entries(requirements=requirements, sections=sections)
        headings = [entry for entry in entries if entry.get("entry_kind") == "HEADING"]

        self.assertEqual([entry.get("section_title") for entry in headings], ["样件数量要求", "集成验证要求"])
        self.assertEqual(headings[1].get("source_line_index"), 79)

    def test_build_complete_entries_keeps_synthetic_split_clauses_in_req_order(self) -> None:
        sections = [
            {
                "appendix_scope": "",
                "section_code": "1",
                "section_key": "1",
                "section_instance_key": "1#油泵性能要求",
                "section_title": "油泵性能要求",
                "section_level": 1,
                "parent_section_code": "",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 3,
                "source_page_end": 3,
                "source_line_index_start": None,
                "source_line_index_end": None,
                "line_count": 0,
                "is_appendix_marker": False,
                "outline_order": None,
                "first_req_order": 15,
            },
            {
                "appendix_scope": "",
                "section_code": "2",
                "section_key": "2",
                "section_instance_key": "2#控制响应性能要求",
                "section_title": "控制响应性能要求",
                "section_level": 1,
                "parent_section_code": "",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 4,
                "source_page_end": 4,
                "source_line_index_start": None,
                "source_line_index_end": None,
                "line_count": 0,
                "is_appendix_marker": False,
                "outline_order": None,
                "first_req_order": 16,
            },
            {
                "appendix_scope": "",
                "section_code": "3",
                "section_key": "3",
                "section_instance_key": "3#油泵总成接口要求",
                "section_title": "油泵总成接口要求",
                "section_level": 1,
                "parent_section_code": "",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 4,
                "source_page_end": 4,
                "source_line_index_start": None,
                "source_line_index_end": None,
                "line_count": 0,
                "is_appendix_marker": False,
                "outline_order": None,
                "first_req_order": 17,
            },
            {
                "appendix_scope": "",
                "section_code": "4",
                "section_key": "4",
                "section_instance_key": "4#外观要求",
                "section_title": "外观要求",
                "section_level": 1,
                "parent_section_code": "",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 4,
                "source_page_end": 5,
                "source_line_index_start": None,
                "source_line_index_end": None,
                "line_count": 0,
                "is_appendix_marker": False,
                "outline_order": None,
                "first_req_order": 18,
            },
            {
                "appendix_scope": "",
                "section_code": "5",
                "section_key": "5",
                "section_instance_key": "5#油管总成性能要求",
                "section_title": "油管总成性能要求",
                "section_level": 1,
                "parent_section_code": "",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 5,
                "source_page_end": 5,
                "source_line_index_start": None,
                "source_line_index_end": None,
                "line_count": 0,
                "is_appendix_marker": False,
                "outline_order": None,
                "first_req_order": 19,
            },
            {
                "appendix_scope": "",
                "section_code": "6",
                "section_key": "6",
                "section_instance_key": "6#双电机控制器总成要求",
                "section_title": "双电机控制器总成要求",
                "section_level": 1,
                "parent_section_code": "",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 5,
                "source_page_end": 5,
                "source_line_index_start": None,
                "source_line_index_end": None,
                "line_count": 0,
                "is_appendix_marker": False,
                "outline_order": None,
                "first_req_order": 20,
            },
            {
                "appendix_scope": "",
                "section_code": "6.1",
                "section_key": "6.1",
                "section_instance_key": "6.1",
                "section_title": "简述",
                "section_level": 2,
                "parent_section_code": "6",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 5,
                "source_page_end": 5,
                "source_line_index_start": None,
                "source_line_index_end": None,
                "line_count": 0,
                "is_appendix_marker": False,
                "outline_order": None,
                "first_req_order": 21,
            },
            {
                "appendix_scope": "",
                "section_code": "6.4",
                "section_key": "6.4",
                "section_instance_key": "6.4",
                "section_title": "低压电气接口",
                "section_level": 2,
                "parent_section_code": "6",
                "has_children": False,
                "is_leaf": True,
                "source_page_start": 5,
                "source_page_end": 6,
                "source_line_index_start": None,
                "source_line_index_end": None,
                "line_count": 0,
                "is_appendix_marker": False,
                "outline_order": None,
                "first_req_order": 24,
            },
        ]
        requirements = [
            {
                "req_code": "REQ-0016",
                "entry_id": "REQ-0016",
                "appendix_scope": "",
                "section_code": "1",
                "section_key": "1",
                "section_instance_key": "1#油泵性能要求",
                "section_title": "油泵性能要求",
                "section_level": 1,
                "parent_section_code": "",
                "source_page": 3,
                "source_page_end": 3,
                "source_line_index": 0,
                "source_line_index_end": 37,
                "requirement_text": "1）油泵性能要求",
                "req_order": 15,
            },
            {
                "req_code": "REQ-0017",
                "entry_id": "REQ-0017",
                "appendix_scope": "",
                "section_code": "2",
                "section_key": "2",
                "section_instance_key": "2#控制响应性能要求",
                "section_title": "控制响应性能要求",
                "section_level": 1,
                "parent_section_code": "",
                "source_page": 4,
                "source_page_end": 4,
                "source_line_index": 0,
                "source_line_index_end": 7,
                "requirement_text": "2）控制响应性能要求",
                "req_order": 16,
            },
            {
                "req_code": "REQ-0018",
                "entry_id": "REQ-0018",
                "appendix_scope": "",
                "section_code": "3",
                "section_key": "3",
                "section_instance_key": "3#油泵总成接口要求",
                "section_title": "油泵总成接口要求",
                "section_level": 1,
                "parent_section_code": "",
                "source_page": 4,
                "source_page_end": 4,
                "source_line_index": 8,
                "source_line_index_end": 34,
                "requirement_text": "3）油泵总成接口要求",
                "req_order": 17,
            },
            {
                "req_code": "REQ-0019",
                "entry_id": "REQ-0019",
                "appendix_scope": "",
                "section_code": "4",
                "section_key": "4",
                "section_instance_key": "4#外观要求",
                "section_title": "外观要求",
                "section_level": 1,
                "parent_section_code": "",
                "source_page": 4,
                "source_page_end": 5,
                "source_line_index": 35,
                "source_line_index_end": 0,
                "requirement_text": "4）外观要求",
                "req_order": 18,
            },
            {
                "req_code": "REQ-0020",
                "entry_id": "REQ-0020",
                "appendix_scope": "",
                "section_code": "5",
                "section_key": "5",
                "section_instance_key": "5#油管总成性能要求",
                "section_title": "油管总成性能要求",
                "section_level": 1,
                "parent_section_code": "",
                "source_page": 5,
                "source_page_end": 5,
                "source_line_index": 1,
                "source_line_index_end": 8,
                "requirement_text": "5）油管总成性能要求",
                "req_order": 19,
            },
            {
                "req_code": "REQ-0021",
                "entry_id": "REQ-0021",
                "appendix_scope": "",
                "section_code": "6",
                "section_key": "6",
                "section_instance_key": "6#双电机控制器总成要求",
                "section_title": "双电机控制器总成要求",
                "section_level": 1,
                "parent_section_code": "",
                "source_page": 5,
                "source_page_end": 5,
                "source_line_index": 9,
                "source_line_index_end": 9,
                "requirement_text": "6）双电机控制器总成要求",
                "req_order": 20,
            },
            {
                "req_code": "REQ-0022",
                "entry_id": "REQ-0022",
                "appendix_scope": "",
                "section_code": "6.1",
                "section_key": "6.1",
                "section_instance_key": "6.1",
                "section_title": "简述",
                "section_level": 2,
                "parent_section_code": "6",
                "source_page": 5,
                "source_page_end": 5,
                "source_line_index": 10,
                "source_line_index_end": 19,
                "requirement_text": "6.1）简述",
                "req_order": 21,
            },
            {
                "req_code": "REQ-0025",
                "entry_id": "REQ-0025",
                "appendix_scope": "",
                "section_code": "6.4",
                "section_key": "6.4",
                "section_instance_key": "6.4",
                "section_title": "低压电气接口",
                "section_level": 2,
                "parent_section_code": "6",
                "source_page": 5,
                "source_page_end": 6,
                "source_line_index": 41,
                "source_line_index_end": 4,
                "requirement_text": "6.4）低压电气接口",
                "req_order": 24,
            },
        ]

        entries = _build_complete_requirement_entries(requirements=requirements, sections=sections)
        heading_titles = [entry.get("section_title") for entry in entries if entry.get("entry_kind") == "HEADING"]

        self.assertEqual(
            heading_titles,
            [
                "油泵性能要求",
                "控制响应性能要求",
                "油泵总成接口要求",
                "外观要求",
                "油管总成性能要求",
                "双电机控制器总成要求",
                "简述",
                "低压电气接口",
            ],
        )

    def test_appendix_scope_preserves_full_subdocument_token(self) -> None:
        self.assertEqual(
            _extract_appendix_scope("Page 16 | Appendix SUB1 | Section 4.1 OTA要求"),
            "SUB1",
        )

    def test_clause_anchor_code_and_title_override_parent_section_anchor(self) -> None:
        anchor = (
            "Page 7 | Clause 8.1.2）产品各方向解耦率及各向频率应满足下表的要求 "
            "| Section 3.2.2 产品详细描述"
        )
        self.assertEqual(_extract_section_code(anchor), "8.1.2")
        self.assertEqual(_extract_section_title(anchor), "产品各方向解耦率及各向频率应满足下表的要求")

    def test_top_level_ordered_clause_anchor_is_promoted_to_local_section_anchor(self) -> None:
        anchor = (
            "Page 10 | Clause 9）技术标书及交流过程中只接受简体中文文件，防止分歧。 "
            "| Section 6 投标说明"
        )
        self.assertEqual(_extract_section_code(anchor), "9")
        self.assertEqual(_extract_section_title(anchor), "技术标书及交流过程中只接受简体中文文件，防止分歧。")

    def test_one_dot_clause_anchor_under_deep_parent_is_promoted(self) -> None:
        anchor = (
            "Page 5 | Clause 6.1）简述 "
            "| Section 3.2.2 产品详细描述"
        )
        self.assertEqual(_extract_section_code(anchor), "6.1")
        self.assertEqual(_extract_section_title(anchor), "简述")

    def test_multiline_clause_requirement_uses_first_line_clause_code_in_preview_row(self) -> None:
        anchor = (
            "Page 7 | Clause 8.1.2）产品各方向解耦率及各向频率应满足下表的要求 "
            "| Section 3.2.2 产品详细描述"
        )
        rows = _split_requirement_preview_rows(
            item={
                "req_code": "REQ-0045",
                "source_anchor": anchor,
                "requirement_type": "",
                "priority": "",
                "source_line_index": 0,
                "source_line_index_end": 0,
            },
            source_page=7,
            source_page_end=7,
            appendix_scope="",
            section_code=_extract_section_code(anchor),
            section_title_hint=_extract_section_title(anchor),
            explicit_section_code="3.2.2",
            text=(
                "8.1.2）产品各方向解耦率及各向频率应满足下表的要求（油泵支架隔振衬套如存在小衬套，需要考虑二级隔振），油泵的刚体模态\n"
                "油泵总成刚体模态分配（整车坐标系）\n"
                "For/Aft 15 30 90"
            ),
            req_order_start=44,
        )
        self.assertEqual(rows[0]["section_code"], "8.1.2")
        self.assertEqual(rows[0]["section_key"], "3.2.2::8.1.2")
        self.assertEqual(rows[0]["structural_parent_section_code"], "8.1")
        self.assertEqual(rows[0]["structural_parent_section_key"], "3.2.2::8.1")
        self.assertEqual(rows[0]["section_label"], "8.1.2）")
        self.assertTrue(rows[0]["section_title"].startswith("产品各方向解耦率及各向频率应满足下表的要求"))

    def test_preview_rows_promote_top_level_ordered_item_to_local_section_anchor(self) -> None:
        anchor = (
            "Page 10 | Clause 10）商务报价应至少包含设计开发费用、模具夹具检具治具费用。 "
            "| Section 6 投标说明"
        )
        rows = _split_requirement_preview_rows(
            item={
                "req_code": "REQ-0104",
                "source_anchor": anchor,
                "requirement_type": "",
                "priority": "",
                "source_line_index": 0,
                "source_line_index_end": 0,
            },
            source_page=10,
            source_page_end=10,
            appendix_scope="",
            section_code=_extract_section_code(anchor),
            section_title_hint=_extract_section_title(anchor),
            explicit_section_code="6",
            text="10）商务报价应至少包含设计开发费用、模具夹具检具治具费用。",
            req_order_start=103,
        )
        self.assertEqual(rows[0]["section_code"], "10")
        self.assertEqual(rows[0]["section_key"], "6::10")
        self.assertEqual(rows[0]["structural_parent_section_code"], "6")
        self.assertEqual(rows[0]["structural_parent_section_key"], "6")
        self.assertEqual(rows[0]["section_label"], "10）")
        self.assertEqual(rows[0]["section_title"], "商务报价应至少包含设计开发费用、模具夹具检具治具费用。")

    def test_preview_rows_promote_same_code_local_clause_inside_parent_section(self) -> None:
        anchor = "Page 9 | Section 6 投标说明"
        rows = _split_requirement_preview_rows(
            item={
                "req_code": "REQ-0606",
                "source_anchor": anchor,
                "requirement_type": "",
                "priority": "",
                "source_line_index": 0,
                "source_line_index_end": 0,
            },
            source_page=9,
            source_page_end=9,
            appendix_scope="",
            section_code=_extract_section_code(anchor),
            section_title_hint=_extract_section_title(anchor),
            explicit_section_code="6",
            text="6）技术标书中应包含生产工艺部分的描述，针对技术标书中提出的技术方案，论证工艺可行性",
            req_order_start=605,
        )
        self.assertEqual(rows[0]["section_code"], "6")
        self.assertEqual(rows[0]["section_key"], "6::6")
        self.assertEqual(rows[0]["section_instance_key"], "")
        self.assertEqual(rows[0]["structural_parent_section_code"], "6")
        self.assertEqual(rows[0]["structural_parent_section_key"], "6")
        self.assertEqual(rows[0]["section_label"], "6）")
        self.assertEqual(rows[0]["owner_section_label"], "6）")
        self.assertIn("Clause 6 技术标书中应包含生产工艺部分的描述", rows[0]["source_anchor"])

    def test_attachment_directory_heading_is_preserved_as_main_body_section(self) -> None:
        header = _parse_section_header("7、附件目录")

        self.assertIsNotNone(header)
        self.assertEqual(header["code"], "7")
        self.assertEqual(header["title"], "附件目录")

    def test_attachment_title_pages_split_out_from_attachment_directory(self) -> None:
        entries = [
            {
                "text": "7、附件目录",
                "source_page": 10,
                "source_anchor": "Page 10",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 0,
            },
            {
                "text": "序号附件名称备注",
                "source_page": 10,
                "source_anchor": "Page 10",
                "table_count": 0,
                "source_line_index": 1,
                "global_line_index": 1,
            },
            {
                "text": "1 软件开发质量要求",
                "source_page": 10,
                "source_anchor": "Page 10",
                "table_count": 0,
                "source_line_index": 2,
                "global_line_index": 2,
            },
            {
                "text": "2 FMEA",
                "source_page": 10,
                "source_anchor": "Page 10",
                "table_count": 0,
                "source_line_index": 3,
                "global_line_index": 3,
            },
            {
                "text": "|Col1|Col2|软件开发要求|",
                "source_page": 11,
                "source_anchor": "Page 11",
                "table_count": 1,
                "source_line_index": 0,
                "global_line_index": 4,
            },
            {
                "text": "|SE00.001|供应商软件质量资质评估|供应商软件质量资质评估|",
                "source_page": 11,
                "source_anchor": "Page 11",
                "table_count": 1,
                "source_line_index": 1,
                "global_line_index": 5,
            },
            {
                "text": "FMEA与特殊特性",
                "source_page": 12,
                "source_anchor": "Page 12",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 6,
            },
            {
                "text": "1 FMEA要求",
                "source_page": 12,
                "source_anchor": "Page 12",
                "table_count": 0,
                "source_line_index": 1,
                "global_line_index": 7,
            },
        ]

        blocks = _extract_section_blocks(entries)
        shaped = [(row["appendix_scope"], row["section_code"], row["section_title"], row["source_page"], row["source_page_end"]) for row in blocks]

        self.assertEqual(
            shaped,
            [
                ("", "7", "附件目录", 10, 10),
                ("SUB1", "", "软件开发要求", 11, 11),
                ("SUB2", "", "FMEA与特殊特性", 12, 12),
                ("SUB2", "1", "FMEA要求", 12, 12),
            ],
        )

    def test_repeated_embedded_scope_codes_with_distinct_titles_rotate_to_new_synthetic_scope(self) -> None:
        entries = [
            {
                "text": "7、附件目录",
                "source_page": 10,
                "source_anchor": "Page 10",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 0,
            },
            {
                "text": "|Col1|Col2|软件开发要求|",
                "source_page": 11,
                "source_anchor": "Page 11",
                "table_count": 1,
                "source_line_index": 0,
                "global_line_index": 1,
            },
            {
                "text": "1 FMEA要求",
                "source_page": 12,
                "source_anchor": "Page 12",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 2,
            },
            {
                "text": "1.可靠性设计目标",
                "source_page": 13,
                "source_anchor": "Page 13",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 3,
            },
            {
                "text": "2.主要设计和验证要求",
                "source_page": 13,
                "source_anchor": "Page 13",
                "table_count": 0,
                "source_line_index": 1,
                "global_line_index": 4,
            },
        ]

        blocks = _extract_section_blocks(entries)
        shaped = [(row["appendix_scope"], row["section_code"], row["section_title"], row["source_page"], row["source_page_end"]) for row in blocks]

        self.assertEqual(
            shaped,
            [
                ("", "7", "附件目录", 10, 10),
                ("SUB1", "", "软件开发要求", 11, 11),
                ("SUB1", "1", "FMEA要求", 12, 12),
                ("SUB2", "1", "可靠性设计目标", 13, 13),
                ("SUB2", "2", "主要设计和验证要求", 13, 13),
            ],
        )

    def test_embedded_attachment_title_splits_reference_list_out_of_previous_section(self) -> None:
        entries = [
            {
                "text": "7、附件目录",
                "source_page": 10,
                "source_anchor": "Page 10",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 0,
            },
            {
                "text": "|Col1|Col2|软件开发要求|",
                "source_page": 11,
                "source_anchor": "Page 11",
                "table_count": 1,
                "source_line_index": 0,
                "global_line_index": 1,
            },
            {
                "text": "1.4 其他要求（可选，如电气负荷等）",
                "source_page": 13,
                "source_anchor": "Page 13",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 2,
            },
            {
                "text": "若部件有油漆涂层，需满足油漆性能要求。",
                "source_page": 13,
                "source_anchor": "Page 13",
                "table_count": 0,
                "source_line_index": 1,
                "global_line_index": 3,
            },
            {
                "text": "PDS表位置内容",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 4,
            },
            {
                "text": "软件刷写签名规范",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 1,
                "global_line_index": 5,
            },
            {
                "text": "刷写规范",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 2,
                "global_line_index": 6,
            },
            {
                "text": "诊断规范-UDS规范",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 3,
                "global_line_index": 7,
            },
            {
                "text": "CANCANFD规范",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 4,
                "global_line_index": 8,
            },
            {
                "text": "DOIP规范",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 5,
                "global_line_index": 9,
            },
            {
                "text": "诊断路由规范",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 6,
                "global_line_index": 10,
            },
            {
                "text": "LIN规范",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 7,
                "global_line_index": 11,
            },
            {
                "text": "27服务认证设计规范",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 8,
                "global_line_index": 12,
            },
            {
                "text": "附件4--诊断、OTA相关标准接入认证设计规范",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 9,
                "global_line_index": 13,
            },
            {
                "text": "3、诊断设计要求",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 10,
                "global_line_index": 14,
            },
        ]

        blocks = _extract_section_blocks(entries)
        shaped = [(row["appendix_scope"], row["section_code"], row["section_title"], row["source_page"], row["source_page_end"]) for row in blocks]

        self.assertEqual(
            shaped,
            [
                ("", "7", "附件目录", 10, 10),
                ("SUB1", "", "软件开发要求", 11, 11),
                ("SUB1", "1.4", "其他要求（可选，如电气负荷等）", 13, 13),
                ("SUB2", "", "附件4--诊断、OTA相关标准接入认证设计规范", 15, 15),
                ("SUB2", "3", "诊断设计要求", 15, 15),
            ],
        )

    def test_embedded_attachment_title_rotates_scope_when_upcoming_codes_repeat(self) -> None:
        entries = [
            {
                "text": "7、附件目录",
                "source_page": 10,
                "source_anchor": "Page 10",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 0,
            },
            {
                "text": "|Col1|Col2|软件开发要求|",
                "source_page": 11,
                "source_anchor": "Page 11",
                "table_count": 1,
                "source_line_index": 0,
                "global_line_index": 1,
            },
            {
                "text": "3.样件数量要求",
                "source_page": 13,
                "source_anchor": "Page 13",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 2,
            },
            {
                "text": "附件4--诊断、OTA相关标准接入认证设计规范",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 3,
            },
            {
                "text": "3、诊断设计要求",
                "source_page": 15,
                "source_anchor": "Page 15",
                "table_count": 0,
                "source_line_index": 1,
                "global_line_index": 4,
            },
        ]

        blocks = _extract_section_blocks(entries)
        shaped = [(row["appendix_scope"], row["section_code"], row["section_title"], row["source_page"], row["source_page_end"]) for row in blocks]

        self.assertEqual(
            shaped,
            [
                ("", "7", "附件目录", 10, 10),
                ("SUB1", "", "软件开发要求", 11, 11),
                ("SUB1", "3", "样件数量要求", 13, 13),
                ("SUB2", "", "附件4--诊断、OTA相关标准接入认证设计规范", 15, 15),
                ("SUB2", "3", "诊断设计要求", 15, 15),
            ],
        )

    def test_heading_anchor_title_is_preserved_for_title_only_attachment_block(self) -> None:
        self.assertEqual(
            _extract_section_title("Page 11 | Appendix SUB1 | Heading 软件开发要求"),
            "软件开发要求",
        )

        section_key = _join_title_section_key("SUB1", "软件开发要求")
        requirements = [
            {
                "req_code": "REQ-T1",
                "appendix_scope": "SUB1",
                "section_code": "",
                "section_key": section_key,
                "section_instance_key": section_key,
                "section_title": "软件开发要求",
                "source_page": 11,
            }
        ]
        sections = [
            {
                "appendix_scope": "SUB1",
                "section_code": "",
                "section_key": section_key,
                "section_instance_key": section_key,
                "section_title": "软件开发要求",
                "parent_section_code": "",
                "source_page_start": 11,
            }
        ]

        _attach_requirement_section_context(requirements, sections)

        self.assertEqual(requirements[0]["parent_section_code"], "")
        self.assertEqual(requirements[0]["parent_section_label"], "软件开发要求")

    def test_requirement_context_uses_current_section_label_instead_of_parent_heading(self) -> None:
        requirements = [
            {
                "req_code": "REQ-0016",
                "appendix_scope": "",
                "section_code": "3.2.2",
                "section_key": "3.2.2",
                "section_instance_key": "3.2.2",
                "section_title": "产品详细描述",
                "source_page": 3,
            }
        ]
        sections = [
            {
                "appendix_scope": "",
                "section_code": "3.2",
                "section_key": "3.2",
                "section_instance_key": "3.2",
                "section_title": "产品技术要求及说明",
                "parent_section_code": "3",
                "source_page_start": 2,
            },
            {
                "appendix_scope": "",
                "section_code": "3.2.2",
                "section_key": "3.2.2",
                "section_instance_key": "3.2.2",
                "section_title": "产品详细描述",
                "parent_section_code": "3.2",
                "source_page_start": 2,
            },
        ]

        _attach_requirement_section_context(requirements, sections)

        self.assertEqual(requirements[0]["parent_section_code"], "3.2.2")
        self.assertEqual(requirements[0]["parent_section_title"], "产品详细描述")
        self.assertEqual(requirements[0]["parent_section_label"], "3.2.2 产品详细描述")

    def test_requirement_context_exposes_owner_and_structural_parent_labels(self) -> None:
        requirements = [
            {
                "req_code": "REQ-0606",
                "appendix_scope": "",
                "section_code": "6",
                "section_key": "6::6",
                "section_instance_key": "6::6",
                "section_title": "技术标书中应包含生产工艺部分的描述，针对技术标书中提出的技术方案，论证工艺可行性",
                "section_label": "6）",
                "source_page": 9,
                "structural_parent_section_code": "6",
                "structural_parent_section_key": "6",
            }
        ]
        sections = [
            {
                "appendix_scope": "",
                "section_code": "6",
                "section_key": "6",
                "section_instance_key": "6",
                "section_title": "投标说明",
                "section_label": "",
                "parent_section_code": "",
                "source_page_start": 9,
            },
            {
                "appendix_scope": "",
                "section_code": "6",
                "section_key": "6::6",
                "section_instance_key": "6::6",
                "section_title": "技术标书中应包含生产工艺部分的描述，针对技术标书中提出的技术方案，论证工艺可行性",
                "section_label": "6）",
                "parent_section_code": "6",
                "parent_section_key": "6",
                "structural_parent_section_code": "6",
                "structural_parent_section_key": "6",
                "structural_parent_section_title": "投标说明",
                "structural_parent_section_label": "6",
                "source_page_start": 9,
            },
        ]

        _attach_requirement_section_context(requirements, sections)

        self.assertEqual(requirements[0]["owner_section_code"], "6")
        self.assertEqual(requirements[0]["owner_section_label"], "6）")
        self.assertEqual(requirements[0]["parent_section_code"], "6")
        self.assertEqual(requirements[0]["parent_section_label"], "6 技术标书中应包含生产工艺部分的描述，针对技术标书中提出的技术方案，论证工艺可行性")
        self.assertEqual(requirements[0]["structural_parent_section_code"], "6")
        self.assertEqual(requirements[0]["structural_parent_section_label"], "6")

    def test_sub1_requirement_context_does_not_bind_to_future_duplicate_section_code(self) -> None:
        requirements = [
            {
                "req_code": "REQ-0122",
                "appendix_scope": "SUB1",
                "section_code": "4.1",
                "section_key": "SUB1:4.1",
                "section_instance_key": "SUB1:4.1",
                "section_title": "传统ECU（基于MCU嵌入式芯片开发，没有智能操作系统和文件系统，不支持自安装）要求：",
                "source_page": 16,
            }
        ]
        sections = [
            {
                "appendix_scope": "SUB1",
                "section_code": "4.1",
                "section_key": "SUB1:4.1",
                "section_instance_key": "SUB1:4.1",
                "section_title": "传统ECU（基于MCU嵌入式芯片开发，没有智能操作系统和文件系统，不支持自安装）要求：",
                "parent_section_code": "4",
                "source_page_start": 16,
            },
            {
                "appendix_scope": "SUB1",
                "section_code": "4",
                "section_key": "SUB1:4",
                "section_instance_key": "SUB1:4",
                "section_title": "解锁力 / Unlock force： ~ 51N",
                "parent_section_code": "",
                "source_page_start": 30,
            },
        ]

        _attach_requirement_section_context(requirements, sections)

        self.assertEqual(requirements[0]["parent_section_code"], "4.1")
        self.assertEqual(
            requirements[0]["parent_section_label"],
            "4.1 传统ECU（基于MCU嵌入式芯片开发，没有智能操作系统和文件系统，不支持自安装）要求：",
        )

    def test_duplicate_outline_codes_in_same_scope_get_distinct_instance_keys(self) -> None:
        requirements = [
            {
                "req_code": "REQ-0114",
                "appendix_scope": "SUB1",
                "section_code": "1",
                "section_key": "SUB1:1",
                "section_title": "可靠性设计目标",
                "requirement_text": "1.可靠性设计目标\n满足整车使用10年24万公里及行车时长6000h的要求。",
            }
        ]
        outline = [
            {
                "appendix_scope": "SUB1",
                "section_code": "1",
                "section_key": "SUB1:1",
                "section_title": "FMEA要求",
            },
            {
                "appendix_scope": "SUB1",
                "section_code": "1",
                "section_key": "SUB1:1",
                "section_title": "可靠性设计目标",
            },
        ]

        _assign_section_instance_keys(requirements, outline)
        groups = _collect_section_instance_groups(requirements, outline)
        first_key = _resolve_outline_section_instance_key(outline[0], "SUB1:1", groups)
        second_key = _resolve_outline_section_instance_key(outline[1], "SUB1:1", groups)

        self.assertIn("#", requirements[0]["section_instance_key"])
        self.assertEqual(requirements[0]["section_instance_key"], second_key)
        self.assertNotEqual(first_key, second_key)

    def test_markdown_table_rows_with_confidential_cells_are_not_treated_as_noise(self) -> None:
        line = "|-1|供应商生产控制计划|适用 Applicable|高度机密highly confidential|●|"

        self.assertFalse(_is_line_noise(line))

    def test_appendix_standalone_table_page_starts_new_title_block(self) -> None:
        entries = [
            {
                "text": "Appendix A",
                "source_page": 25,
                "source_anchor": "Page 25",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 0,
            },
            {
                "text": "6 产品网络安全生命周期-报废/终止支持阶段",
                "source_page": 25,
                "source_anchor": "Page 25",
                "table_count": 0,
                "source_line_index": 1,
                "global_line_index": 1,
            },
            {
                "text": "|用途|Col2|常见不安全密码算法|可遗留使用密码算法|推荐使用的强密码算法|",
                "source_page": 26,
                "source_anchor": "Page 26",
                "table_count": 1,
                "source_line_index": 0,
                "global_line_index": 2,
            },
            {
                "text": "|对称加密|分组加密|Blowfish|AES-GCM|",
                "source_page": 26,
                "source_anchor": "Page 26",
                "table_count": 1,
                "source_line_index": 1,
                "global_line_index": 3,
            },
        ]

        blocks = _extract_section_blocks(entries)
        shaped = [
            (
                row["appendix_scope"],
                row["section_code"],
                row["section_title"],
                row["source_page"],
                row["source_page_end"],
            )
            for row in blocks
        ]

        self.assertEqual(
            shaped,
            [
                ("A", "6", "产品网络安全生命周期-报废/终止支持阶段", 25, 25),
                ("A", "", "常见不安全密码算法", 26, 26),
            ],
        )

    def test_appendix_table_continuation_page_does_not_start_numeric_title_block(self) -> None:
        entries = [
            {
                "text": "Appendix A",
                "source_page": 12,
                "source_anchor": "Page 12",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 0,
            },
            {
                "text": "4.集成验证要求：",
                "source_page": 13,
                "source_anchor": "Page 13",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 1,
            },
            {
                "text": "|序号|测试类别|测试小类|测试项目|",
                "source_page": 13,
                "source_anchor": "Page 13",
                "table_count": 1,
                "source_line_index": 1,
                "global_line_index": 2,
            },
            {
                "text": "|1|产品|SE或者项目组定义|系统级测试|",
                "source_page": 13,
                "source_anchor": "Page 13",
                "table_count": 1,
                "source_line_index": 2,
                "global_line_index": 3,
            },
            {
                "text": "|2|产品|SE或者项目组定义|/|",
                "source_page": 14,
                "source_anchor": "Page 14",
                "table_count": 1,
                "source_line_index": 0,
                "global_line_index": 4,
            },
            {
                "text": "|3|整车环境适应性|三高测试|高温适应性|",
                "source_page": 14,
                "source_anchor": "Page 14",
                "table_count": 1,
                "source_line_index": 1,
                "global_line_index": 5,
            },
        ]

        blocks = _extract_section_blocks(entries)
        shaped = [
            (
                row["appendix_scope"],
                row["section_code"],
                row["section_title"],
                row["source_page"],
                row["source_page_end"],
            )
            for row in blocks
        ]

        self.assertEqual(
            shaped,
            [
                ("A", "4", "集成验证要求", 13, 14),
            ],
        )

    def test_local_clause_lines_split_large_section_block_into_multiple_requirement_subblocks(self) -> None:
        block = _block("3.2.2", "产品详细描述", 3, text="3.2.2 产品详细描述")
        block["entries"] = [
            {"text": "3.2.2 产品详细描述", "source_page": 3, "source_line_index": 1, "source_anchor": "Page 3"},
            {"text": "本文件描述了主动悬架油泵系统总成的要求。", "source_page": 3, "source_line_index": 2, "source_anchor": "Page 3"},
            {"text": "1）油泵性能要求", "source_page": 3, "source_line_index": 3, "source_anchor": "Page 3"},
            {"text": "|序号|项目|要求|", "source_page": 3, "source_line_index": 4, "source_anchor": "Page 3"},
            {"text": "|1|总成重量|前＜16.1kg|", "source_page": 3, "source_line_index": 5, "source_anchor": "Page 3"},
            {"text": "2）控制响应性能要求", "source_page": 4, "source_line_index": 1, "source_anchor": "Page 4"},
            {"text": "1 转速控制精度 ±1%", "source_page": 4, "source_line_index": 2, "source_anchor": "Page 4"},
            {"text": "2 超调量 ＜5%", "source_page": 4, "source_line_index": 3, "source_anchor": "Page 4"},
            {"text": "3 延时时间 ＜1ms", "source_page": 4, "source_line_index": 4, "source_anchor": "Page 4"},
            {"text": "6.6）模式管理", "source_page": 7, "source_line_index": 1, "source_anchor": "Page 7"},
            {"text": "6.6.1）状态机", "source_page": 7, "source_line_index": 2, "source_anchor": "Page 7"},
            {"text": "6.6.2）上下电流程", "source_page": 7, "source_line_index": 3, "source_anchor": "Page 7"},
        ]
        block["source_anchor"] = "Page 3 | Section 3.2.2 产品详细描述"

        subblocks = _split_block_by_bullets(block)

        self.assertGreaterEqual(len(subblocks), 4)
        self.assertEqual(subblocks[0]["lines"][0], "3.2.2 产品详细描述")
        self.assertEqual(subblocks[1]["lines"][0], "1）油泵性能要求")
        self.assertEqual(subblocks[2]["lines"][0], "2）控制响应性能要求")
        self.assertEqual(subblocks[3]["lines"][0], "6.6）模式管理")

    def test_local_clause_heading_can_start_plain_requirement_table_without_explicit_header(self) -> None:
        lines = [
            "2）控制响应性能要求",
            "1 转速控制精度 ±1%（＞1000rpm）",
            "±10rpm（≤1000rpm） （稳定的转速-目标转速）/目标转速",
            "2 超调量 ＜5%",
            "3 延时时间 ＜1ms",
            "4 响应（上升）时间",
            "5 动态调节时间 ＜30ms",
        ]

        normalized = _normalize_table_lines(lines)

        self.assertEqual(normalized[0], "2）控制响应性能要求")
        self.assertEqual(normalized[1], "|序号|项目|要求|")
        self.assertIn("|1|转速控制精度|±1%（＞1000rpm） ±10rpm（≤1000rpm） （稳定的转速-目标转速）/目标转速|", normalized)

    def test_pipe_placeholder_heading_can_trigger_local_clause_split(self) -> None:
        block = _block("3.2.2", "产品详细描述", 3, text="3.2.2 产品详细描述")
        block["entries"] = [
            {"text": "3.2.2 产品详细描述", "source_page": 3, "source_line_index": 1, "source_anchor": "Page 3"},
            {"text": "|油泵总成参考示意图 1）油泵性能要求|Col2|Col3|Col4|Col5|", "source_page": 3, "source_line_index": 2, "source_anchor": "Page 3"},
            {"text": "|序号|项目|要求|", "source_page": 3, "source_line_index": 3, "source_anchor": "Page 3"},
            {"text": "|1|总成重量|前＜16.1kg|", "source_page": 3, "source_line_index": 4, "source_anchor": "Page 3"},
            {"text": "2）控制响应性能要求", "source_page": 4, "source_line_index": 1, "source_anchor": "Page 4"},
            {"text": "1 转速控制精度 ±1%", "source_page": 4, "source_line_index": 2, "source_anchor": "Page 4"},
        ]
        block["source_anchor"] = "Page 3 | Section 3.2.2 产品详细描述"

        subblocks = _split_block_by_bullets(block)

        self.assertGreaterEqual(len(subblocks), 3)
        self.assertIn("Clause 1）油泵性能要求", subblocks[1]["source_anchor"])
        self.assertEqual(subblocks[2]["lines"][0], "2）控制响应性能要求")

    def test_short_local_clause_heading_is_not_dropped_as_noise(self) -> None:
        text = _normalize_block_text(["|6.6.1）状态机|6.6.1）状态机|6.6.1）状态机|"])
        self.assertEqual(text, "6.6.1）状态机")

    def test_duplicate_plain_and_pipe_clause_headings_do_not_create_empty_duplicate_chunk(self) -> None:
        block = _block("3.2.2", "产品详细描述", 5, text="3.2.2 产品详细描述")
        block["entries"] = [
            {"text": "5）油管总成性能要求", "source_page": 5, "source_line_index": 1, "source_anchor": "Page 5"},
            {"text": "|5）油管总成性能要求|5）油管总成性能要求|5）油管总成性能要求|", "source_page": 5, "source_line_index": 2, "source_anchor": "Page 5"},
            {"text": "|序号|序号|项目|项目|要求|要求|", "source_page": 5, "source_line_index": 3, "source_anchor": "Page 5"},
            {"text": "|1|1|尺寸|尺寸|软管≤Φ17.6×3.8|软管≤Φ17.6×3.8|", "source_page": 5, "source_line_index": 4, "source_anchor": "Page 5"},
        ]
        block["source_anchor"] = "Page 5 | Section 3.2.2 产品详细描述"

        subblocks = _split_block_by_bullets(block)

        self.assertEqual(len(subblocks), 1)
        self.assertEqual(subblocks[0]["lines"][0], "5）油管总成性能要求")

    def test_terminal_punctuation_clause_still_splits_when_part_of_ordered_series(self) -> None:
        block = _block("6", "投标说明", 10, text="6、投标说明")
        block["entries"] = [
            {"text": "6、投标说明", "source_page": 10, "source_line_index": 1, "source_anchor": "Page 10"},
            {"text": "8）技术方案的可行性论证需要提供客观论证材料，如弄虚作假，JAC保留处罚及更换供应商的权利；", "source_page": 10, "source_line_index": 2, "source_anchor": "Page 10"},
            {"text": "9）技术标书及交流过程中只接受简体中文文件，防止分歧。", "source_page": 10, "source_line_index": 3, "source_anchor": "Page 10"},
            {"text": "10）商务报价应至少包含设计开发费用、模具夹具检具治具费用。", "source_page": 10, "source_line_index": 4, "source_anchor": "Page 10"},
        ]
        block["source_anchor"] = "Page 10 | Section 6 投标说明"

        subblocks = _split_block_by_bullets(block)
        first_lines = [item["lines"][0] for item in subblocks]

        self.assertIn("8）技术方案的可行性论证需要提供客观论证材料，如弄虚作假，JAC保留处罚及更换供应商的权利；", first_lines)
        self.assertIn("9）技术标书及交流过程中只接受简体中文文件，防止分歧。", first_lines)
        self.assertIn("10）商务报价应至少包含设计开发费用、模具夹具检具治具费用。", first_lines)

    def test_top_level_headers_accept_note_suffix_colon_suffix_and_long_bilingual_titles(self) -> None:
        note_header = _parse_section_header("2.主要设计和验证要求（注：测试工况和评价验收方案需在DVP中详细定义。）")
        colon_header = _parse_section_header("3.样件数量要求：")
        bilingual_header = _parse_section_header(
            "6 产品网络安全生命周期-报废/终止支持阶段 Product Cybersecurity Lifecycle-Decommissioning/End of Support"
        )

        self.assertIsNotNone(note_header)
        self.assertEqual(note_header["title"], "主要设计和验证要求")
        self.assertIsNotNone(colon_header)
        self.assertEqual(colon_header["title"], "样件数量要求")
        self.assertIsNotNone(bilingual_header)
        self.assertEqual(bilingual_header["code"], "6")

    def test_build_line_entries_drops_redundant_table_bridge_lines(self) -> None:
        segments = [
            {
                "source_page": 11,
                "source_anchor": "Page 11",
                "table_count": 1,
                "text": "\n".join(
                    [
                        "|SE00.010|支持与配合|1. 配合项目开展所有资源的协调，提供软件模块接口人|",
                        "1. 参考软件质量协议确定并签订指标 2. 软件交付与验收需要满足软件度量指标要求",
                        "|SE00.011|软件度量指标|1. 参考软件质量协议确定并签订指标 2. 软件交付与验收需要满足软件度量指标要求|",
                        "|2|连接器结合力、分离力及锁止装置强度|主锁锁止后保持力|3）锁止后保持力 / Retention force after locked： 110N Min|",
                        "4）解锁力 / Unlock force： ~ 51N",
                        "|2|连接器结合力、分离力及锁止装置强度|主锁解锁力|4）解锁力 / Unlock force： ~ 51N|",
                    ]
                ),
            }
        ]

        entries = _build_line_entries(segments)
        texts = [row["text"] for row in entries]

        self.assertNotIn("1. 参考软件质量协议确定并签订指标 2. 软件交付与验收需要满足软件度量指标要求", texts)
        self.assertNotIn("4）解锁力 / Unlock force： ~ 51N", texts)

    def test_build_line_entries_skips_table_of_contents_segments(self) -> None:
        segments = [
            {
                "source_page": 5,
                "source_anchor": "Page 5",
                "table_count": 0,
                "text": "\n".join(
                    [
                        "目录",
                        "8.10 读取DTC 信息ReadDTCInformation（19h） ........................................ 70",
                        "8.10.1.1 读取DTC 信息 – 通过状态掩码报告DTC 数目 .............................. 70",
                        "8.10.1.2 读取DTC 信息 – 报告DTCSnapshot 记录标识 .............................. 71",
                        "8.11 读取数据ReadDataByIdentifier（22h） ...................................... 78",
                    ]
                ),
            }
        ]

        self.assertEqual(_build_line_entries(segments), [])

    def test_shape_subblock_uses_first_page_line_index_for_ordering(self) -> None:
        shaped = _shape_subblock(
            [
                {"text": "4）外观要求", "source_page": 4, "source_line_index": 35, "source_anchor": "Page 4"},
                {"text": "—阳极氧化外观要求", "source_page": 4, "source_line_index": 36, "source_anchor": "Page 4"},
                {"text": "|4、此外观标准中只要求不可接受的项目...|Col2|Col3|", "source_page": 5, "source_line_index": 0, "source_anchor": "Page 5"},
            ],
            "Page 4 | Section 3.2.2 产品详细描述",
        )
        self.assertEqual(shaped["source_page"], 4)
        self.assertEqual(shaped["source_line_index"], 35)
        self.assertEqual(shaped["source_page_end"], 5)
        self.assertEqual(shaped["source_line_index_end"], 0)

    def test_punctuated_nested_clause_is_still_treated_as_local_heading(self) -> None:
        block = _block("3.2.2", "产品详细描述", 7, text="3.2.2 产品详细描述")
        block["entries"] = [
            {"text": "7.5）禁用物质", "source_page": 7, "source_line_index": 49, "source_anchor": "Page 7"},
            {"text": "7.5.1）除附录A中规定的在一定期限内豁免的汽车零部件和材料外...", "source_page": 7, "source_line_index": 50, "source_anchor": "Page 7"},
            {"text": "7.5.2）橡塑件表示按照QC/T797执行，并满足GB/T26988要求。", "source_page": 7, "source_line_index": 52, "source_anchor": "Page 7"},
            {"text": "8）油泵衬套要求", "source_page": 7, "source_line_index": 53, "source_anchor": "Page 7"},
        ]
        block["source_anchor"] = "Page 7 | Section 3.2.2 产品详细描述"

        subblocks = _split_block_by_bullets(block)
        first_lines = [item["lines"][0] for item in subblocks]
        self.assertIn("7.5.2）橡塑件表示按照QC/T797执行，并满足GB/T26988要求。", first_lines)

    def test_electric_test_outline_marker_uses_real_title_scope(self) -> None:
        markers = _collect_test_outline_markers(
            [
                {
                    "source_page": 21,
                    "source_anchor": "Page 21",
                    "text": "|序号 / No.|项目 / Item|测试顺序|参考标准|要求 / Requirements|",
                }
            ]
        )

        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["appendix_scope"], "试验大纲")
        self.assertEqual(markers[0]["section_title"], "试验大纲")

    def test_test_group_reference_table_does_not_emit_requirement_rows(self) -> None:
        rows = _extract_test_outline_rows(
            [
                {
                    "source_page": 18,
                    "source_anchor": "Page 18",
                    "text": "\n".join(
                        [
                            "测试分组 / Test Groups（推荐）",
                            "|1|动力总成|建议先执行|",
                            "|2|整车电器|建议随后执行|",
                        ]
                    ),
                }
            ]
        )

        self.assertEqual(rows, [])

    def test_section_consistency_marks_source_duplicate_and_missing_scope_root(self) -> None:
        consistency = analyze_section_consistency(
            [
                {
                    "appendix_scope": "SUB1",
                    "appendix_title": "主动前减振器试验大纲",
                    "section_code": "63",
                    "parent_section_code": "",
                    "section_title": "试验项目A",
                    "source_page_start": 20,
                },
                {
                    "appendix_scope": "SUB1",
                    "appendix_title": "主动前减振器试验大纲",
                    "section_code": "63",
                    "parent_section_code": "",
                    "section_title": "试验项目B",
                    "source_page_start": 21,
                },
                {
                    "appendix_scope": "SUB1",
                    "appendix_title": "主动前减振器试验大纲",
                    "section_code": "64",
                    "parent_section_code": "",
                    "section_title": "试验项目C",
                    "source_page_start": 22,
                },
            ]
        )

        issue_types = {item["issue_type"] for item in consistency["issues"]}
        self.assertIn("source_faithful_duplicate_code", issue_types)
        self.assertIn("missing_scope_root", issue_types)
        self.assertEqual(consistency["source_duplicate_code_count"], 1)
        self.assertEqual(consistency["missing_scope_root_count"], 1)


if __name__ == "__main__":
    unittest.main()
