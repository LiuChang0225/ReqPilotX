"""Meaningful isolation, observation and source-bound grading regressions."""

from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from evals.agent_evals.contracts import (
    digest,
    file_sha,
    freeze_run,
    load_run,
    registry,
    save,
    selected_case,
)
from evals.agent_evals.runner import execute_process, prepare
from evals.agent_evals.reporting import grade, review_template, automatic, table, applicable


class SuiteTests(unittest.TestCase):
    def test_prepared_map_case_is_initial_authoring_with_no_map_score(self):
        # Exercise the supported private-registry hook with an invented case;
        # customer-specific stage identities and requested clauses are not shipped.
        import os
        case, _ = selected_case('synthetic-scoped-extraction')
        case = dict(case, case_id='synthetic-items-from-map',
            preparation='STAGE_FIXTURE', fixture_key='synthetic-items-from-map',
            fixture_sha256=digest({'synthetic': 'prepared-map'}),
            oracle_sha256=digest({'synthetic': 'source-review'}),
            rubric_ids=[r for r in case['rubric_ids'] if r != 'document_map'])
        catalogue = self.root / 'operator-cases.json'
        save(catalogue, {'schema_version':'1.0.0',
            'suite_id':'reqpilotx-agent-evals-v1', 'cases':[case]})
        with patch.dict(os.environ, {'REQPILOTX_AGENT_EVAL_CASES':str(catalogue)}):
            loaded, rubrics = selected_case('synthetic-items-from-map')
        self.assertEqual(loaded['preparation'], 'STAGE_FIXTURE')
        self.assertEqual(loaded['scope'], 'SCOPED_EXTRACTION')
        self.assertNotIn('document_map', {r['rubric_id'] for r in rubrics['rubrics']})
        self.assertEqual(loaded['source_sha256'], case['source_sha256'])

    def test_unsupported_evidence_kind_is_review_not_proven_corruption(self):
        metrics={'saved_artifacts':{'unverified_tasks':[{'task':'t'}],'error_codes':['ARTIFACT_KIND_UNSUPPORTED']}}
        self.assertEqual(automatic('artifact_integrity',{}, {}, metrics, {'later_artifacts':[]})[0],'REVIEW')
        metrics['saved_artifacts']['error_codes'].append('SAVED_ARTIFACT_HASH_MISMATCH')
        self.assertEqual(automatic('artifact_integrity',{}, {}, metrics, {'later_artifacts':[]})[0],'FAIL')
    def test_scoped_extraction_checks_remain_bound_to_frozen_rubric_wording(self):
        _, rubrics = selected_case('synthetic-scoped-extraction')
        save(self.root/'rubrics.json',rubrics)
        explicit=applicable(self.root,{'scope':'SCOPED_EXTRACTION'})
        # This source-only case excludes the derivation rubric (no Snn requested).
        self.assertEqual(len(explicit),23)
        self.assertEqual(sum(c.get('evaluator','manual')!='manual' for _,c in explicit),5)
        for r in rubrics['rubrics']:
            for c in r['checks']:
                c['applies_to']=[s for s in c['applies_to'] if s!='SCOPED_EXTRACTION']
        legacy_root=self.root/'legacy'
        legacy_root.mkdir()
        save(legacy_root/'rubrics.json',rubrics)
        before=(legacy_root/'rubrics.json').read_bytes()
        legacy=applicable(legacy_root,{'scope':'SCOPED_EXTRACTION'})
        self.assertEqual([(r['rubric_id'],c['check_id']) for r,c in explicit],[(r['rubric_id'],c['check_id']) for r,c in legacy])
        self.assertEqual((legacy_root/'rubrics.json').read_bytes(),before)
        self.assertNotIn('feedback-scope-preservation',{c['check_id'] for _,c in legacy})

    def test_no_applicable_criteria_cannot_be_an_empty_pass(self):
        save(self.root/'rubrics.json',{'rubrics':[]})
        with self.assertRaisesRegex(ValueError,'NO_APPLICABLE_RUBRIC'):
            applicable(self.root,{'scope':'SCOPED_EXTRACTION'})

    def test_host_observation_gap_is_not_zero_calls_or_model_failure(self):
        out, run, result, metrics, basis = self.run_fixture()
        result.update(
            natural_completion=None, transport_actual="CLI", tool_events_available=False
        )
        metrics["error_codes"] = ["HOST_UI_UNAVAILABLE"]
        with patch(
            "evals.agent_evals.reporting.inspect_run",
            return_value=(result, metrics, basis),
        ):
            report_path = self.root / "host-gap.json"
            report = grade(out, report_path)
        self.assertEqual(report["execution_classification"], "ENVIRONMENT_BLOCKED")
        self.assertEqual(report["transport"], "CLI")
        table([report_path], self.root / "host-gap.md")
        self.assertIn("| unavailable |", (self.root / "host-gap.md").read_text())

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_fixture(self, kind="MODEL_RUN"):
        case, rubrics = selected_case("synthetic-five-page-map")
        case = deepcopy(case)
        workspace = self.root / "workspace"
        workspace.mkdir()
        source = workspace / "original.pdf"
        source.write_bytes(b"test source")
        case["source_sha256"] = file_sha(source)
        package = workspace / ".reqpilot"
        package.mkdir()
        receipt = {"plugin_files": [], "runtime_files": []}
        receipt["bundle_sha256"] = digest(receipt)
        save(package / "build-receipt.json", receipt)
        out = self.root / "run"
        out.mkdir()
        run = freeze_run(
            out,
            case,
            rubrics,
            run_kind=kind,
            origin="TEST_ONLY",
            source_path=str(source),
            workspace=str(workspace),
            bundle_path=str(package),
            bundle_sha256=receipt["bundle_sha256"],
            model="test-only",
            effort=None,
            transport="TEST",
        )
        result = {
            "natural_completion": True,
            "elapsed_seconds": 1,
            "timed_out": False,
            "interrupted": False,
        }
        save(out / "result.json", result)
        metrics = {
            "events": {"unique_tool_calls": 1, "terminal_integrity": "COMPLETE"},
            "saved_artifacts": {
                "unverified_tasks": [],
                "task_context_files": ["test-context"],
                "operations": [
                    {"effective_saved": True, "artifact_kinds": ["DOCUMENT_MAP"]}
                ],
            },
        }
        basis = {"evidence_basis_sha256": "test-basis", "later_artifacts": []}
        return out, run, result, metrics, basis

    def test_registry_distinguishes_known_source_and_pending_holdout(self):
        cases, rubrics = registry()
        ready = [
            c for c in cases["cases"] if c["status"] == "READY_WITH_PRIVATE_BINDING"
        ]
        self.assertEqual(
            len(
                {
                    c["source_sha256"]
                    for c in ready
                    if c.get("preparation", "SOURCE_ONLY") == "SOURCE_ONLY" and c["partition"] == "development"
                }
            ),
            1,
        )
        self.assertTrue(all(c["partition"] in {"development", "regression"} for c in ready))
        with self.assertRaisesRegex(ValueError, "PENDING_INPUT"):
            prepare(
                "real-complex-held-out",
                "unused",
                "unused",
                self.root / "run",
                "unused",
                "gpt-5.6-luna",
            )
        self.assertFalse((self.root / "run").exists())

    def test_observation_does_not_terminate_process_and_reasoning_is_not_saved(self):
        code = """import json,time
print(json.dumps({"type":"thread.started","thread_id":"test"}),flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"reasoning","text":"PRIVATE_SENTINEL"}}),flush=True)
time.sleep(0.6)
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}),flush=True)
"""
        out = self.root / "execution"
        result = execute_process([sys.executable, "-c", code], "", self.root, out, 0.05)
        self.assertTrue(result["natural_completion"])
        self.assertGreater(result["elapsed_seconds"], 0.5)
        observation = json.loads((out / "observation.json").read_text())
        self.assertFalse(observation["process_stopped"])
        self.assertNotIn("PRIVATE_SENTINEL", (out / "events.jsonl").read_text())
        self.assertIsNone(result["overall_timeout_seconds"])

    def test_successful_process_exit_without_model_terminal_is_not_completion(self):
        result = execute_process(
            [sys.executable, "-c", 'print("{}")'],
            "",
            self.root,
            self.root / "execution",
            1,
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["natural_completion"])

    def test_case_tamper_fails_and_all_program_passes_do_not_resolve_semantics(self):
        out, run, result, metrics, basis = self.run_fixture()
        with patch(
            "evals.agent_evals.reporting.inspect_run",
            return_value=(result, metrics, basis),
        ):
            report = grade(out, self.root / "grade.json")
        self.assertEqual(report["verdict"], "REVIEW")
        self.assertTrue(
            all(
                c["status"] == "PASS"
                for c in report["checks"]
                if c["evaluator"] == "PROGRAM"
            )
        )
        (out / "case.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Frozen case/rubric changed"):
            load_run(out)

    def test_source_review_requires_correct_run_and_evidence_revision(self):
        out, run, result, metrics, basis = self.run_fixture()
        with patch(
            "evals.agent_evals.reporting.inspect_run",
            return_value=(result, metrics, basis),
        ):
            review_template(out, self.root / "template.json")
            review = json.loads((self.root / "template.json").read_text())
            review["reviewer"] = {"kind": "OPERATOR_REVIEW", "id": "regression-test"}
            decision = review["checks"][0]
            decision.update(
                status="FAIL",
                reason="Demonstrated source error.",
                evidence=[{"path": run["source_path"], "sha256": run["source_sha256"]}],
            )
            save(self.root / "review.json", review)
            scored = grade(out, self.root / "failed.json", self.root / "review.json")
            self.assertEqual(scored["verdict"], "FAIL")
            review["evidence_basis_sha256"] = "wrong"
            save(self.root / "wrong.json", review)
            with self.assertRaisesRegex(ValueError, "another run or artifact revision"):
                grade(out, self.root / "unused.json", self.root / "wrong.json")
            with self.assertRaisesRegex(ValueError, "outside the tested workspace"):
                grade(out, Path(run["workspace"]) / "leak.json")

    def test_timeout_and_replay_are_not_model_capability_failure_or_success(self):
        out, run, result, metrics, basis = self.run_fixture(kind="DETERMINISTIC_REPLAY")
        self.assertEqual(
            automatic("autonomous_completion", run, result, metrics, basis)[0], "N/A"
        )
        run["run_kind"] = "MODEL_RUN"
        result["timed_out"] = True
        self.assertEqual(
            automatic("autonomous_completion", run, result, metrics, basis)[0], "REVIEW"
        )
        basis["later_artifacts"] = ["later-legal-continuation"]
        self.assertEqual(
            automatic("artifact_integrity", run, result, metrics, basis)[0], "FAIL"
        )

    def test_full_required_artifacts_use_production_kind_names(self):
        _, run, result, metrics, basis = self.run_fixture()
        run["scope"] = "FULL_DOCUMENT"
        self.assertEqual(
            automatic("required_artifacts", run, result, metrics, basis)[0], "REVIEW"
        )
        metrics["saved_artifacts"]["operations"][0]["artifact_kinds"] = [
            "DOCUMENT_MAP",
            "SOURCE_ITEMS",
            "DELIVERY",
        ]
        self.assertEqual(
            automatic("required_artifacts", run, result, metrics, basis)[0], "PASS"
        )


if __name__ == "__main__":
    unittest.main()
