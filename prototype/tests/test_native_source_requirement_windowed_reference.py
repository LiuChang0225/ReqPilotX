from __future__ import annotations

# Test runtime isolation must be active before application imports.

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (
    SourceRequirementBoundaryAgentCandidateV1,
    _compile_boundary_plan,
    run_native_source_requirement_windowed_reference,
)
from app.application.agent_workflow.source_requirement_baseline import (
    build_source_requirement_scope_request,
    compile_document_map_artifact,
)
from tests.test_source_requirement_baseline import (
    _fragment,
    _public_reference,
    _table_preview,
)


class NativeSourceRequirementWindowedReferenceTests(unittest.TestCase):
    def test_boundary_identity_ignores_nonsemantic_context_reason_wording(self) -> None:
        document_map, request = _windowed_fixture()
        first = _boundary_candidate()
        second = _boundary_candidate()
        second["context_row_groups"][0]["reason_codes"] = ["column_header"]
        second["records"][0]["label"] = "1 替代展示标签"

        first_plan = _compile_boundary_plan(
            document_map=document_map,
            request=request,
            candidate=SourceRequirementBoundaryAgentCandidateV1.model_validate(first),
        )
        second_plan = _compile_boundary_plan(
            document_map=document_map,
            request=request,
            candidate=SourceRequirementBoundaryAgentCandidateV1.model_validate(second),
        )

        self.assertNotEqual(first_plan.plan_sha256, second_plan.plan_sha256)
        self.assertEqual(
            first_plan.boundary_identity_sha256,
            second_plan.boundary_identity_sha256,
        )

    def test_boundary_plan_fans_out_two_concurrent_classification_windows(
        self,
    ) -> None:
        document_map, request = _windowed_fixture()
        runner = _FakeWindowedCommandRunner(synchronize_windows=True)

        with tempfile.TemporaryDirectory() as raw_output:
            output = Path(raw_output) / "result"
            result = run_native_source_requirement_windowed_reference(
                document_map=document_map,
                request=request,
                output_directory=output,
                max_concurrency=2,
                agent_kit_directory=Path(__file__).resolve().parents[2] / "agent-kit",
                command_runner=runner,
            )

            self.assertTrue((output / "boundary-plan.json").is_file())
            self.assertTrue((output / "windows/window-01/input.json").is_file())
            self.assertTrue((output / "windows/window-02/input.json").is_file())

        self.assertEqual(len(result.candidate.records), 6)
        self.assertTrue(
            all(
                item.requirementness.value == "REQUIREMENT"
                for item in result.candidate.records
            )
        )
        self.assertEqual(runner.boundary_calls, 1)
        self.assertEqual(runner.classification_calls, 2)
        self.assertEqual(runner.max_active_classification_calls, 2)
        self.assertEqual(result.run_receipt.execution_strategy, "BOUNDARY_WINDOWS")
        self.assertEqual(result.run_receipt.runner_version, "1.10.0")
        self.assertEqual(result.run_receipt.kit_version, "0.15.2")
        self.assertEqual(result.run_receipt.classification_window_count, 2)
        self.assertEqual(result.run_receipt.classification_attempt_count, 2)
        self.assertEqual(result.run_receipt.attempt_count, 3)
        self.assertEqual(
            result.run_receipt.attempt_reasoning_efforts,
            ("medium", "medium", "medium"),
        )
        self.assertEqual(
            result.run_receipt.on_demand_evidence_resources,
            ("document-map.full.json", "scope.full.json"),
        )

    def test_invalid_classification_retries_only_the_failed_window(self) -> None:
        document_map, request = _windowed_fixture()
        runner = _FakeWindowedCommandRunner(
            invalid_first_window_id="window-02",
        )

        with tempfile.TemporaryDirectory() as raw_output:
            result = run_native_source_requirement_windowed_reference(
                document_map=document_map,
                request=request,
                output_directory=Path(raw_output) / "result",
                max_concurrency=2,
                command_runner=runner,
            )

        self.assertEqual(len(result.candidate.records), 6)
        self.assertEqual(runner.boundary_calls, 1)
        self.assertEqual(runner.window_call_counts["window-01"], 1)
        self.assertEqual(runner.window_call_counts["window-02"], 2)
        self.assertEqual(result.run_receipt.classification_attempt_count, 3)
        self.assertEqual(result.run_receipt.attempt_count, 4)
        self.assertEqual(
            result.run_receipt.attempt_reasoning_efforts,
            ("medium", "medium", "medium", "high"),
        )

    def test_exhausted_window_preserves_accepted_sibling_checkpoint(self) -> None:
        document_map, request = _windowed_fixture()
        runner = _FakeWindowedCommandRunner(
            always_invalid_window_id="window-02",
        )

        with tempfile.TemporaryDirectory() as raw_output:
            output = Path(raw_output) / "result"
            with self.assertRaisesRegex(RuntimeError, "window-02"):
                run_native_source_requirement_windowed_reference(
                    document_map=document_map,
                    request=request,
                    output_directory=output,
                    max_concurrency=2,
                    command_runner=runner,
                )

            self.assertTrue((output / "boundary-plan.json").is_file())
            self.assertTrue((output / "windows/window-01/candidate.json").is_file())
            self.assertTrue((output / "windows/window-01/receipt.json").is_file())
            self.assertTrue((output / "windows/window-02/failure.json").is_file())
            self.assertTrue(
                (output / "windows/window-02/failed-candidate-2.json").is_file()
            )
            self.assertFalse((output / "candidate.json").exists())

        self.assertEqual(runner.window_call_counts["window-01"], 1)
        self.assertEqual(runner.window_call_counts["window-02"], 2)

    def test_resume_reuses_boundary_and_accepted_sibling_only(self) -> None:
        document_map, request = _windowed_fixture()
        first_runner = _FakeWindowedCommandRunner(
            always_invalid_window_id="window-02",
        )
        checkpoints = []

        with tempfile.TemporaryDirectory() as raw_output:
            with self.assertRaisesRegex(RuntimeError, "window-02"):
                run_native_source_requirement_windowed_reference(
                    document_map=document_map,
                    request=request,
                    output_directory=Path(raw_output) / "failed",
                    max_concurrency=2,
                    command_runner=first_runner,
                    checkpoint_sink=checkpoints.append,
                )

            self.assertGreaterEqual(len(checkpoints), 2)
            saved = checkpoints[-1]
            self.assertEqual(
                tuple(item.window.window_id for item in saved.accepted_windows),
                ("window-01",),
            )

            resumed_runner = _FakeWindowedCommandRunner()
            result = run_native_source_requirement_windowed_reference(
                document_map=document_map,
                request=request,
                output_directory=Path(raw_output) / "resumed",
                max_concurrency=2,
                command_runner=resumed_runner,
                resume_checkpoint=saved,
                checkpoint_sink=checkpoints.append,
            )

            self.assertTrue(
                (
                    Path(raw_output) / "resumed/windows/window-01/candidate.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    Path(raw_output) / "resumed/windows/window-02/candidate.json"
                ).is_file()
            )

        self.assertEqual(len(result.candidate.records), 6)
        self.assertEqual(resumed_runner.boundary_calls, 0)
        self.assertNotIn("window-01", resumed_runner.window_call_counts)
        self.assertEqual(resumed_runner.window_call_counts["window-02"], 1)
        self.assertEqual(result.run_receipt.classification_window_count, 2)
        self.assertEqual(result.run_receipt.classification_attempt_count, 2)
        self.assertEqual(result.run_receipt.attempt_count, 3)

    def test_resume_rejects_checkpoint_identity_drift_before_agent_call(self) -> None:
        document_map, request = _windowed_fixture()
        checkpoints = []

        with tempfile.TemporaryDirectory() as raw_output:
            run_native_source_requirement_windowed_reference(
                document_map=document_map,
                request=request,
                output_directory=Path(raw_output) / "initial",
                max_concurrency=2,
                command_runner=_FakeWindowedCommandRunner(),
                checkpoint_sink=checkpoints.append,
            )
            tampered = checkpoints[-1].model_copy(update={"request_sha256": "f" * 64})
            runner = _FakeWindowedCommandRunner()
            with self.assertRaisesRegex(ValueError, "resume identity drifted"):
                run_native_source_requirement_windowed_reference(
                    document_map=document_map,
                    request=request,
                    output_directory=Path(raw_output) / "tampered",
                    max_concurrency=2,
                    command_runner=runner,
                    resume_checkpoint=tampered,
                )

        self.assertEqual(runner.boundary_calls, 0)
        self.assertEqual(runner.classification_calls, 0)


