"""Exercise real public compilation/correction, not prompt wording assertions."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/reqpilot/scripts"))
from worked_source_units import generate  # noqa: E402 - explicit source-checkout test entry


class SourceUnitMethodsTests(unittest.TestCase):
    def test_public_worked_answer_preserves_source_boundaries_and_local_revision(self):
        with TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            root = Path(temporary) / "example"
            receipt = generate(ROOT, root)
            initial = json.loads((root / "initial-baseline.json").read_text())
            final = json.loads(Path(receipt["final_baseline"]).read_text())
            before = {r["source_record_id"]: r for r in initial["records"]}
            after = {r["source_record_id"]: r for r in final["records"]}
            self.assertEqual(set(before), set(after))
            changed = [sid for sid in before if before[sid] != after[sid]]
            self.assertEqual(len(changed), 1)
            bom = after[changed[0]]
            self.assertEqual(bom["node_id"], "bom")
            self.assertEqual(before[changed[0]]["requirement_kinds"], ["PROJECT"])
            self.assertEqual(bom["requirement_kinds"], ["CONSTRAINT"])
            self.assertTrue(bom["text_bindings"])
            self.assertTrue(bom["table_fragments"])
            for sid in before:
                self.assertEqual(
                    before[sid]["source_display_id"], after[sid]["source_display_id"]
                )
                self.assertEqual(
                    before[sid]["text_bindings"], after[sid]["text_bindings"]
                )
            milestones = [r for r in after.values() if r["node_id"] == "milestones"]
            self.assertEqual(len(milestones), 2)
            self.assertTrue(
                all(r["requirement_kinds"] == ["PROJECT"] for r in milestones)
            )
            tests = [r for r in after.values() if r["node_id"] == "tests"]
            self.assertEqual(len(tests), 2)
            complete = next(r for r in tests if len(r["text_bindings"]) == 2)
            self.assertEqual(len({s["page"] for s in complete["text_bindings"]}), 2)
            calls = json.loads((root / "public-calls.json").read_text())
            self.assertEqual(calls[-2]["operation_state"], "NOT_NEEDED")
            delivered = json.loads((root / calls[-1]["result_path"]).read_text())
            self.assertTrue(delivered["local_delivery_ready"])
            directory = Path(receipt["final_baseline"]).parent
            annotations = json.loads((directory / "image-annotations.json").read_text())
            self.assertTrue(
                any(
                    v.get("context_source_ids") for v in annotations["records"].values()
                )
            )
            workbook_path = Path(delivered["result"]["output_dir"]) / "matrix.xlsx"
            workbook = load_workbook(workbook_path)
            try:
                self.assertEqual(
                    workbook["_ReqPilotXMeta"]["B3"].value, final["artifact_sha256"]
                )
                text = "\n".join(
                    str(c.value)
                    for sheet in workbook
                    for row in sheet
                    for c in row
                    if c.value is not None
                )
                for original in [
                    "Concept approval",
                    "Design approval",
                    "Housing",
                    "Seal",
                    "No cracks",
                    "Variant B is excluded",
                ]:
                    self.assertIn(original, text)
                self.assertGreaterEqual(
                    sum(len(sheet._images) for sheet in workbook), 1
                )
            finally:
                workbook.close()


if __name__ == "__main__":
    unittest.main()
