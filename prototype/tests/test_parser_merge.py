import sys
import unittest
from unittest.mock import patch


from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.parser import _extract_clean_lines
from app.services.parser import _should_preserve_structured_table_page
from app.services.parser import _should_prefer_attachment_band_candidate
from app.services.parser import parse_document_with_diagnostics


class ParserMergeHeuristicsTests(unittest.TestCase):
    def test_parse_document_with_diagnostics_strips_surrogate_codepoints(self) -> None:
        with patch(
            "app.services.parser._parse_pdf",
            return_value=(
                "before\ud835\ud835after",
                [{"source_page": 1, "source_anchor": "P\ud8351", "text": "line\ud835text"}],
                "PDF",
                {"mode": "pdf_multi_engine_merge"},
            ),
        ):
            parsed = parse_document_with_diagnostics("surrogate.pdf")

        self.assertEqual(parsed["text"], "beforeafter")
        self.assertEqual(parsed["segments"][0]["text"], "linetext")
        self.assertEqual(parsed["segments"][0]["source_anchor"], "P1")

    def test_preserves_structured_lifecycle_table_page_over_longer_plain_text_page(self) -> None:
        chosen_lines = [
            "|-3|明确产品运维阶段的网络安全需求|供应商应明确本项目产品在运维阶段的网络安全需求|适用 Applicable|产品运维阶段网络安全需求|机密confidential|●|",
            "|6 产品网络安全生命周期-报废/终止支持阶段 Product Cybersecurity Lifecycle -Decommissioning/End Support|6 产品网络安全生命周期-报废/终止支持阶段 Product Cybersecurity Lifecycle -Decommissioning/End Support|6 产品网络安全生命周期-报废/终止支持阶段 Product Cybersecurity Lifecycle -Decommissioning/End Support|6 产品网络安全生命周期-报废/终止支持阶段 Product Cybersecurity Lifecycle -Decommissioning/End Support|",
            "|-1|终止支持引发的网络安全评估和影响分析|供应商的支持最低5年|适用 Applicable|产品终止支持的网络安全评估和影响分析报告|机密confidential|●|",
            "|-2|明确产品报废/终止支持阶段的网络安全需求|供应商应明确本项目产品在报废/终止支持阶段的网络安全需求|适用 Applicable|产品在报废/终止支持阶段的网络安全需求|机密confidential|●|",
        ]
        alt_lines = [
            "供应商应在协议有效期内对所提供的零部件/系统/功能持续提供升级更新（如有）服务，当供应商计划对本项目产品",
            "analysis result in 15 days.",
            "Maintenance of Product product(if applicable) and inform JAC, jointly discussing and deciding management measures and invloving maintenance of product",
            "parties of cybersecurity requirements in this phase.",
            "6 产品网络安全生命周期-报废/终止支持阶段 Product Cybersecurity Lifecycle-Decommissioning/End of Support",
        ]

        self.assertTrue(_should_preserve_structured_table_page(chosen_lines, alt_lines))

    def test_plain_table_without_stage_band_can_still_yield_to_more_complete_text(self) -> None:
        chosen_lines = [
            "|Item|Requirement|",
            "|A|满足要求|",
            "|B|满足要求|",
        ]
        alt_lines = [
            "1 项目要求",
            "供应商应满足项目总体要求。",
            "2 测试要求",
            "供应商应配合完成测试。",
            "3 交付要求",
            "供应商应按期交付。",
        ]

        self.assertFalse(_should_preserve_structured_table_page(chosen_lines, alt_lines))

    def test_preserves_repeated_stage_band_matrix_page_over_much_longer_plain_text_page(self) -> None:
        chosen_lines = [
            "|序号 No.|网络安全活动 Cybersecurity Activity|活动主要内容描述 Activity Description|是否适用于本项目？ Applicable for this Project?|工作产出 Work Product(WP)|工作产出保密性级别 Confidentiality Level|主责 R|批准 A|支持 S|通知 I|咨询 C|",
            "|1 总体要求 General Requirements|1 总体要求 General Requirements|1 总体要求 General Requirements|1 总体要求 General Requirements|1 总体要求 General Requirements|1 总体要求 General Requirements|1 总体要求 General Requirements|1 总体要求 General Requirements|1 总体要求 General Requirements|1 总体要求 General Requirements|1 总体要求 General Requirements|",
            "|-1|签署本协议 Signing this Agreement|在开展本项目前，供应商需与江汽集团签署本协议|适用 Applicable|本网络安全接口协议签署版|机密confidential|●|●|",
            "|-2|供应商的网络安全能力证明|在开展本项目前，供应商应向江汽集团提供能证明其网络安全能力的材料|适用 Applicable|供应商网络安全能力证明材料|机密confidential|●|●|",
            "|-3|设置供应商网络安全联络点|供应商需对本项目设定一名网络安全联络人|适用 Applicable|供应商网络安全联络人信息|机密confidential|●|●|",
            "|-4|设置江汽集团网络安全联络点|江汽集团需对本项目设定一名网络安全联络人|适用 Applicable|江汽集团网络安全联络人信息|机密confidential|●|●|",
            "|项目产品生命周期中的总体要求 General Requirements the Project Product Lifecycle|项目产品生命周期中的总体要求 General Requirements the Project Product Lifecycle|项目产品生命周期中的总体要求 General Requirements the Project Product Lifecycle|项目产品生命周期中的总体要求 General Requirements the Project Product Lifecycle|",
            "|-1|江汽集团对供应商的网络安全审计/审核|江汽集团可能委托第三方机构对本协议商定网络安全活动的完成情况进行审核评估|适用 Applicable|本协议商定的供应商工作产出材料|机密confidential|●|●|",
            "|-2|信息共享机制|供应商与江汽集团之间需就本项目的信息共享机制达成一致意见|适用 Applicable|信息共享机制|机密confidential|●|●|",
            "|-3|江汽集团网络安全测试权利|江汽集团保留对本项目的产品进行网络安全测试的权利|适用 Applicable|N/A|机密confidential|●|●|",
            "|-4|网络安全漏洞共享|供应商应持续监控本项目产品相关的网络安全漏洞|适用 Applicable|漏洞共享证明|机密confidential|●|●|",
            "|-5|网络安全漏洞处置|如发现本项目产品相关的网络安全漏洞，供应商需制定漏洞处置修复方案|适用 Applicable|漏洞处置修复方案和报告|机密confidential|●|●|",
            "|2 产品网络安全生命周期-概念设计阶段 Product Cybersecurity Lifecycle -Concept Phase|2 产品网络安全生命周期-概念设计阶段 Product Cybersecurity Lifecycle -Concept Phase|2 产品网络安全生命周期-概念设计阶段 Product Cybersecurity Lifecycle -Concept Phase|2 产品网络安全生命周期-概念设计阶段 Product Cybersecurity Lifecycle -Concept Phase|",
        ]
        alt_lines = [
            "供应商-xxxxxx 主机厂-江汽集团活动预期时间供应商对该活动意见反馈江汽集团对该活动意见反馈",
            "1 总体要求 General Requirements time",
            "1-1 项目开展前总体要求 General Requirements before the Project Launches",
            "在开展本项目前，供应商需与江汽集团签署本协议",
            "required based on the actual conditions of the project",
            "2.产品网络安全生命周期-概念设计阶段 Product Cybersecurity Lifecycle -Concept Phase",
            "2-1-1 制定总体网络安全计划",
            "2-1-2 网络安全相关功能筛选和功能定义",
            "2-1-3 零部件的威胁分析及风险评估与需求识别",
            "2-1-4 零部件网络安全需求技术方案（spec.）设计",
        ] * 6

        self.assertTrue(_should_preserve_structured_table_page(chosen_lines, alt_lines))

    def test_attachment_band_candidate_is_preferred_when_quality_is_similar(self) -> None:
        chosen = {
            "lines": [
                "4 OTA设计要求",
                "4.1 传统ECU要求",
                "（1）为满足OTA相关验签算法、刷写处理时间等要求",
            ],
            "score": 1.89,
            "fragment_ratio": 0.0,
        }
        alt = {
            "lines": [
                "附件4 --OTA内容",
                "4 OTA设计要求",
                "4.1 传统ECU要求",
                "（1）为满足OTA相关验签算法、刷写处理时间等要求",
            ],
            "score": 1.85,
            "fragment_ratio": 0.0,
        }

        self.assertTrue(_should_prefer_attachment_band_candidate(chosen, alt))

    def test_extract_clean_lines_splits_inline_attachment_band_line(self) -> None:
        lines = _extract_clean_lines("附件4 --诊断要求 （9）部件带执行器，需支持0x2F服务。")
        self.assertEqual(lines, ["附件4 --诊断要求", "（9）部件带执行器，需支持0x2F服务。"])

    def test_attachment_band_candidate_prefers_cleaner_short_labels(self) -> None:
        chosen = {
            "lines": [
                "附件4--诊断、OTA相关标准接入认证设计规范",
                "附件4 --诊断要求",
                "3.1 诊断设计要求",
            ],
            "score": 1.38,
            "fragment_ratio": 0.0,
        }
        alt = {
            "lines": [
                "附件4--诊断、OTA相关标准",
                "附件4 --诊断要求",
                "3、诊断设计要求",
                "3.1 诊断设计要求",
            ],
            "score": 1.33,
            "fragment_ratio": 0.0,
        }

        self.assertTrue(_should_prefer_attachment_band_candidate(chosen, alt))
