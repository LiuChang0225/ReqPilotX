import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import parser


class ParserPageSelectionTests(unittest.TestCase):
    def test_prefers_alternate_engine_when_chosen_page_ends_with_heading_only(self) -> None:
        chosen = {
            "engine": "pymupdf4llm",
            "lines": [
                "|Col1|Col2|...",
                "|ASCRR_FsmResp|CAN（FD）|...|",
                "4.1.2 功能描述",
                "4.1.2.1 前置条件",
            ],
            "table_count": 1,
            "score": 1.79,
        }
        alt = {
            "engine": "pypdf",
            "lines": [
                "4.1.2 功能描述",
                "4.1.2.1 前置条件",
                "ASC_F 上高压",
                "ASC_R 上高压",
                "4.1.2.2 功能策略",
            ],
            "table_count": 0,
            "score": 1.14,
        }

        self.assertTrue(parser._should_prefer_trailing_body_candidate(chosen, alt))

    def test_does_not_switch_when_page_ends_with_table_rows(self) -> None:
        chosen = {
            "engine": "pymupdf4llm",
            "lines": [
                "1 文档范围及控制方法",
                "|版本历史|Col2|Col3|",
                "|版本|更改内容|更改人|",
                "|V1.0.0|初版|李方龙|",
            ],
            "table_count": 3,
            "score": 1.29,
        }
        alt = {
            "engine": "pypdf",
            "lines": [
                "1 文档范围及控制方法",
                "1.1 版本历史",
                "版本 更改内容 更改人",
                "V1.0.0 初版 李方龙",
            ],
            "table_count": 0,
            "score": 0.72,
        }

        self.assertFalse(parser._should_prefer_trailing_body_candidate(chosen, alt))


if __name__ == "__main__":
    unittest.main()
