from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from scripts.decision_eval import simulate_decisions_from_values


class DecisionEvalBaselineTests(unittest.TestCase):
    def test_complex_comparison_abstains_instead_of_copying_gold(self) -> None:
        decisions = simulate_decisions_from_values(
            [
                {
                    "supplier_value": "5 ms",
                    "requirement_value": "10 ms maximum",
                    "expected_decision": "REJECT",
                }
            ]
        )

        self.assertEqual(decisions, ["NEED_CLARIFICATION"])

    def test_equivalent_values_can_still_be_accepted(self) -> None:
        decisions = simulate_decisions_from_values(
            [
                {
                    "supplier_value": "12 V",
                    "requirement_value": "12V",
                    "expected_decision": "ACCEPT",
                }
            ]
        )

        self.assertEqual(decisions, ["ACCEPT"])


if __name__ == "__main__":
    unittest.main()
