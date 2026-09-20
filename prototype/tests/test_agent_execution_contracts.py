from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from pydantic import ValidationError

from app.application.agent_workflow.extraction_runtime import (
    AGENT_ATOMIZATION_STAGE_ORDER,
    AGENT_BASELINE_STAGE_ORDER,
    AgentExecutionRequestV1,
    AgentExtractionStage,
    AgentKitIdentityV1,
    AgentRunEventKind,
    AgentRunEventV1,
    AgentStageArtifactStatus,
    AgentStageArtifactV1,
    AgentWorkflowMode,
)


def _kit_identity() -> AgentKitIdentityV1:
    return AgentKitIdentityV1(
        kit_id="reqpilotx-document-extraction",
        kit_version="0.1.0",
        manifest_sha256="1" * 64,
        bundle_sha256="2" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.0.0",
    )


class AgentExecutionContractTests(unittest.TestCase):
    def test_request_has_no_path_text_node_or_provider_contract(self) -> None:
        request = AgentExecutionRequestV1(
            run_id="agent-run-" + "a" * 32,
            opaque_source_handle="source-handle-" + "b" * 32,
            source_sha256="c" * 64,
            page_count=29,
            document_role="CUSTOMER_SOR",
            runner_profile_id="desktop-reference",
            model_id="gpt-5.6-sol",
            reasoning_effort="high",
            kit_identity=_kit_identity(),
            customer_data_authorized=True,
        )
        schema = request.model_json_schema()
        field_names = set(schema["properties"])

        self.assertFalse(
            field_names
            & {
                "path",
                "source_path",
                "source_text",
                "structure_node_ids",
                "provider_id",
            }
        )
        self.assertEqual(request.stages[0], AgentExtractionStage.SOURCE_PREFLIGHT)
        self.assertEqual(request.stages[-1], AgentExtractionStage.RESULT_ASSEMBLY)
        self.assertEqual(request.stages, AGENT_BASELINE_STAGE_ORDER)
        self.assertNotIn(
            AgentExtractionStage.REQUIREMENT_DECOMPOSITION,
            request.stages,
        )

    def test_request_rejects_stages_outside_selected_workflow(self) -> None:
        base = {
            "run_id": "agent-run-" + "a" * 32,
            "opaque_source_handle": "source-handle-" + "b" * 32,
            "source_sha256": "c" * 64,
            "page_count": 1,
            "document_role": "CUSTOMER_SOR",
            "runner_profile_id": "desktop-reference",
            "model_id": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "kit_identity": _kit_identity(),
        }
        with self.assertRaisesRegex(ValidationError, "BASELINE workflow prefix"):
            AgentExecutionRequestV1(
                **base,
                stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
            )
        with self.assertRaisesRegex(ValidationError, "BASELINE workflow prefix"):
            AgentExecutionRequestV1(
                **base,
                stages=(
                    AgentExtractionStage.SOURCE_PREFLIGHT,
                    AgentExtractionStage.LOGICAL_STRUCTURE,
                    AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                ),
            )
        with self.assertRaisesRegex(ValidationError, "BASELINE workflow prefix"):
            AgentExecutionRequestV1(
                **base,
                stages=tuple(AgentExtractionStage),
            )

        atomization = AgentExecutionRequestV1(
            **base,
            workflow_mode=AgentWorkflowMode.ATOMIZATION,
            stages=AGENT_ATOMIZATION_STAGE_ORDER,
            explicit_user_trigger=True,
            accepted_baseline_artifact_id="agent-artifact-" + "d" * 32,
            accepted_baseline_artifact_sha256="e" * 64,
            selected_scope_sha256="f" * 64,
        )
        self.assertEqual(atomization.stages, AGENT_ATOMIZATION_STAGE_ORDER)
        with self.assertRaisesRegex(ValidationError, "ATOMIZATION workflow prefix"):
            AgentExecutionRequestV1(
                **base,
                workflow_mode=AgentWorkflowMode.ATOMIZATION,
                stages=AGENT_BASELINE_STAGE_ORDER,
                explicit_user_trigger=True,
                accepted_baseline_artifact_id="agent-artifact-" + "d" * 32,
                accepted_baseline_artifact_sha256="e" * 64,
                selected_scope_sha256="f" * 64,
            )

    def test_atomization_requires_trigger_accepted_baseline_and_scope(self) -> None:
        base = {
            "run_id": "agent-run-" + "a" * 32,
            "opaque_source_handle": "source-handle-" + "b" * 32,
            "source_sha256": "c" * 64,
            "page_count": 1,
            "document_role": "CUSTOMER_SOR",
            "runner_profile_id": "desktop-reference",
            "model_id": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "kit_identity": _kit_identity(),
        }
        with self.assertRaisesRegex(ValidationError, "explicit user trigger"):
            AgentExecutionRequestV1(
                **base,
                workflow_mode=AgentWorkflowMode.ATOMIZATION,
                stages=AGENT_ATOMIZATION_STAGE_ORDER,
            )
        with self.assertRaisesRegex(ValidationError, "forbids atomization"):
            AgentExecutionRequestV1(
                **base,
                explicit_user_trigger=True,
                accepted_baseline_artifact_id="agent-artifact-" + "d" * 32,
                accepted_baseline_artifact_sha256="e" * 64,
                selected_scope_sha256="f" * 64,
            )

    def test_event_contract_is_content_free_and_binds_only_validated_artifacts(self) -> None:
        event = AgentRunEventV1(
            run_id="agent-run-" + "a" * 32,
            sequence=1,
            kind=AgentRunEventKind.ARTIFACT_VALIDATED,
            stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
            artifact_id="agent-artifact-" + "d" * 32,
            message_code="artifact_validated",
        )

        self.assertNotIn("text", event.model_dump())
        self.assertNotIn("reasoning", event.model_dump())
        with self.assertRaisesRegex(ValidationError, "bind an artifact"):
            AgentRunEventV1(
                run_id="agent-run-" + "a" * 32,
                sequence=1,
                kind=AgentRunEventKind.STAGE_COMPLETED,
                stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
                artifact_id="agent-artifact-" + "d" * 32,
            )

    def test_partial_artifact_requires_unresolved_evidence(self) -> None:
        base = {
            "artifact_id": "agent-artifact-" + "d" * 32,
            "run_id": "agent-run-" + "a" * 32,
            "source_sha256": "c" * 64,
            "stage": AgentExtractionStage.DOCUMENT_UNDERSTANDING,
            "proposal_schema_id": "document-understanding-proposal/v2",
            "payload_sha256": "1" * 64,
            "evidence_sha256": "2" * 64,
            "validator_receipt_sha256": "3" * 64,
        }
        with self.assertRaisesRegex(ValidationError, "explicit evidence"):
            AgentStageArtifactV1(
                **base,
                status=AgentStageArtifactStatus.PARTIAL,
            )
        partial = AgentStageArtifactV1(
            **base,
            status=AgentStageArtifactStatus.PARTIAL,
            unresolved_scope_count=1,
            diagnostic_codes=("visual_scope_unresolved",),
        )
        self.assertEqual(partial.unresolved_scope_count, 1)


if __name__ == "__main__":
    unittest.main()
