from __future__ import annotations

# Test runtime isolation must be active before application imports.

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageTransportResult,
    canonical_sha256,
)
from app.adapters.agents.codex_app_server.source_item_text_executor import (
    LocalCanonicalSourceItemTextExecutor,
)
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.adapters.agents.reference_workspace import (
    materialize_product_evidence_workspace,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.agent_workflow.source_requirement_baseline import (
    build_source_item_text_scope_request,
    canonical_json_sha256,
    compile_document_map_artifact,
)
from app.ports.source_item_text_executor import SourceItemTextTaskV1
from tests.test_canonical_app_server_document_session import _runtime
from tests.test_source_requirement_baseline import _node, _public_reference


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class SourceItemTextAppServerExecutorTests(unittest.TestCase):
    def test_revision_guidance_is_private_hash_bound_and_resume_sensitive(self) -> None:
        with _scenario() as scenario:
            guidance = _revision_guidance(scenario, message="按用户指引保持来源忠实。")
            task = SourceItemTextTaskV1(
                project_id=scenario.task.project_id,
                document_id=scenario.task.document_id,
                source_path=scenario.task.source_path,
                document_map=scenario.task.document_map,
                scope_requests=(scenario.task.scope_requests[0],),
                timeout_seconds=scenario.task.timeout_seconds,
                max_concurrency=2,
                execution_context=guidance,
            )
            transport = _WindowedTextTransportFactory()
            checkpoints: list[dict[str, object]] = []
            first = scenario.executor(transport).execute(
                task,
                cancellation_requested=lambda: False,
                checkpoint_sink=lambda value: checkpoints.append(dict(value)),
            )

            self.assertTrue(first.batch)
            self.assertTrue(transport.revision_guidance_payloads)
            self.assertTrue(
                all(item == guidance for item in transport.revision_guidance_payloads)
            )
            self.assertTrue(
                all(
                    item["current_affected_scopes"]
                    for item in transport.revision_guidance_payloads
                )
            )

            changed = _revision_guidance(scenario, message="另一条有效但不同的指引。")
            changed_task = replace(task, execution_context=changed)
            resumed_transport = _WindowedTextTransportFactory()
            with self.assertRaisesRegex(ValueError, "execution identity drifted"):
                scenario.executor(resumed_transport).execute(
                    changed_task,
                    cancellation_requested=lambda: False,
                    resume_checkpoint=checkpoints[-1],
                )

            self.assertEqual(resumed_transport.total_calls, 0)

    def test_freezes_each_scope_classifies_at_peak_two_and_resumes_complete(
        self,
    ) -> None:
        with _scenario() as scenario:
            transport = _WindowedTextTransportFactory()
            executor = scenario.executor(transport)
            checkpoints: list[dict[str, object]] = []
            with patch(
                "app.adapters.agents.codex_app_server.canonical_document_session.materialize_product_evidence_workspace",
                wraps=materialize_product_evidence_workspace,
            ) as materialize:
                first = executor.execute(
                    scenario.task,
                    cancellation_requested=lambda: False,
                    checkpoint_sink=lambda value: checkpoints.append(dict(value)),
                )

            self.assertEqual(materialize.call_count, 1)
            self.assertEqual(transport.boundary_call_counts, {
                request.scope_id: 1 for request in scenario.requests
            })
            self.assertEqual(
                transport.classification_call_counts,
                {"text-window-01": 1, "text-window-02": 1},
            )
            self.assertEqual(transport.peak_classification_calls, 2)
            self.assertEqual(len(checkpoints), 5)
            legacy_fixture = json.loads(
                (
                    REPOSITORY_ROOT
                    / "prototype/tests/fixtures/source_item_text_batch_candidate_legacy.v1.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                _semantic_batch(first.batch, strip_lineage=True),
                _semantic_batch(legacy_fixture, strip_lineage=True),
            )
            self.assertEqual(first.run_receipt["runner_version"], "1.1.0")
            self.assertEqual(
                {item.runner_output_execution_mode for item in transport.invocations},
                {
                    "SOURCE_ITEM_TEXT_BOUNDARY_PLAN",
                    "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW",
                },
            )

            resumed_transport = _WindowedTextTransportFactory()
            resumed = scenario.executor(resumed_transport).execute(
                scenario.task,
                cancellation_requested=lambda: False,
                resume_checkpoint=checkpoints[-1],
            )

            self.assertEqual(resumed.batch, first.batch)
            self.assertEqual(resumed_transport.total_calls, 0)

    def test_failed_scope_resume_does_not_recall_accepted_scope(self) -> None:
        with _scenario() as scenario:
            failed_scope = scenario.requests[1].scope_id
            checkpoints: list[dict[str, object]] = []
            first_transport = _WindowedTextTransportFactory(
                always_invalid_boundary_scope=failed_scope
            )
            with self.assertRaisesRegex(RuntimeError, failed_scope):
                scenario.executor(first_transport).execute(
                    scenario.task,
                    cancellation_requested=lambda: False,
                    checkpoint_sink=lambda value: checkpoints.append(dict(value)),
                )

            self.assertEqual(
                first_transport.boundary_call_counts[scenario.requests[0].scope_id],
                1,
            )
            self.assertEqual(first_transport.boundary_call_counts[failed_scope], 2)
            self.assertEqual(len(checkpoints), 1)

            resumed_transport = _WindowedTextTransportFactory()
            scenario.executor(resumed_transport).execute(
                scenario.task,
                cancellation_requested=lambda: False,
                resume_checkpoint=checkpoints[-1],
            )

            self.assertNotIn(
                scenario.requests[0].scope_id,
                resumed_transport.boundary_call_counts,
            )
            self.assertEqual(resumed_transport.boundary_call_counts[failed_scope], 1)

    def test_failed_window_resume_reuses_boundaries_and_sibling_window(self) -> None:
        with _scenario() as scenario:
            checkpoints: list[dict[str, object]] = []
            first_transport = _WindowedTextTransportFactory(
                always_invalid_window="text-window-02"
            )
            with self.assertRaisesRegex(RuntimeError, "text-window-02"):
                scenario.executor(first_transport).execute(
                    scenario.task,
                    cancellation_requested=lambda: False,
                    checkpoint_sink=lambda value: checkpoints.append(dict(value)),
                )

            self.assertEqual(
                first_transport.classification_call_counts["text-window-01"],
                1,
            )
            self.assertEqual(
                first_transport.classification_call_counts["text-window-02"],
                2,
            )
            resumed_transport = _WindowedTextTransportFactory()
            scenario.executor(resumed_transport).execute(
                scenario.task,
                cancellation_requested=lambda: False,
                resume_checkpoint=checkpoints[-1],
            )

            self.assertEqual(resumed_transport.boundary_call_counts, {})
            self.assertNotIn(
                "text-window-01",
                resumed_transport.classification_call_counts,
            )
            self.assertEqual(
                resumed_transport.classification_call_counts["text-window-02"],
                1,
            )

    def test_missing_classification_key_retries_only_that_window(self) -> None:
        with _scenario() as scenario:
            transport = _WindowedTextTransportFactory(
                missing_key_once_window="text-window-01"
            )
            scenario.executor(transport).execute(
                scenario.task,
                cancellation_requested=lambda: False,
            )

            self.assertEqual(
                transport.classification_call_counts,
                {"text-window-01": 2, "text-window-02": 1},
            )
            self.assertEqual(
                transport.boundary_call_counts,
                {request.scope_id: 1 for request in scenario.requests},
            )

    def test_tamper_map_kit_and_codex_identity_fail_before_agent(self) -> None:
        with _scenario() as scenario:
            checkpoints: list[dict[str, object]] = []
            scenario.executor(_WindowedTextTransportFactory()).execute(
                scenario.task,
                cancellation_requested=lambda: False,
                checkpoint_sink=lambda value: checkpoints.append(dict(value)),
            )
            complete = checkpoints[-1]

            tampered = dict(complete)
            tampered["checkpoint_sha256"] = "f" * 64
            transport = _WindowedTextTransportFactory()
            with self.assertRaises(ValueError):
                scenario.executor(transport).execute(
                    scenario.task,
                    cancellation_requested=lambda: False,
                    resume_checkpoint=tampered,
                )
            self.assertEqual(transport.total_calls, 0)

            scenario.runtime.protocol["codex_version"] = "codex-cli drifted"
            transport = _WindowedTextTransportFactory()
            with self.assertRaisesRegex(ValueError, "execution identity drifted"):
                scenario.executor(transport).execute(
                    scenario.task,
                    cancellation_requested=lambda: False,
                    resume_checkpoint=complete,
                )
            self.assertEqual(transport.total_calls, 0)
            scenario.runtime.protocol["codex_version"] = "codex-cli 0.151.0"

            kit = load_portable_agent_kit(REPOSITORY_ROOT / "agent-kit")
            drifted = replace(
                kit,
                identity=kit.identity.model_copy(
                    update={"bundle_sha256": "e" * 64}
                ),
            )
            transport = _WindowedTextTransportFactory()
            with patch(
                "app.adapters.agents.codex_app_server.source_item_text_executor.load_portable_agent_kit",
                return_value=drifted,
            ), self.assertRaisesRegex(ValueError, "execution identity drifted"):
                scenario.executor(transport).execute(
                    scenario.task,
                    cancellation_requested=lambda: False,
                    resume_checkpoint=complete,
                )
            self.assertEqual(transport.total_calls, 0)

            changed_reference = _text_reference(
                scenario.source_sha256,
                suffix="changed-map",
            )
            changed_map = compile_document_map_artifact(changed_reference)
            changed_requests = tuple(
                build_source_item_text_scope_request(
                    changed_map,
                    node_id=f"clause-{index + 1}-changed-map",
                )
                for index in range(2)
            )
            changed_task = SourceItemTextTaskV1(
                project_id="project-1",
                document_id="document-1",
                source_path=scenario.source,
                document_map=changed_map.model_dump(mode="json"),
                scope_requests=tuple(
                    item.model_dump(mode="json") for item in changed_requests
                ),
                timeout_seconds=1200,
                max_concurrency=2,
            )
            transport = _WindowedTextTransportFactory()
            with self.assertRaisesRegex(ValueError, "execution identity drifted"):
                scenario.executor(transport).execute(
                    changed_task,
                    cancellation_requested=lambda: False,
                    resume_checkpoint=complete,
                )
            self.assertEqual(transport.total_calls, 0)

    def test_rejects_source_identity_drift_before_transport(self) -> None:
        with _scenario() as scenario:
            scenario.source.write_bytes(scenario.source.read_bytes() + b"drift")
            transport = _WindowedTextTransportFactory()
            with self.assertRaisesRegex(ValueError, "source identity drifted"):
                scenario.executor(transport).execute(
                    SourceItemTextTaskV1(
                        project_id="project-1",
                        document_id="document-1",
                        source_path=scenario.source,
                        document_map=scenario.document_map.model_dump(mode="json"),
                        scope_requests=tuple(
                            item.model_dump(mode="json")
                            for item in scenario.requests
                        ),
                        timeout_seconds=1200,
                    ),
                    cancellation_requested=lambda: False,
                )
            self.assertEqual(transport.total_calls, 0)

    def test_never_opens_classification_concurrency_above_two(self) -> None:
        with _scenario() as scenario:
            transport = _WindowedTextTransportFactory()
            over_parallel = SourceItemTextTaskV1(
                project_id=scenario.task.project_id,
                document_id=scenario.task.document_id,
                source_path=scenario.task.source_path,
                document_map=scenario.task.document_map,
                scope_requests=scenario.task.scope_requests,
                timeout_seconds=scenario.task.timeout_seconds,
                max_concurrency=3,
            )
            with self.assertRaisesRegex(ValueError, "task is invalid"):
                scenario.executor(transport).execute(
                    over_parallel,
                    cancellation_requested=lambda: False,
                )
            self.assertEqual(transport.total_calls, 0)


class _WindowedTextTransportFactory:
    def __init__(
        self,
        *,
        always_invalid_boundary_scope: str = "",
        always_invalid_window: str = "",
        missing_key_once_window: str = "",
    ) -> None:
        self.always_invalid_boundary_scope = always_invalid_boundary_scope
        self.always_invalid_window = always_invalid_window
        self.missing_key_once_window = missing_key_once_window
        self.boundary_call_counts: dict[str, int] = {}
        self.classification_call_counts: dict[str, int] = {}
        self.invocations = []
        self.revision_guidance_payloads: list[dict[str, object]] = []
        self.active_classification_calls = 0
        self.peak_classification_calls = 0
        self._lock = threading.Lock()

    @property
    def total_calls(self) -> int:
        return sum(self.boundary_call_counts.values()) + sum(
            self.classification_call_counts.values()
        )

    def __call__(self, _timeout, _cancelled):  # type: ignore[no-untyped-def]
        return _WindowedTextTransport(self)


class _WindowedTextTransport:
    def __init__(self, owner: _WindowedTextTransportFactory) -> None:
        self._owner = owner

    def invoke(self, invocation):  # type: ignore[no-untyped-def]
        readable_root = Path(
            invocation.turn_sandbox_policy["access"]["readableRoots"][0]
        )
        phase_path = _only(readable_root.rglob("phase-input.json"))
        scope_path = _only(readable_root.rglob("scope.full.json"))
        phase = json.loads(phase_path.read_text(encoding="utf-8"))
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        guidance_paths = list(readable_root.rglob("revision-guidance.json"))
        if guidance_paths:
            with self._owner._lock:
                self._owner.revision_guidance_payloads.append(
                    json.loads(_only(guidance_paths).read_text(encoding="utf-8"))
                )
        mode = invocation.runner_output_execution_mode
        with self._owner._lock:
            self._owner.invocations.append(invocation)
        if mode == "SOURCE_ITEM_TEXT_BOUNDARY_PLAN":
            scope_id = str(phase["scope_id"])
            with self._owner._lock:
                self._owner.boundary_call_counts[scope_id] = (
                    self._owner.boundary_call_counts.get(scope_id, 0) + 1
                )
            payload = _boundary_payload(scope)
            if scope_id == self._owner.always_invalid_boundary_scope:
                payload["records"] = []
                payload["structure_only_spans"] = []
        elif mode == "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW":
            window_input = phase["classification_window"]
            window_id = str(window_input["window"]["window_id"])
            with self._owner._lock:
                self._owner.classification_call_counts[window_id] = (
                    self._owner.classification_call_counts.get(window_id, 0) + 1
                )
                call_number = self._owner.classification_call_counts[window_id]
                self._owner.active_classification_calls += 1
                self._owner.peak_classification_calls = max(
                    self._owner.peak_classification_calls,
                    self._owner.active_classification_calls,
                )
            try:
                # Session preparation is intentionally serialized so that one
                # Evidence Workspace is materialized. Keep the first transport
                # turn open long enough for the sibling to enter the bounded pool.
                time.sleep(0.25)
                record_keys = [
                    str(item["record_key"])
                    for item in window_input["records"]
                ]
                if (
                    window_id == self._owner.always_invalid_window
                    or (
                        window_id == self._owner.missing_key_once_window
                        and call_number == 1
                    )
                ):
                    record_keys = record_keys[:-1]
                payload = _classification_payload(record_keys)
            finally:
                with self._owner._lock:
                    self._owner.active_classification_calls -= 1
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


class _Scenario:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = root / "runtime"
        self.state.mkdir()
        corpus = root / "corpus"
        corpus.mkdir()
        self.source = corpus / "source.pdf"
        build_synthetic_pdf(self.source)
        self.source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        reference = _text_reference(self.source_sha256)
        self.document_map = compile_document_map_artifact(reference)
        self.requests = tuple(
            build_source_item_text_scope_request(
                self.document_map,
                node_id=f"clause-{index + 1}",
            )
            for index in range(2)
        )
        self.runtime = _runtime(self.state)
        self.runtime.protocol["codex_version"] = "codex-cli 0.151.0"
        self.task = SourceItemTextTaskV1(
            project_id="project-1",
            document_id="document-1",
            source_path=self.source,
            document_map=self.document_map.model_dump(mode="json"),
            scope_requests=tuple(
                item.model_dump(mode="json") for item in self.requests
            ),
            timeout_seconds=1200,
            max_concurrency=2,
        )

    def executor(
        self,
        transport: _WindowedTextTransportFactory,
    ) -> LocalCanonicalSourceItemTextExecutor:
        @contextmanager
        def runtime_state(**_kwargs):  # type: ignore[no-untyped-def]
            yield self.runtime

        return LocalCanonicalSourceItemTextExecutor(
            codex_bin=Path(sys.executable),
            codex_home=self.state,
            repository_root=REPOSITORY_ROOT,
            transport_factory=transport,
            runtime_state_factory=runtime_state,
        )


@contextmanager
def _scenario():  # type: ignore[no-untyped-def]
    with tempfile.TemporaryDirectory() as raw:
        yield _Scenario(Path(raw))


def _text_reference(source_sha256: str, *, suffix: str = "") -> dict[str, object]:
    reference = _public_reference()
    reference["source_sha256"] = source_sha256
    tree = reference["document_tree"]
    assert isinstance(tree, dict)
    nodes = tree["nodes"]
    assert isinstance(nodes, list)
    for scope_index in range(2):
        node_id = f"clause-{scope_index + 1}{('-' + suffix) if suffix else ''}"
        source_content = []
        for record_index in range(2):
            content_id = f"content-{scope_index + 1}-{record_index + 1}{('-' + suffix) if suffix else ''}"
            text = (
                f"供应商应提交第{scope_index + 1}-{record_index + 1}份验证报告。"
            )
            source_content.append(
                {
                    "content_id": content_id,
                    "page": scope_index + 1,
                    "bbox": [
                        0.0,
                        float(record_index * 30),
                        200.0,
                        float(record_index * 30 + 20),
                    ],
                    "source_bounds": [0.0, 0.0, 595.0, 842.0],
                    "source_handles": [f"source-{content_id}"],
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "precision": "EXACT_NATIVE_SPAN_RANGE",
                    "is_title": False,
                }
            )
        nodes.append(
            _node(
                node_id,
                "root",
                len(nodes),
                f"验证范围 {scope_index + 1}{suffix}",
                f"2.{scope_index + 1}",
                (scope_index + 1,),
                (),
                source_content=tuple(source_content),
            )
        )
    return reference


def _revision_guidance(
    scenario: _Scenario,
    *,
    message: str,
) -> dict[str, object]:
    values = {
        "schema_version": "source-item-scope-revision-agent-context/v2",
        "source_sha256": scenario.source_sha256,
        "document_map_artifact_id": scenario.document_map.artifact_id,
        "document_map_artifact_sha256": scenario.document_map.artifact_sha256,
        "current_baseline_artifact_id": "source-requirement-baseline-" + "a" * 32,
        "current_baseline_artifact_sha256": "b" * 64,
        "current_baseline_revision": 2,
        "affected_scope_ids": [scenario.requests[0].scope_id],
        "comparison_scope_ids": [scenario.requests[1].scope_id],
        "guidance_items": [
            {
                "guidance_id": "source-requirement-feedback-" + "c" * 32,
                "origin": "USER_FEEDBACK",
                "source_record_id": "source-record-" + "d" * 32,
                "scope_id": scenario.requests[0].scope_id,
                "message": message,
            }
        ],
        "current_affected_scopes": [
            {
                "scope_id": scenario.requests[0].scope_id,
                "records": [
                    {
                        "source_record_id": "source-record-" + f"{index + 1:032x}",
                        "source_display_id": f"SRC-{index + 1:04d}",
                        "requirement_display_id": f"REQ-{index + 1:04d}",
                        "label": f"当前已接受条目 {index + 1}",
                        "source_form": "NUMBERED_CLAUSE",
                        "text_spans": [
                            {
                                "content_id": content.content_id,
                                "start_offset": 0,
                                "end_offset": len(content.text),
                            }
                        ],
                        "shared_source_content_ids": [],
                        "requirementness": "REQUIREMENT",
                        "requirement_kinds": ["CONSTRAINT"],
                        "non_requirement_role": None,
                        "confidence": 0.98,
                        "decision_summary": "当前来源边界已被接受，等待本轮复核。",
                    }
                    for index, content in enumerate(
                        scenario.requests[0].source_content
                    )
                ],
                "structure_only_spans": [],
            }
        ],
        "comparison_scopes": [scenario.requests[1].model_dump(mode="json")],
        "instruction": (
            "Use source-supported feedback to return complete replacements for "
            "affected scopes only; comparison scopes are read-only context."
        ),
    }
    return {
        **values,
        "context_sha256": canonical_json_sha256(values),
    }


def _boundary_payload(scope: dict[str, object]) -> dict[str, object]:
    records = []
    for index, content in enumerate(scope["source_content"]):
        assert isinstance(content, dict)
        text = str(content["text"])
        records.append(
            {
                "label": f"{scope['node_label']} {scope['node_title']} · {index + 1}",
                "source_form": "NUMBERED_CLAUSE",
                "text_spans": [
                    {
                        "content_id": content["content_id"],
                        "start_offset": 0,
                        "end_offset": len(text),
                    }
                ],
                "shared_source_content_ids": [],
            }
        )
    return {
        "schema_version": "source-item-text-boundary-agent-candidate/v1",
        "scope_id": scope["scope_id"],
        "records": records,
        "structure_only_spans": [],
    }


def _classification_payload(record_keys: list[str]) -> dict[str, object]:
    return {
        "schema_version": "source-item-text-classification-window-candidate/v1",
        "decisions": [
            {
                "record_key": record_key,
                "requirementness": "REQUIREMENT",
                "requirement_kinds": ["CONSTRAINT"],
                "non_requirement_role": None,
                "confidence": 0.98,
                "decision_summary": "完整条款规定供应商提交验证报告。",
                "reason_codes": ["supplier_deliverable_constraint"],
            }
            for record_key in record_keys
        ],
        "diagnostic_codes": [],
    }


def _semantic_batch(value, *, strip_lineage: bool = False):  # type: ignore[no-untyped-def]
    payload = json.loads(json.dumps(value, ensure_ascii=False))
    if strip_lineage:
        for key in (
            "source_sha256",
            "document_map_artifact_id",
            "document_map_artifact_sha256",
        ):
            payload.pop(key, None)
    for candidate in payload["candidates"]:
        if strip_lineage:
            for key in (
                "source_sha256",
                "document_map_artifact_id",
                "document_map_artifact_sha256",
                "request_sha256",
            ):
                candidate.pop(key, None)
        for item in candidate["items"]:
            item.pop("item_key", None)
    return payload


def _only(values):  # type: ignore[no-untyped-def]
    items = tuple(values)
    if len(items) != 1:
        raise AssertionError(f"expected one path, got {len(items)}")
    return items[0]


if __name__ == "__main__":
    unittest.main()
