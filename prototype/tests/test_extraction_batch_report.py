from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import extraction_batch_report as batch_report


class ExtractionBatchReportTests(unittest.TestCase):
    def test_normalize_logical_name_strips_uuid_prefix(self) -> None:
        self.assertEqual(
            batch_report.normalize_logical_name(
                "6fd15d3fb7104ea882d544d10f32372d_产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf"
            ),
            "产品设计规格说明书PDS-DE前后油泵总成-V1.1-20251215.pdf",
        )

    def test_resolve_batch_documents_prefers_candidate_root_order_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            primary = root / "primary"
            secondary = root / "secondary"
            primary.mkdir()
            secondary.mkdir()
            primary_file = primary / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_oil-pump.pdf"
            secondary_file = secondary / "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb_oil-pump.pdf"
            primary_file.write_bytes(b"%PDF-1.4 primary")
            secondary_file.write_bytes(b"%PDF-1.4 secondary")

            resolved, missing = batch_report.resolve_batch_documents(
                {
                    "documents": [
                        {
                            "doc_key": "oil_pump",
                            "profile": "oil_profile",
                            "match_regex": "oil-pump",
                            "gate_tags": ["oil-pump-benchmark"],
                        }
                    ]
                },
                [
                    {"name": "primary", "path": primary},
                    {"name": "secondary", "path": secondary},
                ],
            )

        self.assertEqual(missing, [])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].path, primary_file.resolve())
        self.assertEqual(resolved[0].logical_name, "oil-pump.pdf")
        self.assertEqual(resolved[0].source_root, "primary")

    def test_build_issue_rollup_maps_unknown_issue_to_unmapped(self) -> None:
        taxonomy = {
            "default_category": "unmapped",
            "categories": {
                "parser_integrity": "Parser Integrity",
                "unmapped": "Unmapped",
            },
            "issue_types": {
                "strict_merge_mode_violation": "parser_integrity",
            },
        }

        rows, summary = batch_report.build_issue_rollup(
            quality_issue_rows=[
                {
                    "pdf_name": "doc-a.pdf",
                    "issue_type": "strict_merge_mode_violation",
                    "severity": "high",
                    "appendix_scope": "",
                    "section_code": "",
                    "detail": "merge mode drift",
                }
            ],
            fidelity_issue_rows=[
                {
                    "pdf_name": "doc-b.pdf",
                    "issue_type": "new_unknown_issue",
                    "severity": "medium",
                    "appendix_scope": "",
                    "section_code": "",
                    "detail": "unknown",
                }
            ],
            section_issue_rows=[],
            taxonomy=taxonomy,
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(summary["category_counts"]["parser_integrity"], 1)
        self.assertEqual(summary["category_counts"]["unmapped"], 1)
        self.assertEqual(summary["unmapped_issue_types"], ["new_unknown_issue"])

    def test_build_document_rollup_combines_quality_fidelity_and_section_split(self) -> None:
        docs = [
            batch_report.ResolvedDocument(
                doc_key="q_jly_core",
                profile="q_jly_core",
                path=Path("/tmp/q_jly.pdf"),
                logical_name="q_jly.pdf",
                source_root="shared_uploads",
                section_split=True,
                gate_tags=("regression-suite",),
            ),
            batch_report.ResolvedDocument(
                doc_key="pds_de_oil_pump_v1_1",
                profile="pds_de_oil_pump_v1_1",
                path=Path("/tmp/oil.pdf"),
                logical_name="oil.pdf",
                source_root="shared_uploads",
                section_split=False,
                gate_tags=("regression-suite", "oil-pump-benchmark"),
            ),
        ]
        quality_rows = [
            {"pdf_name": "q_jly.pdf", "checks_passed": "8", "checks_total": "8", "score_pct": "100.0"},
            {"pdf_name": "oil.pdf", "checks_passed": "10", "checks_total": "10", "score_pct": "100.0"},
        ]
        fidelity_rows = [
            {"pdf_name": "q_jly.pdf", "checks_passed": "22", "checks_total": "22", "score_pct": "100.0", "passed": "1"},
            {"pdf_name": "oil.pdf", "checks_passed": "16", "checks_total": "16", "score_pct": "100.0", "passed": "1"},
        ]
        section_split_results = [
            {
                "pdf_name": "q_jly.pdf",
                "passed": True,
                "score_pct": 100.0,
            }
        ]

        rows = batch_report.build_document_rollup(
            resolved_docs=docs,
            quality_doc_rows=quality_rows,
            fidelity_doc_rows=fidelity_rows,
            section_split_results=section_split_results,
        )

        self.assertEqual(len(rows), 2)
        q_jly_row = next(row for row in rows if row["doc_key"] == "q_jly_core")
        oil_row = next(row for row in rows if row["doc_key"] == "pds_de_oil_pump_v1_1")
        self.assertTrue(q_jly_row["quality_pass"])
        self.assertTrue(q_jly_row["fidelity_pass"])
        self.assertTrue(q_jly_row["section_split_target"])
        self.assertTrue(q_jly_row["section_split_pass"])
        self.assertTrue(oil_row["quality_pass"])
        self.assertTrue(oil_row["fidelity_pass"])
        self.assertIn("oil-pump-benchmark", oil_row["gate_tags"])

    def test_manifest_is_valid_json(self) -> None:
        manifest_path = Path(__file__).resolve().parents[1] / "eval" / "extraction_batches.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("candidate_roots", payload)
        self.assertIn("batches", payload)

    def test_resolve_candidate_roots_supports_home_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            (home / "Downloads/SERES MDA").mkdir(parents=True)
            with mock.patch("pathlib.Path.home", return_value=home):
                roots = batch_report.resolve_candidate_roots(
                    {
                        "candidate_roots": [
                            {
                                "name": "seres_downloads",
                                "root": "home",
                                "path": "Downloads/SERES MDA",
                            }
                        ]
                    },
                    shared_repo_root=Path("/tmp/shared"),
                )

        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["name"], "seres_downloads")
        self.assertEqual(roots[0]["path"], (home / "Downloads/SERES MDA").resolve())

    def test_attach_diagnosis_rollup_enriches_document_rows(self) -> None:
        rows = [
            {
                "doc_key": "pds_de_oil_pump_v1_1",
                "pdf_name": "oil.pdf",
            }
        ]

        batch_report._attach_diagnosis_rollup(
            rows,
            [
                {
                    "pdf_name": "oil.pdf",
                    "findings": [
                        {"issue_type": "local_clause_section_missing", "suspected_layer": "preview"},
                        {"issue_type": "raw_duplicate_scoped_code", "suspected_layer": "extractor"},
                    ],
                    "pattern_candidates": [{"pattern_id": "local_clause_heading_not_promoted"}],
                    "status": {"needs_human_review": True},
                }
            ],
        )

        self.assertEqual(rows[0]["diagnosis_finding_count"], 2)
        self.assertEqual(rows[0]["diagnosis_pattern_candidate_count"], 1)
        self.assertTrue(rows[0]["diagnosis_needs_human_review"])
        self.assertEqual(rows[0]["diagnosis_layers"], "extractor|preview")


if __name__ == "__main__":
    unittest.main()
