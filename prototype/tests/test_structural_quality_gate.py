import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import structural_quality_gate


class StructuralQualityGateTests(unittest.TestCase):
    def test_main_scope_alias_matches_unscoped_required_code(self) -> None:
        outline = [
            {
                "appendix_scope": "MAIN",
                "section_code": "4.1.2",
            }
        ]

        missing = structural_quality_gate._missing_required_codes(outline, [], ["4.1.2"])

        self.assertEqual(missing, [])

    def test_source_faithful_duplicates_do_not_raise_duplicate_section_issue(self) -> None:
        result = structural_quality_gate._evaluate_document(
            pdf_name="sample.pdf",
            outline=[
                {"appendix_scope": "MAIN", "section_code": "5.4"},
                {"appendix_scope": "MAIN", "section_code": "5.4"},
            ],
            requirements=[],
            parser_diagnostics={},
            section_consistency={
                "source_duplicate_code_count": 1,
                "issues": [
                    {
                        "issue_type": "source_faithful_duplicate_code",
                        "severity": "INFO",
                        "appendix_scope": "MAIN",
                        "section_code": "5.4",
                        "detail": "duplicate_count=2; parent=5; titles=帧结束 / 初始值和无效值",
                    }
                ],
            },
            defaults={
                "max_duplicate_code_count": 0,
                "max_suspicious_heading_ratio": 1.0,
                "max_sibling_gap_alerts": 10,
                "late_low_major_page_threshold": 999,
                "min_outline_count": 0,
                "require_strict_merge_mode": False,
                "max_line_injection_count": 99,
                "max_line_reorder_count": 99,
            },
            profile=None,
        )

        issue_types = {row["issue_type"] for row in result["issue_rows"]}
        self.assertNotIn("duplicate_section_code", issue_types)
        self.assertEqual(result["doc_row"]["duplicate_code_count"], 0)
        self.assertEqual(result["doc_row"]["source_duplicate_code_count"], 1)

    def test_true_duplicate_code_issue_still_counts_against_gate(self) -> None:
        result = structural_quality_gate._evaluate_document(
            pdf_name="sample.pdf",
            outline=[
                {"appendix_scope": "MAIN", "section_code": "4"},
                {"appendix_scope": "MAIN", "section_code": "4"},
            ],
            requirements=[],
            parser_diagnostics={},
            section_consistency={
                "duplicate_code_count": 1,
                "issues": [
                    {
                        "issue_type": "duplicate_code",
                        "severity": "HIGH",
                        "appendix_scope": "MAIN",
                        "section_code": "4",
                        "detail": "duplicate_count=3; titles=技术要求",
                    }
                ],
            },
            defaults={
                "max_duplicate_code_count": 0,
                "max_suspicious_heading_ratio": 1.0,
                "max_sibling_gap_alerts": 10,
                "late_low_major_page_threshold": 999,
                "min_outline_count": 0,
                "require_strict_merge_mode": False,
                "max_line_injection_count": 99,
                "max_line_reorder_count": 99,
            },
            profile=None,
        )

        issue_rows = [row for row in result["issue_rows"] if row["issue_type"] == "duplicate_section_code"]
        self.assertEqual(len(issue_rows), 1)
        self.assertEqual(issue_rows[0]["severity"], "high")
        self.assertEqual(result["doc_row"]["duplicate_code_count"], 1)


if __name__ == "__main__":
    unittest.main()
