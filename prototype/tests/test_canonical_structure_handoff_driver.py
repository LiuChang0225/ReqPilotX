from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import tempfile
from pathlib import Path
import unittest

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CANONICAL_STRUCTURE_HANDOFF_EXECUTION_MODE,
    CanonicalProductEvidenceRequest,
    CanonicalStructureHandoffBindingV1,
    prepare_canonical_app_server_document_session,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageTransportResult,
    canonical_sha256,
)
from app.adapters.agents.codex_app_server.canonical_structure_handoff_driver import (
    CanonicalStructureHandoffDriver,
    NativeStructureHandoffTurnResultV1,
)
from app.adapters.agents.codex_app_server.canonical_structure_handoff_tools import (
    CanonicalNativeStructureHandoffToolHost,
)
from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage

if __package__:
    from .test_canonical_app_server_document_session import _runtime
    from .test_primary_runtime_pdf import _runtime_fixture
else:
    from test_canonical_app_server_document_session import _runtime
    from test_primary_runtime_pdf import _runtime_fixture


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGENT_KIT_ROOT = REPOSITORY_ROOT / "agent-kit"


class CanonicalStructureHandoffDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="handoff-primary-runtime-")
        self.addCleanup(directory.cleanup)
        self.primary_runtime_root = _runtime_fixture(Path(directory.name))

    def test_every_turn_must_select_a_working_decision(self) -> None:
        selected = NativeStructureHandoffTurnResultV1.model_validate(
            {
                "schema_version": "native-structure-handoff-result/v2",
                "status": "NO_CHANGE",
                "reason_code": "current_structure_selected",
                "evidence_state": "INSUFFICIENT",
                "decision_policy": "KEEP_CURRENT_STRUCTURE",
                "public_summary": "证据有限，明确保留当前结构继续。",
            }
        )

        self.assertEqual(selected.status, "NO_CHANGE")
        with self.assertRaises(ValueError):
            NativeStructureHandoffTurnResultV1.model_validate(
                {
                    "schema_version": "native-structure-handoff-result/v2",
                    "status": "UNRESOLVED",
                    "reason_code": "evidence_insufficient",
                    "evidence_state": "INSUFFICIENT",
                    "decision_policy": "KEEP_CURRENT_STRUCTURE",
                    "public_summary": "不允许终态未决。",
                }
            )

    def test_handoff_variant_requires_binding_and_matches_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            corpus = state / "corpus"
            corpus.mkdir(mode=0o700)
            source = corpus / "source.pdf"
            build_synthetic_pdf(source, include_prompt_injection=False)
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            run_id = "agent-run-" + "1" * 32
            binding = _binding(run_id=run_id, source_sha256=source_sha256)
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=_runtime(state),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.LOGICAL_STRUCTURE,),
                evidence_request=CanonicalProductEvidenceRequest(
                    source_path=source,
                    expected_source_sha256=source_sha256,
                    document_role="CUSTOMER_SOR",
                    authorized_corpus_root=corpus,
                    run_id=run_id,
                    opaque_source_handle="source-handle-" + "2" * 32,
                ),
                structure_handoff=binding,
                repository_root=REPOSITORY_ROOT,
            )
            publisher = _Publisher()
            host = CanonicalNativeStructureHandoffToolHost(
                project_id="project-1",
                document_id="document-1",
                handoff_id=binding.handoff_id,
                revision_publisher=publisher,
            )
            host.context_call_count = 1
            transport = _Transport()
            try:
                activation = session.require_inference_stage(
                    AgentExtractionStage.LOGICAL_STRUCTURE,
                    execution_mode=CANONICAL_STRUCTURE_HANDOFF_EXECUTION_MODE,
                )
                self.assertIsNone(activation.predecessor)
                self.assertEqual(activation.structure_handoff, binding)
                self.assertEqual(
                    activation.runner_output_schema_id,
                    "native-structure-handoff-result/v2",
                )
                result = CanonicalStructureHandoffDriver(
                    transport=transport,
                    dynamic_tool_host=host,
                    revision_publisher=publisher,  # type: ignore[arg-type]
                ).run(session, task_binding_sha256="a" * 64)
            finally:
                session.close()

        self.assertEqual(result.turn_result.status, "PUBLISHED")
        self.assertEqual(result.publication, publisher.last_result)
        self.assertEqual(
            transport.invocation.runner_output_execution_mode,
            CANONICAL_STRUCTURE_HANDOFF_EXECUTION_MODE,
        )
        self.assertIn(
            "host_task_directive=", transport.invocation.input_items[0]["text"]
        )
        self.assertIn(
            "first Host-valid reversible proposal",
            transport.invocation.input_items[0]["text"],
        )
        self.assertIn(
            "KEPT_UNRESOLVED",
            transport.invocation.input_items[0]["text"],
        )

    def test_handoff_mode_without_binding_fails_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            corpus = state / "corpus"
            corpus.mkdir(mode=0o700)
            source = corpus / "source.pdf"
            build_synthetic_pdf(source, include_prompt_injection=False)
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            session = prepare_canonical_app_server_document_session(
                primary_runtime_root=self.primary_runtime_root,
                base_runtime=_runtime(state),
                canonical_kit_root=AGENT_KIT_ROOT,
                stages=(AgentExtractionStage.LOGICAL_STRUCTURE,),
                evidence_request=CanonicalProductEvidenceRequest(
                    source_path=source,
                    expected_source_sha256=source_sha256,
                    document_role="CUSTOMER_SOR",
                    authorized_corpus_root=corpus,
                    run_id="agent-run-" + "7" * 32,
                    opaque_source_handle="source-handle-" + "8" * 32,
                ),
                repository_root=REPOSITORY_ROOT,
            )
            try:
                with self.assertRaisesRegex(
                    LifecycleProbeError,
                    "CANONICAL_STRUCTURE_HANDOFF_BINDING_MISSING",
                ):
                    session.require_inference_stage(
                        AgentExtractionStage.LOGICAL_STRUCTURE,
                        execution_mode=CANONICAL_STRUCTURE_HANDOFF_EXECUTION_MODE,
                    )
            finally:
                session.close()


