from __future__ import annotations

# Test runtime isolation must be active before application imports.

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageTransportResult,
    canonical_sha256,
)
from app.adapters.agents.codex_app_server.source_requirement_window_executor import (
    LocalCanonicalSourceRequirementWindowExecutor,
)
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.adapters.agents.reference_workspace import (
    materialize_product_evidence_workspace,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.agent_workflow.source_requirement_baseline import (
    build_source_requirement_scope_request,
    compile_document_map_artifact,
)
from app.ports.source_requirement_window_executor import (
    SourceRequirementWindowTaskV1,
)
from tests.test_canonical_app_server_document_session import _runtime
from tests.test_native_source_requirement_windowed_reference import (
    _boundary_candidate,
    _classification_candidate,
)
from tests.test_source_requirement_baseline import (
    _fragment,
    _public_reference,
    _table_preview,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class SourceRequirementAppServerExecutorTests(unittest.TestCase):
    def test_orchestrates_locked_boundary_windows_and_reuses_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "runtime"
            state.mkdir()
            corpus = root / "corpus"
            corpus.mkdir()
            source = corpus / "source.pdf"
            build_synthetic_pdf(source)
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            document_map, request = _source_bound_windowed_fixture(source_sha256)
            transport = _RecordingTransportFactory()
            runtime = _runtime(state)
            runtime.protocol["codex_version"] = "codex-cli 0.151.0"

            @contextmanager
            def runtime_state(**_kwargs):  # type: ignore[no-untyped-def]
                yield runtime

            executor = LocalCanonicalSourceRequirementWindowExecutor(
                codex_bin=Path(sys.executable),
                codex_home=state,
                repository_root=REPOSITORY_ROOT,
                transport_factory=transport,
                runtime_state_factory=runtime_state,
            )
            checkpoints: list[dict[str, object]] = []
            task = SourceRequirementWindowTaskV1(
                project_id="project-1",
                document_id="document-1",
                source_path=source,
                document_map=document_map.model_dump(mode="json"),
                scope_request=request.model_dump(mode="json"),
                timeout_seconds=1200,
                max_concurrency=2,
            )

            with patch(
                "app.adapters.agents.codex_app_server.canonical_document_session.materialize_product_evidence_workspace",
                wraps=materialize_product_evidence_workspace,
            ) as materialize:
                first = executor.execute(
                    task,
                    resume_checkpoint=None,
                    checkpoint_sink=lambda value: checkpoints.append(dict(value)),
                    cancellation_requested=lambda: False,
                )

            self.assertEqual(len(first.candidate["records"]), 6)
            self.assertEqual(materialize.call_count, 1)
            self.assertEqual(transport.boundary_calls, 1)
            self.assertEqual(transport.classification_calls, 2)
            self.assertEqual(len(checkpoints), 3)
            self.assertEqual(
                {item.runner_output_execution_mode for item in transport.invocations},
                {
                    "SOURCE_REQUIREMENT_BOUNDARY_PLAN",
                    "SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW",
                },
            )
            self.assertTrue(
                all(
                    item.session_attestation.source_requirement_stage_input
                    is not None
                    for item in transport.invocations
                )
            )
            self.assertTrue(
                all(
                    item.turn_output_schema_sha256
                    == canonical_sha256(dict(item.turn_output_schema))
                    for item in transport.invocations
                )
            )

            resumed_transport = _RecordingTransportFactory()
            resumed = LocalCanonicalSourceRequirementWindowExecutor(
                codex_bin=Path(sys.executable),
                codex_home=state,
                repository_root=REPOSITORY_ROOT,
                transport_factory=resumed_transport,
                runtime_state_factory=runtime_state,
            ).execute(
                task,
                resume_checkpoint=checkpoints[-1],
                checkpoint_sink=lambda _value: None,
                cancellation_requested=lambda: False,
            )

            self.assertEqual(resumed.candidate, first.candidate)
            self.assertEqual(resumed_transport.boundary_calls, 0)
            self.assertEqual(resumed_transport.classification_calls, 0)

    def test_source_identity_drift_is_rejected_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "runtime"
            state.mkdir()
            corpus = root / "corpus"
            corpus.mkdir()
            source = corpus / "source.pdf"
            build_synthetic_pdf(source)
            document_map, request = _source_bound_windowed_fixture("a" * 64)
            transport = _RecordingTransportFactory()

            @contextmanager
            def runtime_state(**_kwargs):  # type: ignore[no-untyped-def]
                yield _runtime(state)

            executor = LocalCanonicalSourceRequirementWindowExecutor(
                codex_bin=Path(sys.executable),
                codex_home=state,
                repository_root=REPOSITORY_ROOT,
                transport_factory=transport,
                runtime_state_factory=runtime_state,
            )

            with self.assertRaisesRegex(ValueError, "source identity drifted"):
                executor.execute(
                    SourceRequirementWindowTaskV1(
                        project_id="project-1",
                        document_id="document-1",
                        source_path=source,
                        document_map=document_map.model_dump(mode="json"),
                        scope_request=request.model_dump(mode="json"),
                        timeout_seconds=1200,
                        max_concurrency=2,
                    ),
                    resume_checkpoint=None,
                    checkpoint_sink=lambda _value: None,
                    cancellation_requested=lambda: False,
                )

            self.assertEqual(transport.boundary_calls, 0)
            self.assertEqual(transport.classification_calls, 0)


class _RecordingTransportFactory:
    def __init__(self) -> None:
        self.boundary_calls = 0
        self.classification_calls = 0
        self.invocations = []
        self._lock = threading.Lock()

    def __call__(self, _timeout, _cancelled):  # type: ignore[no-untyped-def]
        return _RecordingTransport(self)


class _RecordingTransport:
    def __init__(self, owner: _RecordingTransportFactory) -> None:
        self._owner = owner

    def invoke(self, invocation):  # type: ignore[no-untyped-def]
        readable_root = Path(
            invocation.turn_sandbox_policy["access"]["readableRoots"][0]
        )
        phase_paths = tuple(readable_root.rglob("phase-input.json"))
        if len(phase_paths) != 1:
            raise AssertionError("expected exactly one locked phase-input.json")
        phase_input = json.loads(phase_paths[0].read_text(encoding="utf-8"))
        mode = invocation.runner_output_execution_mode
        with self._owner._lock:
            self._owner.invocations.append(invocation)
            if mode == "SOURCE_REQUIREMENT_BOUNDARY_PLAN":
                self._owner.boundary_calls += 1
                payload = _boundary_candidate()
            elif mode == "SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW":
                self._owner.classification_calls += 1
                records = phase_input["classification_window"]["records"]
                payload = _classification_candidate(
                    [str(item["record_key"]) for item in records]
                )
            else:
                raise AssertionError(f"unexpected execution mode: {mode}")
        return CanonicalStageTransportResult(
            stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            structured_output=payload,
            output_schema_sha256=invocation.turn_output_schema_sha256,
            response_sha256=canonical_sha256(payload),
            tool_receipt_sha256="3" * 64,
            runtime_thread_id="thread-1",
            runtime_turn_id="turn-1",
            tool_activity_summary={
                "event_counts": {"turn.completed": 1},
                "dynamic_tools": {"tool_call_counts": {}},
            },
        )


def _source_bound_windowed_fixture(source_sha256: str):  # type: ignore[no-untyped-def]
    public_reference = _public_reference()
    public_reference["source_sha256"] = source_sha256
    public_reference["logical_tables"][0]["fragments"][0]["row_range"] = [0, 7]
    document_map = compile_document_map_artifact(public_reference)
    preview = _table_preview(
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
    )
    preview["source_sha256"] = source_sha256
    request = build_source_requirement_scope_request(document_map, preview)
    return document_map, request


if __name__ == "__main__":
    unittest.main()
