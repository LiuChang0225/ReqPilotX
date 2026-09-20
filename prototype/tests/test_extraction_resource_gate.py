from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.extraction_case_runner import CorpusPathError
from scripts.extraction_resource_gate import (
    RESOURCE_GATE_FAIL,
    RESOURCE_GATE_NOT_EVALUATED,
    RESOURCE_GATE_PASS,
    RESOURCE_GATE_SCHEMA_VERSION,
    RESOURCE_POLICY_SCHEMA_VERSION,
    ExtractionResourceGateRejected,
    canonical_resource_gate_sha256,
    evaluate_resource_gate,
    main,
    validate_resource_probe_report,
    write_resource_gate_report,
)
from scripts.extraction_resource_probe import (
    RESOURCE_PROBE_SCHEMA_VERSION,
    canonical_resource_report_sha256,
)


DOCUMENT_A = "DOC-SYNTHETIC-A"
DOCUMENT_B = "DOC-SYNTHETIC-B"
SOURCE_A = "a" * 64
SOURCE_B = "b" * 64


class ExtractionResourceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.corpus_root = self.root / "corpus"
        self.corpus_root.mkdir()

    def test_approved_policy_passes_on_worst_run_with_safety_margin(self) -> None:
        policy = _policy()
        reports = _cohort_reports()

        gate = evaluate_resource_gate(policy, reports)

        self.assertEqual(gate["schema_version"], RESOURCE_GATE_SCHEMA_VERSION)
        self.assertEqual(gate["status"], RESOURCE_GATE_PASS)
        self.assertTrue(gate["eligibility"]["applicable"])
        self.assertEqual(gate["blockers"], [])
        self.assertEqual(gate["inputs"]["report_count"], 6)
        self.assertEqual(
            gate["limits"]["total_wall_ms"]["effective_observed_limit"],
            1600.0,
        )
        self.assertEqual(
            gate["limits"]["total_wall_ms"]["observed_worst"],
            1350.0,
        )
        self.assertEqual(
            gate["limits"]["process_peak_rss_bytes"][
                "effective_observed_limit"
            ],
            800,
        )
        self.assertEqual(
            gate["limits"]["process_peak_rss_bytes"]["observed_worst"],
            750,
        )
        self.assertEqual(
            gate["gate_report_sha256"],
            canonical_resource_gate_sha256(gate),
        )
        self.assertFalse(gate["scope"]["mutates_serving"])
        self.assertFalse(
            gate["scope"]["sufficient_for_overall_serving_activation"]
        )
        self.assertFalse(
            gate["policy"]["approval_identity_verified_by_gate"]
        )
        self.assertFalse(
            gate["privacy"]["automatically_activates_serving"]
        )
        serialized = json.dumps(gate, ensure_ascii=False)
        self.assertNotIn("CUSTOMER SECRET", serialized)
        self.assertNotIn(str(self.root), serialized)

    def test_approved_policy_fails_when_worst_run_crosses_effective_limit(
        self,
    ) -> None:
        policy = _policy()
        policy["limits"]["process_peak_rss_bytes"] = {
            "capacity": 800,
            "minimum_headroom_ratio": 0.1,
        }
        reports = _cohort_reports()

        gate = evaluate_resource_gate(policy, reports)

        self.assertEqual(gate["status"], RESOURCE_GATE_FAIL)
        self.assertEqual(
            gate["blockers"],
            ["process_peak_rss_bytes_limit_exceeded"],
        )
        self.assertEqual(
            gate["limits"]["process_peak_rss_bytes"]["status"],
            RESOURCE_GATE_FAIL,
        )
        self.assertEqual(
            gate["limits"]["total_wall_ms"]["status"],
            RESOURCE_GATE_PASS,
        )

    def test_draft_policy_never_passes_even_when_candidate_limits_fit(
        self,
    ) -> None:
        policy = _policy(approval_status="DRAFT")

        gate = evaluate_resource_gate(policy, _cohort_reports())

        self.assertEqual(gate["status"], RESOURCE_GATE_NOT_EVALUATED)
        self.assertFalse(gate["eligibility"]["applicable"])
        self.assertEqual(
            gate["eligibility"]["blockers"],
            ["resource_policy_not_approved"],
        )
        self.assertTrue(
            gate["limits"]["process_peak_rss_bytes"][
                "observed_within_effective_limit"
            ]
        )
        self.assertEqual(
            gate["limits"]["process_peak_rss_bytes"]["status"],
            RESOURCE_GATE_NOT_EVALUATED,
        )

    def test_runtime_mismatch_and_missing_runs_remain_not_evaluated(
        self,
    ) -> None:
        reports = _cohort_reports()
        reports = [
            report
            for report in reports
            if report["document"]["document_id"] != DOCUMENT_B
        ]
        reports[0]["runtime"]["platform_system"] = "Linux"
        reports[0]["report_sha256"] = canonical_resource_report_sha256(
            reports[0]
        )

        gate = evaluate_resource_gate(_policy(), reports)

        self.assertEqual(gate["status"], RESOURCE_GATE_NOT_EVALUATED)
        self.assertEqual(
            gate["eligibility"]["blockers"],
            [
                f"minimum_runs_not_met:{DOCUMENT_B}",
                f"platform_mismatch:{DOCUMENT_A}",
            ],
        )

    def test_unstable_counts_are_not_an_applicable_resource_gate(self) -> None:
        reports = _cohort_reports()
        reports[1]["pipeline"]["source_ir"]["counts"]["nodes"] += 1
        reports[1]["report_sha256"] = canonical_resource_report_sha256(
            reports[1]
        )

        gate = evaluate_resource_gate(_policy(), reports)

        self.assertEqual(gate["status"], RESOURCE_GATE_NOT_EVALUATED)
        self.assertIn(
            f"result_counts_unstable:{DOCUMENT_A}",
            gate["eligibility"]["blockers"],
        )

    def test_tampered_or_extended_probe_report_is_rejected(self) -> None:
        report = _probe_report(
            document_id=DOCUMENT_A,
            source_sha256=SOURCE_A,
            run_number=1,
            wall_ms=1000,
            peak_rss_bytes=600,
        )
        report["customer_text"] = "CUSTOMER SECRET"
        report["report_sha256"] = canonical_resource_report_sha256(report)

        with self.assertRaisesRegex(
            ExtractionResourceGateRejected,
            "resource_gate_probe_shape_invalid",
        ):
            validate_resource_probe_report(report)

        report.pop("customer_text")
        report["measurements"]["total"]["wall_ms"] = 9999
        with self.assertRaisesRegex(
            ExtractionResourceGateRejected,
            "resource_gate_probe_hash_mismatch",
        ):
            validate_resource_probe_report(report)

        report["report_sha256"] = canonical_resource_report_sha256(report)
        with self.assertRaisesRegex(
            ExtractionResourceGateRejected,
            "resource_gate_probe_total_wall_ms_inconsistent",
        ):
            validate_resource_probe_report(report)

    def test_duplicate_probe_does_not_satisfy_minimum_runs(self) -> None:
        report = _probe_report(
            document_id=DOCUMENT_A,
            source_sha256=SOURCE_A,
            run_number=1,
            wall_ms=1000,
            peak_rss_bytes=600,
        )
        policy = _policy(documents=((DOCUMENT_A, SOURCE_A),))

        with self.assertRaisesRegex(
            ExtractionResourceGateRejected,
            "resource_gate_probe_duplicate",
        ):
            evaluate_resource_gate(policy, [report, report, report])

    def test_writer_is_non_overwriting_and_rejects_corpus_destination(
        self,
    ) -> None:
        gate = evaluate_resource_gate(_policy(), _cohort_reports())
        output_root = self.root / "reports"
        destination = output_root / "gate.json"

        written = write_resource_gate_report(
            gate,
            destination,
            corpus_root=self.corpus_root,
        )
        self.assertEqual(json.loads(written.read_text(encoding="utf-8")), gate)
        self.assertEqual(
            {path.name for path in output_root.iterdir()},
            {"gate.json"},
        )
        with self.assertRaisesRegex(
            ExtractionResourceGateRejected,
            "resource_gate_output_exists",
        ):
            write_resource_gate_report(
                gate,
                destination,
                corpus_root=self.corpus_root,
            )
        with self.assertRaises(CorpusPathError):
            write_resource_gate_report(
                gate,
                self.corpus_root / "gate.json",
                corpus_root=self.corpus_root,
            )

    def test_cli_uses_distinct_exit_codes_for_draft_and_failed_policy(
        self,
    ) -> None:
        reports = _cohort_reports()
        policy_path = self.root / "policy.json"
        report_paths: list[Path] = []
        for index, report in enumerate(reports):
            path = self.root / f"report-{index}.json"
            path.write_text(
                json.dumps(report, ensure_ascii=False),
                encoding="utf-8",
            )
            report_paths.append(path)

        draft = _policy(approval_status="DRAFT")
        policy_path.write_text(
            json.dumps(draft, ensure_ascii=False),
            encoding="utf-8",
        )
        arguments = [
            "--policy",
            str(policy_path),
            "--corpus-root",
            str(self.corpus_root),
        ]
        for path in report_paths:
            arguments.extend(["--report", str(path)])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(arguments), 4)

        failed = _policy()
        failed["limits"]["total_wall_ms"] = {
            "capacity": 1200,
            "minimum_headroom_ratio": 0.1,
        }
        policy_path.write_text(
            json.dumps(failed, ensure_ascii=False),
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(arguments), 3)