class _FakeWindowedCommandRunner:
    def __init__(
        self,
        *,
        synchronize_windows: bool = False,
        invalid_first_window_id: str = "",
        always_invalid_window_id: str = "",
        delayed_window_id: str = "",
    ) -> None:
        self.boundary_calls = 0
        self.classification_calls = 0
        self.max_active_classification_calls = 0
        self.window_call_counts: dict[str, int] = {}
        self._active_classification_calls = 0
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(2, timeout=5) if synchronize_windows else None
        self._invalid_first_window_id = invalid_first_window_id
        self._always_invalid_window_id = always_invalid_window_id
        self._delayed_window_id = delayed_window_id

    def __call__(self, command, **kwargs):  # type: ignore[no-untyped-def]
        if "--version" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="codex-cli 0.151.0\n",
                stderr="",
            )
        workspace = Path(command[command.index("--cd") + 1])
        final_path = Path(command[command.index("--output-last-message") + 1])
        self._assert_capability_safe_workspace(workspace)
        prompt = str(kwargs["input"])
        if "SOURCE_REQUIREMENT_BOUNDARY_PLAN" in prompt:
            with self._lock:
                self.boundary_calls += 1
            payload = _boundary_candidate()
        else:
            window_payload = json.loads(
                (workspace / "classification-window.json").read_text(encoding="utf-8")
            )
            window_id = str(window_payload["window"]["window_id"])
            with self._lock:
                self.classification_calls += 1
                self.window_call_counts[window_id] = (
                    self.window_call_counts.get(window_id, 0) + 1
                )
                call_count = self.window_call_counts[window_id]
                self._active_classification_calls += 1
                self.max_active_classification_calls = max(
                    self.max_active_classification_calls,
                    self._active_classification_calls,
                )
            if self._barrier is not None and call_count == 1:
                self._barrier.wait()
            if window_id == self._delayed_window_id and call_count == 1:
                time.sleep(0.03)
            record_keys = [
                str(item["record_key"]) for item in window_payload["records"]
            ]
            if window_id == self._always_invalid_window_id or (
                window_id == self._invalid_first_window_id and call_count == 1
            ):
                record_keys = record_keys[:-1]
            payload = _classification_candidate(record_keys)
            with self._lock:
                self._active_classification_calls -= 1
        final_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"type":"thread.started"}\n'
                '{"type":"turn.started"}\n'
                '{"type":"turn.completed"}\n'
            ),
            stderr="",
        )

    def _assert_capability_safe_workspace(self, workspace: Path) -> None:
        self.assert_read_only(workspace / "document-map.full.json")
        self.assert_read_only(workspace / "scope.full.json")

    @staticmethod
    def assert_read_only(path: Path) -> None:
        if not path.is_file() or path.stat().st_mode & 0o777 != 0o400:
            raise AssertionError(f"expected read-only evidence resource: {path}")


