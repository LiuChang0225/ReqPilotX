import sys
import unittest


sys.path.insert(0, "/Users/liuchang/Documents/New project/worktrees/_active/extraction-optimization/prototype")

from app.services.extractor import (
    _build_line_entries,
    _extract_section_blocks,
    _is_line_noise,
    _parse_section_header,
    _consolidate_section_blocks,
    _is_serial_table_row_candidate,
    _normalize_block_text,
    _normalize_table_lines,
    _shape_subblock,
    _split_block_by_bullets,
)
from app.services.extraction_preview import _build_complete_requirement_entries
from app.services.extraction_preview import _attach_requirement_section_context
from app.services.extraction_preview import _assign_section_instance_keys
from app.services.extraction_preview import _collect_section_instance_groups
from app.services.extraction_preview import _extract_appendix_scope
from app.services.extraction_preview import _extract_section_title
from app.services.extraction_preview import _join_title_section_key
from app.services.extraction_preview import _resolve_outline_section_instance_key


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
    def test_short_series_clause_with_sentence_punctuation_is_not_dropped(self) -> None:
        self.assertEqual(
            _normalize_block_text(["10）表面处理要求以DVP为准。"]),
            "10）表面处理要求以DVP为准。",
        )

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

    def test_appendix_scope_preserves_full_subdocument_token(self) -> None:
        self.assertEqual(
            _extract_appendix_scope("Page 16 | Appendix SUB1 | Section 4.1 OTA要求"),
            "SUB1",
        )

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
                ("SUB1", "", "FMEA与特殊特性", 12, 12),
                ("SUB1", "1", "FMEA要求", 12, 12),
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
                ("SUB1", "", "附件4--诊断、OTA相关标准接入认证设计规范", 15, 15),
                ("SUB1", "3", "诊断设计要求", 15, 15),
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


if __name__ == "__main__":
    unittest.main()
