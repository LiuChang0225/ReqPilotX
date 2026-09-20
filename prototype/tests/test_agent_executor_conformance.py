from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
import unittest

from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.adapters.agents.portable_runtime import (
    CodexAppServerProductAgentExecutor,
    DesktopReferenceAgentExecutor,
    PortableAgentDriverResult,
)
from app.application.agent_workflow.extraction_runtime import (
    AGENT_ATOMIZATION_STAGE_ORDER,
    AgentExecutionRequestV1,
    AgentExecutionStatus,
    AgentExtractionStage,
    AgentRunEventKind,
    AgentRunEventV1,
    AgentStageArtifactStatus,
    AgentStageArtifactV1,
    AgentWorkflowMode,
)
from app.ports.agent_runtime import AgentRuntimeInvocationError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _SyntheticDriver:
    def __init__(self, kit, runtime_version: str) -> None:
        self.kit = kit
        self.runtime_version = runtime_version

    def run(self, request, *, emit_event, cancellation_requested):  # type: ignore[no-untyped-def]
        sequence = 1
        emit_event(
            AgentRunEventV1(
                run_id=request.run_id,
                sequence=sequence,
                kind=AgentRunEventKind.RUN_STARTED,
                message_code="run_started",
            )
        )
        artifacts = []
        predecessor_ids: tuple[str, ...] = ()
        stage_specs = tuple(
            item for item in self.kit.manifest.stages if item.stage in request.stages
        )
        for index, stage_spec in enumerate(stage_specs, start=1):
            if cancellation_requested():
                raise AssertionError("unexpected cancellation")
            sequence += 1
            emit_event(
                AgentRunEventV1(
                    run_id=request.run_id,
                    sequence=sequence,
                    kind=AgentRunEventKind.STAGE_STARTED,
                    stage=stage_spec.stage,
                    message_code="stage_started",
                )
            )
            artifact = AgentStageArtifactV1(
                artifact_id=f"agent-artifact-{index:032x}",
                run_id=request.run_id,
                source_sha256=request.source_sha256,
                stage=stage_spec.stage,
                status=AgentStageArtifactStatus.VALIDATED,
                proposal_schema_id=stage_spec.artifact_schema_id,
                payload_sha256=f"{index:x}" * 64,
                evidence_sha256=f"{index + 7:x}" * 64,
                validator_receipt_sha256=f"{index + 8:x}" * 64,
                predecessor_artifact_ids=predecessor_ids,
            )
            artifacts.append(artifact)
            predecessor_ids = (artifact.artifact_id,)
            sequence += 1
            emit_event(
                AgentRunEventV1(
                    run_id=request.run_id,
                    sequence=sequence,
                    kind=AgentRunEventKind.ARTIFACT_VALIDATED,
                    stage=stage_spec.stage,
                    artifact_id=artifact.artifact_id,
                    message_code="artifact_validated",
                )
            )
            sequence += 1
            emit_event(
                AgentRunEventV1(
                    run_id=request.run_id,
                    sequence=sequence,
                    kind=AgentRunEventKind.STAGE_COMPLETED,
                    stage=stage_spec.stage,
                    message_code="stage_completed",
                )
            )
        sequence += 1
        emit_event(
            AgentRunEventV1(
                run_id=request.run_id,
                sequence=sequence,
                kind=AgentRunEventKind.RUN_COMPLETED,
                message_code="run_completed",
            )
        )
        return PortableAgentDriverResult(
            runtime_version=self.runtime_version,
            status=AgentExecutionStatus.SUCCEEDED,
            stage_artifacts=tuple(artifacts),
        )


def _request(kit, profile_id: str) -> AgentExecutionRequestV1:  # type: ignore[no-untyped-def]
    profile = kit.runner_profile(profile_id)
    return AgentExecutionRequestV1(
        run_id="agent-run-" + "a" * 32,
        opaque_source_handle="source-handle-" + "b" * 32,
        source_sha256="c" * 64,
        page_count=3,
        document_role="CUSTOMER_SOR",
        runner_profile_id=profile_id,
        model_id=profile.model_id,
        reasoning_effort=profile.reasoning_effort,
        kit_identity=kit.identity,
        customer_data_authorized=True,
    )


class AgentExecutorConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kit = load_portable_agent_kit(REPOSITORY_ROOT / "agent-kit")

    def test_desktop_and_app_server_share_contract_and_stage_artifacts(self) -> None:
        desktop = DesktopReferenceAgentExecutor(
            self.kit,
            _SyntheticDriver(self.kit, "codex-desktop-test"),
        )
        app_server = CodexAppServerProductAgentExecutor(
            self.kit,
            _SyntheticDriver(self.kit, "codex-app-server-test"),
        )
        desktop_events = []
        product_events = []

        desktop_result = desktop.execute(
            _request(self.kit, "desktop-reference"),
            emit_event=desktop_events.append,
            cancellation_requested=lambda: False,
        )
        product_result = app_server.execute(
            _request(self.kit, "app-server-product"),
            emit_event=product_events.append,
            cancellation_requested=lambda: False,
        )

        self.assertEqual(desktop_result.kit_identity, product_result.kit_identity)
        self.assertEqual(desktop_result.events, product_result.events)
        self.assertEqual(
            desktop_result.stage_artifacts,
            product_result.stage_artifacts,
        )
        self.assertNotEqual(
            desktop_result.runner_identity.adapter_id,
            product_result.runner_identity.adapter_id,
        )
        self.assertNotEqual(
            desktop_result.runner_identity.runtime_version,
            product_result.runner_identity.runtime_version,
        )

    def test_executor_rejects_kit_and_profile_drift_before_driver(self) -> None:
        executor = DesktopReferenceAgentExecutor(
            self.kit,
            _SyntheticDriver(self.kit, "codex-desktop-test"),
        )
        request = _request(self.kit, "desktop-reference")
        drifted_identity = request.kit_identity.model_copy(
            update={"bundle_sha256": "f" * 64}
        )
        with self.assertRaisesRegex(
            AgentRuntimeInvocationError,
            "portable_agent_kit_identity_mismatch",
        ):
            executor.execute(
                request.model_copy(update={"kit_identity": drifted_identity}),
                emit_event=lambda _event: None,
                cancellation_requested=lambda: False,
            )
        with self.assertRaisesRegex(
            AgentRuntimeInvocationError,
            "portable_agent_runner_profile_mismatch",
        ):
            executor.execute(
                request.model_copy(update={"runner_profile_id": "app-server-product"}),
                emit_event=lambda _event: None,
                cancellation_requested=lambda: False,
            )

    def test_d0_d1_prefix_can_succeed_without_fabricating_later_artifacts(self) -> None:
        executor = DesktopReferenceAgentExecutor(
            self.kit,
            _SyntheticDriver(self.kit, "codex-desktop-test"),
        )
        request = _request(self.kit, "desktop-reference").model_copy(
            update={
                "stages": (
                    AgentExtractionStage.SOURCE_PREFLIGHT,
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                )
            }
        )

        result = executor.execute(
            request,
            emit_event=lambda _event: None,
            cancellation_requested=lambda: False,
        )

        self.assertEqual(result.status, AgentExecutionStatus.SUCCEEDED)
        self.assertEqual(result.requested_stages, request.stages)
        self.assertEqual(
            tuple(item.stage for item in result.stage_artifacts),
            request.stages,
        )

    def test_explicit_atomization_uses_d4_d5_d6_without_replaying_baseline(self) -> None:
        executor = DesktopReferenceAgentExecutor(
            self.kit,
            _SyntheticDriver(self.kit, "codex-desktop-test"),
        )
        base = _request(self.kit, "desktop-reference")
        request = AgentExecutionRequestV1(
            **base.model_dump(
                mode="python",
                exclude={
                    "workflow_mode",
                    "stages",
                    "explicit_user_trigger",
                    "accepted_baseline_artifact_id",
                    "accepted_baseline_artifact_sha256",
                    "selected_scope_sha256",
                },
            ),
            workflow_mode=AgentWorkflowMode.ATOMIZATION,
            stages=AGENT_ATOMIZATION_STAGE_ORDER,
            explicit_user_trigger=True,
            accepted_baseline_artifact_id="agent-artifact-" + "d" * 32,
            accepted_baseline_artifact_sha256="e" * 64,
            selected_scope_sha256="f" * 64,
        )

        result = executor.execute(
            request,
            emit_event=lambda _event: None,
            cancellation_requested=lambda: False,
        )

        self.assertIs(result.workflow_mode, AgentWorkflowMode.ATOMIZATION)
        self.assertEqual(result.requested_stages, AGENT_ATOMIZATION_STAGE_ORDER)
        self.assertTrue(result.explicit_user_trigger)
        self.assertEqual(
            result.accepted_baseline_artifact_id,
            request.accepted_baseline_artifact_id,
        )
        self.assertEqual(
            result.accepted_baseline_artifact_sha256,
            request.accepted_baseline_artifact_sha256,
        )
        self.assertEqual(
            result.selected_scope_sha256,
            request.selected_scope_sha256,
        )
        self.assertEqual(
            tuple(item.stage for item in result.stage_artifacts),
            AGENT_ATOMIZATION_STAGE_ORDER,
        )

    def test_customer_document_requires_explicit_authorization(self) -> None:
        executor = DesktopReferenceAgentExecutor(
            self.kit,
            _SyntheticDriver(self.kit, "codex-desktop-test"),
        )
        request = _request(self.kit, "desktop-reference").model_copy(
            update={"customer_data_authorized": False}
        )
        with self.assertRaisesRegex(
            AgentRuntimeInvocationError,
            "portable_agent_customer_data_not_authorized",
        ):
            executor.execute(
                request,
                emit_event=lambda _event: None,
                cancellation_requested=lambda: False,
            )


if __name__ == "__main__":
    unittest.main()