def _windowed_fixture():  # type: ignore[no-untyped-def]
    public_reference = _public_reference()
    public_reference["logical_tables"][0]["fragments"][0]["row_range"] = [0, 7]
    document_map = compile_document_map_artifact(public_reference)
    request = build_source_requirement_scope_request(
        document_map,
        _table_preview(
            scope_id="lt-bom",
            title="产品信息",
            fragments=(
                _fragment(
                    "bom-fragment",
                    "T0001",
                    page=2,
                    rows=(
                        ("编号", "项目"),
                        ("1", "记录一"),
                        ("2", "记录二"),
                        ("3", "记录三"),
                        ("4", "记录四"),
                        ("5", "记录五"),
                        ("6", "记录六"),
                    ),
                ),
            ),
        ),
    )
    return document_map, request


def _boundary_candidate() -> dict[str, object]:
    return {
        "schema_version": "source-requirement-boundary-agent-candidate/v1",
        "records": [
            {
                "label": f"{index} 记录{index}",
                "source_form": "TABLE_RECORD",
                "row_groups": [
                    {
                        "fragment_id": "bom-fragment",
                        "row_start": index,
                        "row_count": 1,
                    }
                ],
                "shared_source_content_ids": [],
            }
            for index in range(1, 7)
        ],
        "context_row_groups": [
            {
                "role": "HEADER",
                "row_group": {
                    "fragment_id": "bom-fragment",
                    "row_start": 0,
                    "row_count": 1,
                },
                "reason_codes": ["shared_table_header"],
            }
        ],
        "diagnostic_codes": [],
    }


def _classification_candidate(record_keys: list[str]) -> dict[str, object]:
    return {
        "schema_version": ("source-requirement-classification-window-candidate/v1"),
        "decisions": [
            {
                "record_key": key,
                "requirementness": "REQUIREMENT",
                "requirement_kinds": ["QUALITY", "CONSTRAINT"],
                "non_requirement_role": None,
                "confidence": 0.95,
                "decision_summary": "来源记录规定必须执行的事项。",
                "reason_codes": [
                    "product_characteristic",
                    "normative_source_record",
                ],
            }
            for key in record_keys
        ],
        "diagnostic_codes": [],
    }


if __name__ == "__main__":
    unittest.main()
