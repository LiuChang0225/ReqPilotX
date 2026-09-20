from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageTransportResult,
    canonical_sha256,
)
from app.adapters.agents.codex_app_server.document_map_replacement_driver import (
    CanonicalDocumentMapScopeReplacementStageDriver,
    DocumentMapScopeReplacementHostCompilationError,
)
from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from tests.document_map_replacement_support import build_replacement_fixture


_DRIVER = "app.adapters.agents.codex_app_server.document_map_replacement_driver."


class DocumentMapReplacementDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.fixture = build_replacement_fixture(root / "evidence")
        request = self.fixture.request
        self.activation = SimpleNamespace(
            session_attestation=SimpleNamespace(
                document_evidence=SimpleNamespace(
                    run_id=request.run_id,
                    source_sha256=request.source_sha256,
                )
            ),
            document_map_replacement_stage_input_attestation=SimpleNamespace(
                replacement_request_sha256=request.request_sha256,
                heading_compilation_sha256=request.heading_compilation.compilation_sha256,
            ),
            evidence_workspace_path=root / "evidence",
        )
        self.session = Mock()
        self.session.require_inference_stage.return_value = self.activation
        self.build = self.stack.enter_context(
            patch(
                _DRIVER + "build_canonical_stage_invocation",
                side_effect=lambda *args, **kwargs: SimpleNamespace(**kwargs),
            )
        )
        self.stack.enter_context(
            patch(
                _DRIVER + "_load_runner_output_schema", return_value={"type": "object"}
            )
        )
        self.load_evidence = self.stack.enter_context(
            patch(
                _DRIVER + "load_document_map_scope_evidence",
                return_value=self.fixture.evidence,
            )
        )
        self.invoke = self.stack.enter_context(patch(_DRIVER + "_invoke_transport"))

    def _output(self, payload=None, pages=(1, 2)):
        payload = (
            payload
            if payload is not None
            else self.fixture.candidate.model_dump(mode="json")
        )
        payload = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "request_id",
                "request_sha256",
                "heading_compilation_sha256",
            }
        }
        payload["schema_version"] = "document-map-scope-replacement-candidate/v2"
        return CanonicalStageTransportResult(
            stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
            structured_output=payload,
            output_schema_sha256="4" * 64,
            response_sha256=canonical_sha256(payload),
            tool_receipt_sha256="5" * 64,
            runtime_thread_id="test-thread",
            runtime_turn_id="test-turn",
            tool_activity_summary={
                "visual_inspection": {
                    "high_resolution_page_numbers": list(pages),
                    "receipt_root_sha256": "6" * 64,
                },
            },
        )

    def _run(self):
        return CanonicalDocumentMapScopeReplacementStageDriver(transport=Mock()).run(
            self.session,
            request=self.fixture.request,
            task_binding_sha256="f" * 64,
        )

    def test_first_valid_scope_has_no_extra_repair_turn(self) -> None:
        self.invoke.return_value = self._output()
        result = self._run()
        self.assertIsNotNone(result.compilation)
        self.assertEqual(result.tool_activity_summary["scope_repair_count"], 0)
        self.assertNotIn("request_sha256", self._output().structured_output)
        self.assertEqual(
            result.candidate.request_sha256, self.fixture.request.request_sha256
        )
        self.assertEqual(
            result.candidate.heading_compilation_sha256,
            self.fixture.request.heading_compilation.compilation_sha256,
        )
        self.invoke.assert_called_once()

    def test_missing_business_field_gets_only_one_targeted_repair(self) -> None:
        invalid = self.fixture.candidate.model_dump(mode="json")
        del invalid["logical_tables"][0]["title"]
        self.invoke.side_effect = [self._output(invalid), self._output()]
        result = self._run()
        self.assertEqual(result.tool_activity_summary["scope_repair_count"], 1)
        self.assertIn(
            "logical_tables.0.title:missing",
            self.build.call_args.kwargs["task_directive"],
        )
        self.assertEqual(self.invoke.call_count, 2)
        self.session.require_inference_stage.assert_called_once()

    def test_large_failed_candidate_is_not_copied_into_the_task_directive(self) -> None:
        invalid = self.fixture.candidate.model_dump(mode="json")
        invalid["public_summary"] = "too-long-candidate " * 1000
        self.invoke.side_effect = [self._output(invalid), self._output()]
        result = self._run()
        directive = self.build.call_args.kwargs["task_directive"]
        self.assertLessEqual(len(directive), 2000)
        self.assertNotIn("too-long-candidate", directive)
        self.assertEqual(result.tool_activity_summary["scope_repair_count"], 1)

    def test_incomplete_row_coverage_can_be_replaced_without_redeciding_headings(
        self,
    ) -> None:
        invalid = self.fixture.candidate.model_dump(mode="json")
        invalid["logical_tables"][0]["fragments"][0]["end_row"] -= 1
        self.invoke.side_effect = [self._output(invalid), self._output()]
        result = self._run()
        self.assertEqual(result.tool_activity_summary["scope_attempt_count"], 2)
        self.assertIn(
            "table_row_coverage_invalid", self.build.call_args.kwargs["task_directive"]
        )
        self.assertEqual(
            result.compilation.heading_nodes,
            self.fixture.request.heading_compilation.heading_nodes,
        )

    def test_repeated_failure_stops_after_one_repair_and_preserves_private_candidate(
        self,
    ) -> None:
        invalid = self.fixture.candidate.model_dump(mode="json")
        invalid["logical_tables"][0]["fragments"][0]["end_row"] -= 1
        self.invoke.return_value = self._output(invalid)
        with self.assertRaises(
            DocumentMapScopeReplacementHostCompilationError
        ) as caught:
            self._run()
        self.assertEqual(self.invoke.call_count, 2)
        self.assertEqual(caught.exception.private_candidate_payload, invalid)
        self.assertEqual(
            caught.exception.tool_activity_summary["scope_attempt_count"], 2
        )

    def test_host_evidence_failure_is_not_sent_back_as_an_agent_mistake(self) -> None:
        self.invoke.return_value = self._output()
        self.load_evidence.side_effect = ValueError(
            "replacement Evidence Workspace lineage drifted"
        )
        with self.assertRaises(DocumentMapScopeReplacementHostCompilationError):
            self._run()
        self.invoke.assert_called_once()

    def test_missing_visual_receipt_does_not_publish_a_claimed_complete_scope(
        self,
    ) -> None:
        self.invoke.return_value = self._output(pages=(1,))
        with self.assertRaisesRegex(LifecycleProbeError, "VISUAL_COVERAGE_INCOMPLETE"):
            self._run()
        self.invoke.assert_called_once()


if __name__ == "__main__":
    unittest.main()