def _policy(
    *,
    approval_status: str = "APPROVED",
    documents: tuple[tuple[str, str], ...] = (
        (DOCUMENT_A, SOURCE_A),
        (DOCUMENT_B, SOURCE_B),
    ),
) -> dict[str, object]:
    approved = approval_status == "APPROVED"
    return {
        "schema_version": RESOURCE_POLICY_SCHEMA_VERSION,
        "policy_id": "synthetic-local-pilot",
        "revision": 1,
        "approval": {
            "status": approval_status,
            "authority": "DEPLOYMENT_OWNER",
            "approved_by": "synthetic-deployment-owner" if approved else None,
            "approved_at": "2026-07-25T00:00:00+00:00" if approved else None,
        },
        "deployment_profile": {
            "profile_id": "synthetic-darwin-arm64",
            "platform_system": "Darwin",
            "machine": "arm64",
            "python_version_prefix": "3.13",
        },
        "pipeline": {
            "source_ir": {
                "schema_version": "source-ir/v2",
                "adapter_id": "pdf-native",
                "adapter_version": "1.10.2",
            },
            "structure": {
                "schema_version": "structure-tree/v2",
                "resolver_id": "deterministic",
                "resolver_version": "2.19.0",
            },
        },
        "cohort": {
            "documents": [
                {
                    "document_id": document_id,
                    "source_sha256": source_sha256,
                    "minimum_runs": 3,
                }
                for document_id, source_sha256 in documents
            ]
        },
        "limits": {
            "total_wall_ms": {
                "capacity": 2000.0,
                "minimum_headroom_ratio": 0.2,
            },
            "process_peak_rss_bytes": {
                "capacity": 1000,
                "minimum_headroom_ratio": 0.2,
            },
        },
    }


