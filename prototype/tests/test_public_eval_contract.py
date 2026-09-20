from __future__ import annotations

# Test isolation intentionally runs before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()


import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import jsonschema

from app.private_eval import PRIVATE_EVAL_ENV, private_eval_path
from scripts.decision_eval import simulate_decisions_from_values
from scripts.section_split_regression import evaluate_preview


ROOT = Path(__file__).resolve().parents[1]


class PublicEvalContractTests(unittest.TestCase):
    def test_structure_checks_require_an_outline_and_honor_explicit_profile(self):
        self.assertFalse(evaluate_preview({})["summary"]["passed"])
        preview = {"section_outline": [{"section_code": "1", "section_title": "Scope", "source_page_start": 1, "requirement_like": True}]}
        generic = evaluate_preview(preview)
        self.assertTrue(generic["summary"]["passed"])
        self.assertEqual(generic["summary"]["evaluation_scope"], "generic_structure_only")
        bound = evaluate_preview(preview, required_codes=["1", "2"])
        self.assertFalse(bound["summary"]["passed"])
        self.assertEqual(bound["detail"]["missing_required"], ["2"])

    def test_synthetic_values_are_predicted_without_gold_leakage(self):
        payload = json.loads((ROOT / "eval/public/decision_cases.json").read_text())
        cases = payload["cases"]
        expected = [case["expected_decision"] for case in cases]
        self.assertEqual(simulate_decisions_from_values(cases), expected)
        poisoned = copy.deepcopy(cases)
        for case in poisoned:
            case["expected_decision"] = "REJECT"
        self.assertEqual(simulate_decisions_from_values(poisoned), expected)

    def test_private_root_is_explicit_and_rejects_path_escape(self):
        with patch.dict("os.environ", {PRIVATE_EVAL_ENV: "/tmp/controlled-eval"}):
            self.assertEqual(
                private_eval_path("extraction_cases/manifest.json"),
                Path("/tmp/controlled-eval/extraction_cases/manifest.json").resolve(),
            )
            with self.assertRaises(ValueError):
                private_eval_path("../outside.json")

    def test_public_schema_contracts_remain_valid_without_private_data(self):
        for path in (ROOT / "eval/extraction_cases").glob("*.schema.json"):
            with self.subTest(schema=path.name):
                jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text()))
        jsonschema.Draft202012Validator.check_schema(
            json.loads((ROOT / "eval/extraction_cases/schema.json").read_text())
        )


if __name__ == "__main__":
    unittest.main()
