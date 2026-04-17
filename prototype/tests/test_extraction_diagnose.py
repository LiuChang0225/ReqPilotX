from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import extraction_diagnose


class ExtractionDiagnoseTests(unittest.TestCase):
    def test_resolve_path_prefers_existing_cwd_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "prototype").mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=repo_root):
                resolved = extraction_diagnose._resolve_path(Path("prototype/eval/extraction_diagnose/run"))

            self.assertEqual(resolved, (repo_root / "prototype/eval/extraction_diagnose/run").resolve())

    def test_local_clause_summary_detects_missing_section_anchor(self) -> None:
        requirements = [
            {
                "appendix_scope": "",
                "section_code": "3.2.2",
                "source_page": 8,
                "source_page_end": 8,
                "requirement_text": "6）双电机控制器总成要求",
            },
            {
                "appendix_scope": "",
                "section_code": "3.2.2",
                "source_page": 8,
                "source_page_end": 8,
                "requirement_text": "7.1）寿命耐久要求",
            },
        ]
        sections = [
            {
                "appendix_scope": "",
                "section_code": "3.2.2",
                "section_instance_key": "3.2.2#1",
                "source_page_start": 2,
                "source_page_end": 8,
            },
            {
                "appendix_scope": "",
                "section_code": "7.1",
                "section_instance_key": "7.1#remote",
                "source_page_start": 13,
                "source_page_end": 13,
            },
        ]

        summary = extraction_diagnose._build_local_clause_summary(requirements=requirements, sections=sections)

        self.assertEqual(summary["page_counts"], {"8": 2})
        self.assertEqual(len(summary["parent_findings"]), 1)
        finding = summary["parent_findings"][0]
        self.assertEqual(finding["parent_section_code"], "3.2.2")
        self.assertEqual(finding["missing_section_anchor_count"], 2)
        self.assertEqual(finding["remote_section_match_count"], 1)
        self.assertEqual(finding["expected_direct_children"], ["6", "7"])

    def test_duplicate_scope_summary_tracks_instance_keys(self) -> None:
        outline = [
            {"appendix_scope": "SUB1", "section_code": "1"},
            {"appendix_scope": "SUB1", "section_code": "1"},
        ]
        sections = [
            {"appendix_scope": "SUB1", "section_code": "1", "section_instance_key": "SUB1:1#1"},
            {"appendix_scope": "SUB1", "section_code": "1", "section_instance_key": "SUB1:1#2"},
        ]
        requirements = [
            {"appendix_scope": "SUB1", "section_code": "1"},
            {"appendix_scope": "SUB1", "section_code": "1"},
            {"appendix_scope": "SUB1", "section_code": "1"},
        ]

        summary = extraction_diagnose._build_duplicate_scope_summary(
            outline=outline,
            sections=sections,
            requirements=requirements,
        )

        self.assertEqual(len(summary["duplicates"]), 1)
        duplicate = summary["duplicates"][0]
        self.assertEqual(duplicate["scoped_code"], "SUB1:1")
        self.assertEqual(duplicate["raw_duplicate_count"], 2)
        self.assertEqual(duplicate["section_instance_key_count"], 2)
        self.assertEqual(duplicate["requirement_row_count"], 3)

    def test_duplicate_scope_summary_ignores_source_faithful_duplicates(self) -> None:
        summary = extraction_diagnose._build_duplicate_scope_summary(
            outline=[
                {"appendix_scope": "MAIN", "section_code": "5.4"},
                {"appendix_scope": "MAIN", "section_code": "5.4"},
            ],
            sections=[
                {"appendix_scope": "MAIN", "section_code": "5.4", "section_instance_key": "MAIN:5.4#帧结束"},
                {"appendix_scope": "MAIN", "section_code": "5.4", "section_instance_key": "MAIN:5.4#初始值和无效值"},
            ],
            requirements=[
                {"appendix_scope": "MAIN", "section_code": "5.4"},
                {"appendix_scope": "MAIN", "section_code": "5.4"},
            ],
            section_consistency={
                "issues": [
                    {
                        "issue_type": "source_faithful_duplicate_code",
                        "appendix_scope": "MAIN",
                        "section_code": "5.4",
                    }
                ]
            },
        )

        self.assertEqual(summary["duplicates"], [])

    def test_build_findings_emits_pattern_backed_findings(self) -> None:
        pattern_library = {
            "patterns": [
                {
                    "pattern_id": "local_clause_heading_not_promoted",
                    "category": "heading_recovery",
                    "name": "Local clause heading not promoted",
                    "validation_status": "candidate",
                    "promoted": False,
                },
                {
                    "pattern_id": "raw_scoped_code_namespace_collision",
                    "category": "scope_and_namespace",
                    "name": "Raw scoped code namespace collision",
                    "validation_status": "candidate",
                    "promoted": False,
                },
            ]
        }
        evidence = {
            "local_clause_summary": {
                "parent_findings": [
                    {
                        "appendix_scope": "",
                        "parent_section_code": "3.2.2",
                        "missing_section_anchor_count": 5,
                        "remote_section_match_count": 0,
                        "sample_missing_codes": ["6", "7.1"],
                        "expected_direct_children": ["6", "7", "8"],
                    }
                ]
            },
            "duplicate_scope_summary": {
                "duplicates": [
                    {
                        "appendix_scope": "SUB1",
                        "section_code": "1",
                        "scoped_code": "SUB1:1",
                        "raw_duplicate_count": 2,
                        "section_instance_key_count": 2,
                        "requirement_row_count": 3,
                    }
                ]
            },
            "baseline_delta": {},
            "parser_diagnostics": {},
        }

        findings = extraction_diagnose.build_findings(
            doc_key="oil_pump",
            pdf_name="oil-pump.pdf",
            gate_tags=["oil-pump-benchmark"],
            issue_rollup=[],
            evidence=evidence,
            pattern_library=pattern_library,
            quality_profile_name="oil_quality",
            fidelity_profile_name="oil_fidelity",
        )

        issue_types = {item["issue_type"] for item in findings}
        self.assertIn("local_clause_section_missing", issue_types)
        self.assertIn("raw_duplicate_scoped_code", issue_types)

    def test_build_pattern_candidates_dedupes_by_pattern_and_doc(self) -> None:
        pattern_library = {
            "patterns": [
                {
                    "pattern_id": "local_clause_heading_not_promoted",
                    "category": "heading_recovery",
                    "name": "Local clause heading not promoted",
                    "validation_status": "candidate",
                    "promoted": False,
                }
            ]
        }
        findings = [
            {
                "pattern_id": "local_clause_heading_not_promoted",
                "category": "heading_recovery",
                "issue_type": "local_clause_section_missing",
                "suspected_layer": "preview",
                "recommended_fix_direction": "promote local clauses",
            },
            {
                "pattern_id": "local_clause_heading_not_promoted",
                "category": "heading_recovery",
                "issue_type": "required_code_missing",
                "suspected_layer": "extractor",
                "recommended_fix_direction": "promote local clauses",
            },
        ]

        candidates = extraction_diagnose.build_pattern_candidates(
            findings=findings,
            pattern_library=pattern_library,
            doc_key="oil_pump",
            pdf_name="oil-pump.pdf",
            gate_tags=["oil-pump-benchmark"],
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["pattern_id"], "local_clause_heading_not_promoted")
        self.assertEqual(candidates[0]["doc_key"], "oil_pump")


if __name__ == "__main__":
    unittest.main()
