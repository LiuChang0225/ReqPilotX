from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest

import pymupdf

from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.adapters.agents.reference_workspace import materialize_reference_workspace


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = REPOSITORY_ROOT / "agent-kit/scripts/validate_bundle_references.py"
SCRIPT_ROOT = VALIDATOR.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
VALIDATOR_NAMESPACE = runpy.run_path(str(VALIDATOR))
validate_artifact_references = VALIDATOR_NAMESPACE["validate_artifact_references"]
LINEAGE_NAMESPACE = runpy.run_path(
    str(REPOSITORY_ROOT / "agent-kit/scripts/validate_stage_lineage.py")
)
validate_stage_lineage = LINEAGE_NAMESPACE["validate_stage_lineage"]
COVERAGE_NAMESPACE = runpy.run_path(
    str(REPOSITORY_ROOT / "agent-kit/scripts/validate_parent_source_coverage.py")
)
validate_parent_source_coverage = COVERAGE_NAMESPACE[
    "validate_parent_source_coverage"
]


class AgentKitBundleReferenceValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.corpus = self.root / "corpus"
        self.corpus.mkdir(mode=0o700)
        two_page = self.corpus / "two-page.pdf"
        build_synthetic_pdf(two_page, include_prompt_injection=True)
        self.source = self.corpus / "three-page.pdf"
        original = pymupdf.open(two_page)
        combined = pymupdf.open()
        combined.insert_pdf(original)
        combined.new_page()
        combined.save(self.source, no_new_id=True, clean=True, deflate=True, garbage=4)
        combined.close()
        original.close()
        self.source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.workspace = self.root / "workspace"
        manifest = materialize_reference_workspace(
            self.source,
            expected_source_sha256=self.source_sha256,
            document_role="SYNTHETIC_TEST",
            output_directory=self.workspace,
            corpus_root=self.corpus,
            repository_root=REPOSITORY_ROOT,
        )
        self.bundle_sha256 = str(manifest["workspace_bundle_sha256"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_resolves_current_bundle_handles_and_rejects_dangling_refs(self) -> None:
        text_item = json.loads(
            (self.workspace / "pages/text-index.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        table_index = json.loads(
            (self.workspace / "tables/index.json").read_text(encoding="utf-8")
        )["tables"][0]
        table = json.loads(
            (self.workspace / table_index["json_path"]).read_text(encoding="utf-8")
        )
        valid = self.workspace / "scratch/valid-artifact.json"
        valid.write_text(
            json.dumps(
                {
                    "page": "P0001",
                    "source_handle": text_item["source_handle"],
                    "table_token": table_index["table_token"],
                    "table_id": table_index["table_id"],
                    "cell_id": table["cells"][0]["cell_id"],
                }
            ),
            encoding="utf-8",
        )

        passed = validate_artifact_references(
            self.workspace,
            valid,
            expected_source_sha256=self.source_sha256,
            expected_bundle_sha256=self.bundle_sha256,
        )
        self.assertEqual(passed["status"], "PASS")
        self.assertEqual(passed["missing_reference_count"], 0)

        invalid = self.workspace / "scratch/invalid-artifact.json"
        invalid.write_text(
            json.dumps(
                {
                    "source_handle": "node-" + "f" * 32,
                    "cell_id": "cell-" + "e" * 32,
                }
            ),
            encoding="utf-8",
        )
        failed = validate_artifact_references(
            self.workspace,
            invalid,
            expected_source_sha256=self.source_sha256,
            expected_bundle_sha256=self.bundle_sha256,
        )

        self.assertEqual(failed["status"], "FAIL")
        self.assertEqual(failed["missing_reference_count"], 2)
        self.assertEqual(
            {item["kind"] for item in failed["missing_references"]},
            {"SOURCE_HANDLE", "CELL_ID"},
        )

    def test_stage_lineage_requires_consistent_page_and_table_ownership(self) -> None:
        source_handle = json.loads(
            (self.workspace / "pages/text-index.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )["source_handle"]
        table_tokens = [
            item["table_token"]
            for item in json.loads(
                (self.workspace / "tables/index.json").read_text(encoding="utf-8")
            )["tables"]
        ]
        d2 = self.workspace / "scratch/d2.json"
        d3 = self.workspace / "scratch/d3.json"
        d2.write_text(
            json.dumps(
                {
                    "document_units": [
                        {"unit_id": "DU1", "page_numbers": [1, 2, 3]}
                    ],
                    "logical_objects": [
                        {
                            "object_id": "LO1",
                            "unit_id": "DU1",
                            "business_role": "SOURCE_BODY",
                            "page_numbers": [1, 2],
                            "physical_table_tokens": table_tokens,
                            "title_evidence_refs": [source_handle],
                        },
                        {
                            "object_id": "LO2",
                            "unit_id": "DU1",
                            "business_role": "INTENTIONAL_BLANK",
                            "page_numbers": [3],
                            "physical_table_tokens": [],
                            "title_evidence_refs": [source_handle],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        d3.write_text(
            json.dumps(
                {
                    "parent_families": [
                        {
                            "family_id": "PF1",
                            "logical_object_ids": ["LO1", "LO2"],
                            "page_numbers": [1, 2, 3],
                            "physical_table_tokens": table_tokens,
                            "source_handle": source_handle,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        passed = validate_stage_lineage(
            self.workspace,
            d2,
            d3,
            expected_source_sha256=self.source_sha256,
            expected_bundle_sha256=self.bundle_sha256,
        )
        self.assertEqual(passed["status"], "PASS")

        invalid = json.loads(d3.read_text(encoding="utf-8"))
        invalid["parent_families"][0]["logical_object_ids"] = ["LO1"]
        d3.write_text(json.dumps(invalid), encoding="utf-8")
        failed = validate_stage_lineage(
            self.workspace,
            d2,
            d3,
            expected_source_sha256=self.source_sha256,
            expected_bundle_sha256=self.bundle_sha256,
        )
        self.assertEqual(failed["status"], "FAIL")
        self.assertIn(
            "parent_family_page_scope_conflict",
            {item["code"] for item in failed["findings"]},
        )

    def test_parent_source_coverage_rejects_nonwhitespace_omissions(self) -> None:
        text_item = json.loads(
            (self.workspace / "pages/text-index.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        source_handle = text_item["source_handle"]
        source_text = text_item["text"]
        d3 = self.workspace / "scratch/coverage-d3.json"
        d4 = self.workspace / "scratch/coverage-d4.json"
        d3.write_text(
            json.dumps(
                {
                    "parent_provisions": [
                        {"parent_id": "P1", "source_handles": [source_handle]}
                    ]
                }
            ),
            encoding="utf-8",
        )
        d4_payload = {
            "decompositions": [
                {
                    "parent_id": "P1",
                    "source_handle": source_handle,
                    "source_coverage": [
                        {
                            "source_handle": source_handle,
                            "ranges": [
                                {
                                    "start": 0,
                                    "end": len(source_text),
                                    "disposition": "CONTEXT",
                                    "target_id": "C1",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        d4.write_text(json.dumps(d4_payload), encoding="utf-8")

        passed = validate_parent_source_coverage(
            self.workspace,
            d3,
            d4,
            expected_source_sha256=self.source_sha256,
            expected_bundle_sha256=self.bundle_sha256,
        )
        self.assertEqual(passed["status"], "PASS")
        self.assertEqual(passed["exactly_covered_parent_count"], 1)

        d4_payload["decompositions"][0]["source_coverage"][0]["ranges"][0][
            "end"
        ] = max(1, len(source_text) // 2)
        d4.write_text(json.dumps(d4_payload), encoding="utf-8")
        failed = validate_parent_source_coverage(
            self.workspace,
            d3,
            d4,
            expected_source_sha256=self.source_sha256,
            expected_bundle_sha256=self.bundle_sha256,
        )
        self.assertEqual(failed["status"], "FAIL")
        self.assertIn(
            "source_nonwhitespace_gap",
            {item["code"] for item in failed["findings"]},
        )


if __name__ == "__main__":
    unittest.main()
