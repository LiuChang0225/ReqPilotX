from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class AgentEvalCompatibilityTests(unittest.TestCase):
    def test_old_imports_resolve_to_canonical_eval_sources(self) -> None:
        from evals.agent_evals import contracts as canonical
        from prototype.eval.agent_evals import contracts as compatibility

        self.assertEqual(
            Path(canonical.__file__).resolve(),
            Path(compatibility.__file__).resolve(),
        )
        self.assertTrue(
            Path(canonical.__file__).resolve().is_relative_to(ROOT / "evals")
        )


if __name__ == "__main__":
    unittest.main()
