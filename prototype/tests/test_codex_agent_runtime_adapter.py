from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from dataclasses import replace
from pathlib import Path
import sys
import tempfile

from app.adapters.agents.codex_app_server import (
    CodexAppServerAgentRuntime,
    CodexDriverResult,
)
from app.adapters.agents.codex_app_server.source_requirement_window_executor import (
    LocalCanonicalSourceRequirementWindowExecutor,
)
from app.adapters.agents.codex_app_server.source_item_text_executor import (
    LocalCanonicalSourceItemTextExecutor,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import (
    create_structure_patch_proposal,
    get_base_effective_structure,
)
from app.domain.extraction.structure import StructureNodeKind
from app.domain.extraction.structure_revision import ReparentStructureOperationV1
from app.infrastructure.runtime_container import (
    _build_agent_runtime,
    _build_agent_runtime_components,
    _build_source_requirement_window_executor,
    _build_source_item_text_executor,
)
from app.infrastructure.runtime_settings import (
    AgentRuntimeBackend,
    DatabaseBackend,
    DeploymentMode,
    ObjectStoreBackend,
    ProcessRole,
    RuntimeSettings,
)
from app.ports.agent_runtime import (
    AgentRuntimeInvocationError,
    AgentStructureProposalTaskV1,
)
from tests.test_structure_revision import _source_ir_fixture


class _Driver:
    def __init__(self, proposal) -> None:
        self.proposal = proposal
        self.calls = 0

    def run(
        self,
        task,
        *,
        progress,
        cancellation_requested,
        steering=None,
    ):
        self.calls += 1
        progress("CODEX_TURN_RUNNING")
        if cancellation_requested():
            raise AssertionError("driver observed unexpected cancellation")
        return CodexDriverResult(
            proposal=self.proposal,
            runtime_thread_id="codex-thread-metadata",
            request_sha256="1" * 64,
            response_sha256="2" * 64,
            tool_receipt_sha256="3" * 64,
            output_schema_sha256="4" * 64,
            input_tokens=100,
            cached_input_tokens=20,
            output_tokens=30,
        )


class CodexAgentRuntimeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        source = _source_ir_fixture()
        tree = build_structure_tree(source)
        current = get_base_effective_structure(source, tree)
        target = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.parent_structure_node_id is not None
        )
        parent = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.SECTION
            and node.structure_node_id != target.parent_structure_node_id
        )
        proposal = create_structure_patch_proposal(
            source,
            tree,
            (),
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=parent.structure_node_id,
                    new_sibling_index=0,
                ),
            ),
        )
        self.driver = _Driver(proposal)
        self.runtime = CodexAppServerAgentRuntime(
            self.driver,
            runtime_version="codex-cli 0.148.0-alpha.15",
            model_id="gpt-5.6-sol",
        )
        self.task = AgentStructureProposalTaskV1(
            run_id="job-1",
            project_id="project-1",
            document_id="document-1",
            facts_sha256="5" * 64,
            source_ir=source,
            base_structure=tree,
            structure_revisions=(),
            effective_structure=current,
            model_id="gpt-5.6-sol",
            reasoning_effort="medium",
            skill_id="reqpilotx-structure-tree-generation",
            skill_version="1.1.0",
            deadline_seconds=180,
        )

    def test_adapter_maps_driver_result_to_content_free_runtime_receipt(self) -> None:
        progress: list[str] = []
        result = self.runtime.propose_structure(
            self.task,
            progress=progress.append,
            cancellation_requested=lambda: False,
        )

        self.assertEqual(result.proposal, self.driver.proposal)
        self.assertEqual(result.runtime_thread_id, "codex-thread-metadata")
        self.assertEqual(result.runtime_receipt.runtime_adapter_id, "codex-app-server")
        self.assertEqual(result.runtime_receipt.model_id, "gpt-5.6-sol")
        self.assertEqual(result.runtime_receipt.input_tokens, 100)
        self.assertEqual(progress, ["CODEX_TURN_RUNNING"])

    def test_adapter_rejects_identity_drift_and_pre_start_cancellation(self) -> None:
        drifted = replace(self.task, model_id="other-model")
        with self.assertRaisesRegex(
            AgentRuntimeInvocationError,
            "agent_runtime_task_identity_mismatch",
        ):
            self.runtime.propose_structure(
                drifted,
                progress=lambda _stage: None,
                cancellation_requested=lambda: False,
            )
        with self.assertRaisesRegex(
            AgentRuntimeInvocationError,
            "agent_runtime_cancelled_before_start",
        ):
            self.runtime.propose_structure(
                self.task,
                progress=lambda _stage: None,
                cancellation_requested=lambda: True,
            )
        self.assertEqual(self.driver.calls, 0)

    def test_runtime_factory_selects_only_the_explicit_codex_backend(self) -> None:
        settings = RuntimeSettings(
            deployment_mode=DeploymentMode.LOCAL,
            process_role=ProcessRole.WORKER,
            database_backend=DatabaseBackend.POSTGRESQL,
            database_url="postgresql://localhost/reqpilotx",
            object_store_backend=ObjectStoreBackend.LOCAL,
            object_root=Path("/tmp/reqpilotx-test-objects"),
            allowed_hosts=("localhost",),
            allowed_origins=("http://localhost",),
            agent_runtime_backend=AgentRuntimeBackend.CODEX_APP_SERVER,
            codex_bin=Path("/opt/reqpilotx/codex"),
            codex_home=Path("/opt/reqpilotx/codex-home"),
            agent_model_id="gpt-5.6-sol",
        )

        runtime = _build_agent_runtime(settings)

        self.assertIsInstance(runtime, CodexAppServerAgentRuntime)
        self.assertEqual(runtime.model_id, "gpt-5.6-sol")
        self.assertEqual(
            runtime._driver.codex_bin,
            Path("/opt/reqpilotx/codex"),
        )
        self.assertEqual(
            runtime._driver.repository_root,
            Path(__file__).resolve().parents[2],
        )
        self.assertIsNone(
            _build_agent_runtime(
                replace(settings, agent_runtime_backend=AgentRuntimeBackend.NONE)
            )
        )

        _runtime, semantic, atomization = _build_agent_runtime_components(settings)
        self.assertIsNotNone(semantic)
        assert semantic is not None
        self.assertTrue(semantic._driver.reuse_document_session)
        self.assertEqual(semantic._repetition_count, 1)
        self.assertEqual(semantic._max_concurrent_repetitions, 1)
        self.assertIs(atomization, semantic)

    def test_runtime_factory_configures_source_requirement_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = RuntimeSettings(
                deployment_mode=DeploymentMode.LOCAL,
                process_role=ProcessRole.WORKER,
                database_backend=DatabaseBackend.POSTGRESQL,
                database_url="postgresql://localhost/reqpilotx",
                object_store_backend=ObjectStoreBackend.LOCAL,
                object_root=Path(temporary) / "objects",
                allowed_hosts=("localhost",),
                allowed_origins=("http://localhost",),
                agent_runtime_backend=AgentRuntimeBackend.CODEX_APP_SERVER,
                codex_bin=Path(sys.executable),
                codex_home=Path(temporary),
                agent_model_id="gpt-5.6-sol",
            )

            executor = _build_source_requirement_window_executor(settings)

            self.assertIsInstance(
                executor,
                LocalCanonicalSourceRequirementWindowExecutor,
            )
            assert executor is not None
            self.assertEqual(executor.model_id, "gpt-5.6-sol")
            self.assertEqual(
                executor._repository_root,
                Path(__file__).resolve().parents[2],
            )
            self.assertIsNone(
                _build_source_requirement_window_executor(
                    replace(
                        settings,
                        agent_runtime_backend=AgentRuntimeBackend.NONE,
                    )
                )
            )
            text_executor = _build_source_item_text_executor(settings)
            self.assertIsInstance(
                text_executor,
                LocalCanonicalSourceItemTextExecutor,
            )
            self.assertIsNone(
                _build_source_item_text_executor(
                    replace(
                        settings,
                        agent_runtime_backend=AgentRuntimeBackend.NONE,
                    )
                )
            )


if __name__ == "__main__":
    unittest.main()