class _Publisher:
    def __init__(self) -> None:
        self.last_result = {
            "attempt_id": "native-structure-handoff-attempt-" + "3" * 32,
            "status": "VERIFICATION_REQUIRED",
            "evidence_state": "CONFLICTING",
            "decision_policy": "FIRST_HOST_VALIDATED_PROPOSAL",
        }

    def publish(self, submission):  # type: ignore[no-untyped-def]
        return self.last_result


class _Transport:
    invocation = None

    def invoke(self, invocation):  # type: ignore[no-untyped-def]
        self.invocation = invocation
        structured_output = {
            "schema_version": "native-structure-handoff-result/v2",
            "status": "PUBLISHED",
            "reason_code": "revision_published",
            "evidence_state": "CONFLICTING",
            "decision_policy": "FIRST_HOST_VALIDATED_PROPOSAL",
            "public_summary": "Agent 已提交完整替换候选。",
        }
        return CanonicalStageTransportResult(
            stage=AgentExtractionStage.LOGICAL_STRUCTURE,
            structured_output=structured_output,
            output_schema_sha256=invocation.turn_output_schema_sha256,
            response_sha256=canonical_sha256(structured_output),
            tool_receipt_sha256="5" * 64,
            runtime_thread_id="thread-1",
            runtime_turn_id="turn-1",
            tool_activity_summary={},
        )


def _binding(
    *,
    run_id: str,
    source_sha256: str,
) -> CanonicalStructureHandoffBindingV1:
    values = {
        "run_id": run_id,
        "source_sha256": source_sha256,
        "artifact_id": "artifact-1",
        "handoff_id": "handoff-1",
        "input_fingerprint": "6" * 64,
        "status": "ACKNOWLEDGED",
        "affected_scope_ids": ("scope-1",),
        "work_item_count": 1,
    }
    return CanonicalStructureHandoffBindingV1(
        **values,
        binding_sha256=canonical_sha256(
            {
                "schema_version": "canonical-structure-handoff-binding/v1",
                "content_free": True,
                **values,
            }
        ),
    )


if __name__ == "__main__":
    unittest.main()
