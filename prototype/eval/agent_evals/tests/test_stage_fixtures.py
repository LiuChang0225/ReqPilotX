"""Calibrate a relation grader with source identity, omission and excess links."""

from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from eval.agent_evals.contracts import digest, file_sha, save
from eval.agent_evals.micro_cases import CASES, generate
from eval.agent_evals.stage_fixtures import inventory, relation_checks, verify_seed
from eval.agent_evals.runner import prepare


class StageFixtureTests(unittest.TestCase):
    def test_retired_preparation_cannot_start_another_model_run(self):
        with self.assertRaisesRegex(ValueError, "RETIRED"):
            prepare(
                "stage-context-bounded-scope",
                "unused",
                "unused",
                "unused",
                "unused",
                "gpt-5.6-luna",
            )

    def test_v2_declares_introductory_paragraph_form_explicitly(self):
        case = next(c for c in CASES if c["key"] == "stage-context-bounded-scope-v2")
        intro = next(u for u in case["units"] if u["key"] == "conditions")
        self.assertEqual(intro["source_form"], "PARAGRAPH")
        self.assertTrue(
            all(
                u["source_form"] == "NUMBERED_CLAUSE"
                for u in case["units"]
                if u["key"] != "conditions"
            )
        )

    def candidate(self, case):
        ids = {
            u["key"]: f"unrelated-id-{i * 11}"
            for i, u in enumerate(reversed(case["units"]))
        }
        baseline = {
            "records": [
                {
                    "source_record_id": ids[u["key"]],
                    "text_bindings": [{"text": u["body"]}],
                }
                for u in case["units"]
            ]
        }
        oracle = {
            "selectors": {u["key"]: u["body"] for u in case["units"]},
            "must_link": case["must_link"],
            "must_not_link": case["must_not_link"],
        }
        annotations = {
            "records": {sid: {"context_source_ids": []} for sid in ids.values()}
        }
        for owner, context in case["must_link"]:
            annotations["records"][ids[owner]]["context_source_ids"].append(
                ids[context]
            )
        return baseline, annotations, oracle, ids

    def test_correct_and_counterexamples_for_both_cases(self):
        for case in CASES:
            baseline, annotations, oracle, ids = self.candidate(case)
            with self.subTest(case=case["key"]):
                self.assertEqual(
                    relation_checks(baseline, annotations, oracle)["verdict"], "PASS"
                )
                missing = deepcopy(annotations)
                owner, context = case["must_link"][0]
                missing["records"][ids[owner]]["context_source_ids"].remove(
                    ids[context]
                )
                self.assertEqual(
                    relation_checks(baseline, missing, oracle)["verdict"], "FAIL"
                )
                extra = deepcopy(annotations)
                owner, context = case["must_not_link"][0]
                extra["records"][ids[owner]]["context_source_ids"].append(ids[context])
                self.assertEqual(
                    relation_checks(baseline, extra, oracle)["verdict"], "FAIL"
                )
                # A valid alternative boundary needs review, not a hardcoded-ID FAIL.
                baseline["records"].append(deepcopy(baseline["records"][0]))
                self.assertEqual(
                    relation_checks(baseline, annotations, oracle)["verdict"],
                    "REVIEW_REQUIRED",
                )

    def test_original_generation_is_reproducible(self):
        with TemporaryDirectory() as temp:
            a, b = Path(temp) / "a", Path(temp) / "b"
            generate(a)
            generate(b)
            for case in CASES:
                for name in (case["filename"], "recipe.json", "oracle.json"):
                    self.assertEqual(
                        file_sha(a / case["key"] / name),
                        file_sha(b / case["key"] / name),
                    )

    def test_stage_seed_cannot_change_after_prepare(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            data = workspace / "runtime-data"
            data.mkdir(parents=True)
            (data / "source_ir.json").write_text("original capture")
            (workspace / "stage-input.md").write_text("prepared input")
            operator = root / "operator"
            save(operator / "stage-recipe.json", {"recipe": "frozen"})
            save(operator / "stage-seed.json", {"model_produced": False})
            files = inventory(workspace)
            save(operator / "stage-files.json", files)
            run = {
                "workspace": str(workspace),
                "stage_fixture": {
                    "recipe_sha256": file_sha(operator / "stage-recipe.json"),
                    "seed_sha256": file_sha(operator / "stage-seed.json"),
                    "files_sha256": digest(files),
                    "note_sha256": file_sha(workspace / "stage-input.md"),
                },
            }
            verify_seed(root, run)
            (data / "source_ir.json").write_text("changed capture")
            with self.assertRaisesRegex(ValueError, "changed"):
                verify_seed(root, run)


if __name__ == "__main__":
    unittest.main()
