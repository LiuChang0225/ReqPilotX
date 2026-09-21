#!/usr/bin/env python3
"""Run one unittest file and write machine-readable per-test outcomes."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.successes: list[str] = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.successes.append(test.id())


def run_test_file(path: Path, *, verbosity: int = 2) -> tuple[dict, bool]:
    path = path.expanduser().resolve(strict=True)
    spec = importlib.util.spec_from_file_location(f"reqpilotx_required_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load unittest file: {path}")
    sys.path.insert(0, str(path.parent))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    runner = unittest.TextTestRunner(verbosity=verbosity, resultclass=RecordingResult)
    result: RecordingResult = runner.run(suite)
    summary = {
        "schema_version": "reqpilotx-structured-unittest/v1",
        "test_file": path.name,
        "tests_run": result.testsRun,
        "successful": sorted(result.successes),
        "failures": [{"test": test.id(), "detail": detail} for test, detail in result.failures],
        "errors": [{"test": test.id(), "detail": detail} for test, detail in result.errors],
        "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        "expected_failures": [test.id() for test, _ in result.expectedFailures],
        "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
    }
    allowed_value = os.environ.get("REQPILOT_TEST_ALLOWED_PRODUCT_ROOT")
    if allowed_value:
        allowed = Path(allowed_value).expanduser().resolve(strict=True)
        origins = {}
        for name, loaded in sorted(sys.modules.items()):
            if name == "app" or name.startswith("app.") or name == "reqpilotx_engine" or name.startswith("reqpilotx_engine."):
                source = getattr(loaded, "__file__", None)
                origins[name] = [str(Path(source).resolve())] if source else [str(Path(item).resolve()) for item in getattr(loaded, "__path__", [])]
        summary["module_origins"] = origins
        summary["outside_allowed_root"] = {
            name: paths for name, paths in origins.items()
            if not paths or any(not Path(source).is_relative_to(allowed) for source in paths)
        }
    summary["was_successful"] = result.wasSuccessful()
    return summary, result.wasSuccessful()


def evaluate_required_tests(summary: dict, required_suffixes: tuple[str, ...]) -> dict:
    outcomes = {}
    successful = set(summary.get("successful") or [])
    skipped = {item["test"]: item["reason"] for item in summary.get("skipped") or []}
    failed = {item["test"]: item["detail"] for item in summary.get("failures") or []}
    errors = {item["test"]: item["detail"] for item in summary.get("errors") or []}
    all_ids = successful | set(skipped) | set(failed) | set(errors)
    for suffix in required_suffixes:
        matches = [identity for identity in all_ids if identity.endswith(suffix)]
        if len(matches) != 1:
            outcomes[suffix] = {"status": "NOT_RUN", "matches": sorted(matches)}
            continue
        identity = matches[0]
        if identity in successful:
            outcomes[suffix] = {"status": "PASSED", "test": identity}
        elif identity in skipped:
            outcomes[suffix] = {"status": "SKIPPED", "test": identity, "reason": skipped[identity]}
        elif identity in failed:
            outcomes[suffix] = {"status": "FAILED", "test": identity, "detail": failed[identity]}
        else:
            outcomes[suffix] = {"status": "ERROR", "test": identity, "detail": errors[identity]}
    complete = bool(required_suffixes) and all(item["status"] == "PASSED" for item in outcomes.values())
    return {
        "status": "PASSED" if complete else "NOT_VERIFIED",
        "tests_run": int(summary.get("tests_run") or 0),
        "success_count": len(successful),
        "failure_count": len(failed),
        "error_count": len(errors),
        "skip_count": len(skipped),
        "required": outcomes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-file", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    try:
        summary, successful = run_test_file(args.test_file)
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 0 if successful else 1
    except Exception as exc:
        print(json.dumps({"status": "RUNNER_FAILED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
