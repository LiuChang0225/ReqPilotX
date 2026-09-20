import unittest
from pathlib import Path

from prototype.eval.agent_evals.host_install import pair_patch


class PairPatchTests(unittest.TestCase):
    def test_preserves_other_config_and_is_idempotent(self):
        original = "# custom\n- id: hub\n  config:\n    root: !!js process.env.ROOT\n"
        skills = Path("/tmp/path with spaces/skills")
        output = pair_patch(original, skills)
        self.assertTrue(output.startswith(original))
        self.assertEqual(output, pair_patch(output, skills))
        self.assertIn('"/tmp/path with spaces/skills"', output)

    def test_empty_profile_and_upgrade(self):
        output = pair_patch("# keep\n[]\n", Path("/v1/skills"))
        self.assertNotIn("[]", output)
        output = pair_patch(output, Path("/v2/skills"))
        self.assertNotIn("/v1/", output)
        self.assertEqual(output.count("id: reqpilot-skills"), 1)

    def test_unmanaged_conflict_is_not_overwritten(self):
        with self.assertRaises(ValueError):
            pair_patch("- id: reqpilot-skills\n", Path("/skills"))