def _cohort_reports() -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for document_id, source_sha256, wall_values, rss_values in (
        (
            DOCUMENT_A,
            SOURCE_A,
            (1000.0, 1100.0, 1200.0),
            (600, 650, 700),
        ),
        (
            DOCUMENT_B,
            SOURCE_B,
            (1250.0, 1300.0, 1350.0),
            (710, 730, 750),
        ),
    ):
        for run_number, (wall_ms, peak_rss_bytes) in enumerate(
            zip(wall_values, rss_values, strict=True),
            start=1,
        ):
            reports.append(
                _probe_report(
                    document_id=document_id,
                    source_sha256=source_sha256,
                    run_number=run_number,
                    wall_ms=wall_ms,
                    peak_rss_bytes=peak_rss_bytes,
                )
            )
    return reports


def _probe_report(
    *,
    document_id: str,
    source_sha256: str,
    run_number: int,
    wall_ms: float,
    peak_rss_bytes: int,
) -> dict[str, object]:
    baseline_peak = 100
    preflight_peak = max(baseline_peak, peak_rss_bytes - 400)
    source_ir_peak = max(preflight_peak, peak_rss_bytes - 200)
    preflight_metrics = _stage_metrics(
        10.0,
        previous_peak_rss_bytes=baseline_peak,
        peak_rss_bytes=preflight_peak,
    )
    source_ir_metrics = _stage_metrics(
        wall_ms - 100.0,
        previous_peak_rss_bytes=preflight_peak,
        peak_rss_bytes=source_ir_peak,
    )
    structure_metrics = _stage_metrics(
        90.0,
        previous_peak_rss_bytes=source_ir_peak,
        peak_rss_bytes=peak_rss_bytes,
    )
    total_metrics = _stage_metrics(
        wall_ms,
        previous_peak_rss_bytes=baseline_peak,
        peak_rss_bytes=peak_rss_bytes,
    )
    report: dict[str, object] = {
        "schema_version": RESOURCE_PROBE_SCHEMA_VERSION,
        "generated_at": f"2026-07-25T00:00:0{run_number}+00:00",
        "measurement_scope": {
            "pipeline": "PREFLIGHT_SOURCE_IR_STRUCTURE",
            "visual_provider": "off",
            "process_mode": "DEDICATED_CLI_PROCESS",
            "rss_semantics": "PROCESS_CUMULATIVE_HIGH_WATER_MARK",
        },
        "runtime": {
            "python_version": "3.13.5",
            "platform_system": "Darwin",
            "machine": "arm64",
        },
        "document": {
            "document_id": document_id,
            "source_sha256": source_sha256,
            "source_size_bytes": 1000,
            "page_count": 10,
            "format": "pdf",
        },
        "pipeline": {
            "source_ir": {
                "schema_version": "source-ir/v2",
                "adapter_id": "pdf-native",
                "adapter_version": "1.10.2",
                "status": "PARTIAL",
                "counts": {
                    "nodes": 100,
                    "assets": 5,
                    "diagnostics": 1,
                    "visual_text_snapshots": 0,
                },
            },
            "structure": {
                "schema_version": "structure-tree/v2",
                "resolver_id": "deterministic",
                "resolver_version": "2.19.0",
                "status": "REVIEW_REQUIRED",
                "counts": {
                    "nodes": 80,
                    "relations": 90,
                    "findings": 2,
                    "blocking_findings": 1,
                },
            },
        },
        "measurements": {
            "baseline_process_peak_rss_bytes": baseline_peak,
            "stages": {
                "preflight": preflight_metrics,
                "source_ir": source_ir_metrics,
                "structure": structure_metrics,
            },
            "total": total_metrics,
        },
        "privacy": {
            "contains_customer_text": False,
            "contains_source_path": False,
            "contains_source_filename": False,
            "contains_locators": False,
            "contains_asset_bytes": False,
            "is_fidelity_or_quality_evidence": False,
            "is_human_gold": False,
            "is_serving_activation_gate": False,
        },
    }
    report["report_sha256"] = canonical_resource_report_sha256(report)
    return report


def _stage_metrics(
    wall_ms: float,
    *,
    previous_peak_rss_bytes: int,
    peak_rss_bytes: int,
) -> dict[str, object]:
    return {
        "wall_ms": wall_ms,
        "user_cpu_ms": wall_ms * 0.8,
        "system_cpu_ms": wall_ms * 0.1,
        "cpu_ms": wall_ms * 0.9,
        "process_peak_rss_bytes_after_stage": peak_rss_bytes,
        "process_peak_rss_growth_bytes_during_stage": (
            peak_rss_bytes - previous_peak_rss_bytes
        ),
    }


if __name__ == "__main__":
    unittest.main()
