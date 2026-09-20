from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from scripts.table_asset_regression_gate import _gate_exit_code


class TableAssetRegressionGateTests(unittest.TestCase):
    def test_failure_or_error_is_a_failed_gate(self) -> None:
        self.assertEqual(_gate_exit_code({"failures": 1, "errors": 0, "skipped": 0}), 1)
        self.assertEqual(_gate_exit_code({"failures": 0, "errors": 1, "skipped": 0}), 1)

    def test_skipped_required_test_is_an_incomplete_gate(self) -> None:
        self.assertEqual(_gate_exit_code({"failures": 0, "errors": 0, "skipped": 1}), 2)

    def test_only_a_fully_executed_clean_run_is_green(self) -> None:
        self.assertEqual(_gate_exit_code({"failures": 0, "errors": 0, "skipped": 0}), 0)


if __name__ == "__main__":
    unittest.main()
