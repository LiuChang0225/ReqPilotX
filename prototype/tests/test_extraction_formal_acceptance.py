from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import hashlib
import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from scripts.extraction_case_runner import SCHEMA_VERSION
from scripts.extraction_formal_acceptance import (
    FORMAL_ACCEPTANCE_GATE_KIND,
    FORMAL_ACCEPTANCE_SCHEMA_VERSION,
    ExtractionFormalAcceptanceRejected,
    canonical_formal_acceptance_sha256,
    evaluate_extraction_formal_acceptance,
    main,
    validate_formal_acceptance_result,
    write_formal_acceptance_result,
)


class ExtractionFormalAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {
            "schema_version": "test-manifest/v1",
            "documents": {
                "DOC-A": {"required": True},
                "DOC-B": {"required": True},
            },
            "cases": {
                "CASE-A": {
                    "document_ids": ["DOC-A"],
                    "assertions": [
                        {"assertion_id": "CASE-A-A1"},
                        {"assertion_id": "CASE-A-A2"},
                    ],
                },
                "CASE-B": {
                    "document_ids": ["DOC-B"],
                    "assertions": [{"assertion_id": "CASE-B-A1"}],
                },
            },
        }
        manifest_sha256 = hashlib.sha256(
            json.dumps(
                self.manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.oracle_registry = {
            "schema_version": "test-oracles/v1",
            "oracles": {},
        }
        oracle_registry_sha256 = hashlib.sha256(
            json.dumps(
                self.oracle_registry,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.report = {
            "schema_version": SCHEMA_VERSION,
            "manifest_schema_version": "test-manifest/v1",
            "manifest_sha256": manifest_sha256,
            "oracle_registry_sha256": oracle_registry_sha256,
            "source_ir_mode": "native",
            "pdf_visual_provider": "off",
            "summary": {
                "documents": 2,
                "cases": 2,
                "assertions": 3,
                "hard_blocker_count": 0,
                "identity_gate_pass": True,
                "source_ir_observation_ok": True,
                "case_status_counts": {"PASS": 2},
                "assertion_status_counts": {"PASS": 3},
            },
            "hard_blockers": [],
            "documents": {"DOC-A": {}, "DOC-B": {}},
            "cases": {
                "CASE-A": {
                    "case_id": "CASE-A",
                    "status": "PASS",
                    "assertions": [
                        {
                            "assertion_id": "CASE-A-A1",
                            "status": "PASS",
                            "reason_code": "ok",
                        },
                        {
                            "assertion_id": "CASE-A-A2",
                            "status": "PASS",
                            "reason_code": "ok",
                        },
                    ],
                },
                "CASE-B": {
                    "case_id": "CASE-B",
                    "status": "PASS",
                    "assertions": [
                        {
                            "assertion_id": "CASE-B-A1",
                            "status": "PASS",
                            "reason_code": "ok",
                        }
                    ],
                },
            },
        }

    def evaluate(self, report: dict | None = None) -> dict:
        candidate = self.report if report is None else report
        return evaluate_extraction_formal_acceptance(
            candidate,
            manifest=self.manifest,
            oracle_registry=self.oracle_registry,
            expected_report_sha256=_canonical_sha256(candidate),
        )

    def test_all_pass_report_is_formally_accepted(self) -> None:
        result = self.evaluate()

        self.assertEqual(
            result["schema_version"],
            FORMAL_ACCEPTANCE_SCHEMA_VERSION,
        )
        self.assertEqual(result["gate_kind"], FORMAL_ACCEPTANCE_GATE_KIND)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["formal_acceptance_pass"])
        self.assertEqual(result["binding"]["acceptance_scope"], "FULL_MANIFEST")
        self.assertEqual(
            result["binding"]["report_sha256"],
            result["binding"]["expected_report_sha256"],
        )
        self.assertEqual(result["assertion_status_counts"], {"PASS": 3})
        self.assertEqual(result["non_pass_assertions"], [])
        self.assertFalse(result["privacy"]["is_human_gold"])
        self.assertFalse(result["privacy"]["is_visual_review_evidence"])
        self.assertEqual(
            result["artifact_sha256"],
            canonical_formal_acceptance_sha256(result),
        )
        self.assertEqual(validate_formal_acceptance_result(result), result)

    def test_gap_blocks_formal_acceptance_even_without_fail(self) -> None:
        report = copy.deepcopy(self.report)
        assertion = report["cases"]["CASE-B"]["assertions"][0]
        assertion["status"] = "GAP"
        assertion["reason_code"] = "external_human_review_pending"
        report["cases"]["CASE-B"]["status"] = "GAP"
        report["summary"]["case_status_counts"] = {"GAP": 1, "PASS": 1}
        report["summary"]["assertion_status_counts"] = {"GAP": 1, "PASS": 2}

        result = self.evaluate(report)

        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["formal_acceptance_pass"])
        self.assertEqual(
            result["non_pass_assertions"],
            [
                {
                    "case_id": "CASE-B",
                    "assertion_id": "CASE-B-A1",
                    "status": "GAP",
                    "reason_code": "external_human_review_pending",
                }
            ],
        )

    def test_phase_scope_is_not_supported_and_partial_report_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            evaluate_extraction_formal_acceptance(
                self.report,
                manifest=self.manifest,
                oracle_registry=self.oracle_registry,
                expected_report_sha256=_canonical_sha256(self.report),
                selected_case_ids=["CASE-A"],  # type: ignore[call-arg]
            )

        partial = copy.deepcopy(self.report)
        partial["documents"] = {"DOC-A": {}}
        partial["cases"] = {"CASE-A": partial["cases"]["CASE-A"]}
        partial["summary"].update(
            {
                "documents": 1,
                "cases": 1,
                "assertions": 2,
                "case_status_counts": {"PASS": 1},
                "assertion_status_counts": {"PASS": 2},
            }
        )
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_document_coverage_mismatch",
        ):
            self.evaluate(partial)

    def test_independent_report_hash_pin_is_mandatory_and_exact(self) -> None:
        with self.assertRaises(TypeError):
            evaluate_extraction_formal_acceptance(
                self.report,
                manifest=self.manifest,
                oracle_registry=self.oracle_registry,
            )
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_expected_report_sha256_invalid",
        ):
            evaluate_extraction_formal_acceptance(
                self.report,
                manifest=self.manifest,
                oracle_registry=self.oracle_registry,
                expected_report_sha256="not-a-sha256",
            )
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_expected_report_sha256_mismatch",
        ):
            evaluate_extraction_formal_acceptance(
                self.report,
                manifest=self.manifest,
                oracle_registry=self.oracle_registry,
                expected_report_sha256="0" * 64,
            )

    def test_cli_requires_report_pin_and_rejects_phase_case_option(self) -> None:
        base_args = [
            "--report",
            "report.json",
            "--corpus-root",
            "corpus",
            "--output",
            "formal.json",
        ]
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as missing_pin:
                main(base_args)
        self.assertEqual(missing_pin.exception.code, 2)

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as phase_option:
                main(
                    [
                        *base_args,
                        "--expected-report-sha256",
                        "0" * 64,
                        "--case-id",
                        "CASE-A",
                    ]
                )
        self.assertEqual(phase_option.exception.code, 2)

    def test_missing_case_or_assertion_fails_closed(self) -> None:
        missing_case = copy.deepcopy(self.report)
        missing_case["cases"].pop("CASE-B")
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_case_coverage_mismatch",
        ):
            self.evaluate(missing_case)

        missing_assertion = copy.deepcopy(self.report)
        missing_assertion["cases"]["CASE-A"]["assertions"].pop()
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_assertion_coverage_or_order_mismatch",
        ):
            self.evaluate(missing_assertion)

    def test_summary_or_manifest_tampering_fails_closed(self) -> None:
        summary_tamper = copy.deepcopy(self.report)
        summary_tamper["summary"]["assertion_status_counts"] = {"PASS": 2}
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_assertion_summary_mismatch",
        ):
            self.evaluate(summary_tamper)

        manifest_tamper = copy.deepcopy(self.report)
        manifest_tamper["manifest_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_manifest_sha256_mismatch",
        ):
            self.evaluate(manifest_tamper)

    def test_oracle_registry_and_hard_blockers_are_bound(self) -> None:
        result = self.evaluate()
        self.assertEqual(
            result["binding"]["oracle_registry_sha256"],
            self.report["oracle_registry_sha256"],
        )

        wrong_oracles = copy.deepcopy(self.oracle_registry)
        wrong_oracles["oracles"]["OTHER"] = {}
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_oracle_registry_sha256_mismatch",
        ):
            evaluate_extraction_formal_acceptance(
                self.report,
                manifest=self.manifest,
                oracle_registry=wrong_oracles,
                expected_report_sha256=_canonical_sha256(self.report),
            )

        hidden_blocker = copy.deepcopy(self.report)
        hidden_blocker["hard_blockers"] = [{"code": "hidden"}]
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_hard_blocker_present",
        ):
            self.evaluate(hidden_blocker)

    def test_result_validator_rejects_tampering_even_with_recomputed_hash(
        self,
    ) -> None:
        result = self.evaluate()

        stale_hash = copy.deepcopy(result)
        stale_hash["coverage"]["assertion_count"] += 1
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_result_assertion_status_count_mismatch",
        ):
            validate_formal_acceptance_result(stale_hash)

        narrowed_scope = copy.deepcopy(result)
        narrowed_scope["binding"]["acceptance_scope"] = "EXPLICIT_CASE_COHORT"
        narrowed_scope["artifact_sha256"] = canonical_formal_acceptance_sha256(
            narrowed_scope
        )
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_result_scope_invalid",
        ):
            validate_formal_acceptance_result(narrowed_scope)

        visual_claim = copy.deepcopy(result)
        visual_claim["privacy"]["is_visual_review_evidence"] = True
        visual_claim["artifact_sha256"] = canonical_formal_acceptance_sha256(
            visual_claim
        )
        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_result_privacy_is_visual_review_evidence_must_be_false",
        ):
            validate_formal_acceptance_result(visual_claim)

        with self.assertRaisesRegex(
            ExtractionFormalAcceptanceRejected,
            "formal_expected_artifact_sha256_mismatch",
        ):
            validate_formal_acceptance_result(
                result,
                expected_artifact_sha256="0" * 64,
            )

    def test_writer_validates_is_0600_external_atomic_and_non_overwriting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            output = root / "reports" / "formal.json"
            result = self.evaluate()

            written = write_formal_acceptance_result(
                result,
                output,
                corpus_root=corpus,
            )
            self.assertEqual(
                json.loads(written.read_text(encoding="utf-8")),
                result,
            )
            self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600)
            with self.assertRaisesRegex(
                ExtractionFormalAcceptanceRejected,
                "formal_output_exists",
            ):
                write_formal_acceptance_result(
                    result,
                    output,
                    corpus_root=corpus,
                )
            with self.assertRaises(ValueError):
                write_formal_acceptance_result(
                    result,
                    corpus / "formal.json",
                    corpus_root=corpus,
                )

    def test_writer_rejects_arbitrary_or_incomplete_result(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            output = root / "formal.json"
            fake = {
                "schema_version": FORMAL_ACCEPTANCE_SCHEMA_VERSION,
                "gate_kind": FORMAL_ACCEPTANCE_GATE_KIND,
                "status": "PASS",
                "formal_acceptance_pass": True,
            }

            with self.assertRaisesRegex(
                ExtractionFormalAcceptanceRejected,
                "formal_result_missing_field",
            ):
                write_formal_acceptance_result(
                    fake,
                    output,
                    corpus_root=corpus,
                )
            self.assertFalse(output.exists())


def _canonical_sha256(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    unittest.main()
