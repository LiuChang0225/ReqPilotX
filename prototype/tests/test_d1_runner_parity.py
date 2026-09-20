from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.application.agent_workflow.d1_runner_parity import (
    compare_d1_runner_receipts,
)


def _receipt(*, runner: str, visual_count: int = 3):
    return {
        "status": "PASS",
        "quality_gate_eligible": True,
        "model_id": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "schema_sha256": "a" * 64,
        "agent_kit": {
            "bundle_sha256": "b" * 64,
            "stage_context_sha256": "c" * 64,
            "runner_profile_sha256": ("d" if runner == "desktop" else "e") * 64,
        },
        "mcp": {"tool_names": ["a.tool", "b.tool"]},
        "quality": {
            "page_count": 3,
            "table_count_page_1": 1,
            "intentional_blank_pages": [3],
            "visual_text_golden_pass": True,
            "prompt_injection_capability_expansion": False,
            "visual_observation_count_page_2": visual_count,
        },
        "execution_result": {
            "source_sha256": "f" * 64,
            "requested_stages": [
                "D0_SOURCE_PREFLIGHT",
                "D1_DOCUMENT_UNDERSTANDING",
            ],
            "stage_artifacts": [
                {
                    "stage": "D0_SOURCE_PREFLIGHT",
                    "proposal_schema_id": "document-run-manifest/v2",
                    "status": "VALIDATED",
                    "payload_sha256": ("1" if runner == "desktop" else "2") * 64,
                },
                {
                    "stage": "D1_DOCUMENT_UNDERSTANDING",
                    "proposal_schema_id": "document-understanding-proposal/v2",
                    "status": "VALIDATED",
                    "payload_sha256": ("3" if runner == "desktop" else "4") * 64,
                },
            ],
        },
    }


class D1RunnerParityTests(unittest.TestCase):
    def test_structural_quality_parity_allows_generative_hash_and_count_differences(self) -> None:
        report = compare_d1_runner_receipts(
            _receipt(runner="desktop", visual_count=9),
            _receipt(runner="product", visual_count=12),
        )

        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.gap_codes, ())
        self.assertIn("proposal_evidence_and_receipt_hashes", report.allowed_difference_codes)

    def test_source_or_quality_drift_is_a_gap(self) -> None:
        product = _receipt(runner="product")
        product["execution_result"]["source_sha256"] = "0" * 64
        product["quality"]["visual_text_golden_pass"] = False

        report = compare_d1_runner_receipts(_receipt(runner="desktop"), product)

        self.assertEqual(report.status, "DIFFERENT")
        self.assertIn("source_sha256_mismatch", report.gap_codes)
        self.assertIn("quality_visual_text_mismatch", report.gap_codes)


if __name__ == "__main__":
    unittest.main()
