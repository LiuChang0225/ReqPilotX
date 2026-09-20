from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CANONICAL_STRUCTURE_HANDOFF_EXECUTION_MODE,
    CanonicalAppServerDocumentSession,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageTransportResult,
    CanonicalStructuredStageTransportPort,
    build_canonical_stage_invocation,
)
from app.adapters.agents.codex_app_server.canonical_structure_handoff_tools import (
    CanonicalNativeStructureHandoffToolHost,
)
from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.adapters.agents.codex_app_server.native_structure_revision_publisher import (
    NativeStructureRevisionPublisher,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage


NATIVE_STRUCTURE_HANDOFF_RESULT_SCHEMA_ID = "native-structure-handoff-result/v2"


class NativeStructureHandoffTurnResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal["native-structure-handoff-result/v2"] = (
        NATIVE_STRUCTURE_HANDOFF_RESULT_SCHEMA_ID
    )
    status: Literal["PUBLISHED", "NO_CHANGE"]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")
    evidence_state: Literal["SUFFICIENT", "CONFLICTING", "INSUFFICIENT"]
    decision_policy: Literal[
        "FIRST_HOST_VALIDATED_PROPOSAL",
        "KEEP_CURRENT_STRUCTURE",
    ]
    public_summary: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_status(self) -> NativeStructureHandoffTurnResultV1:
        allowed = {
            "PUBLISHED": {"revision_published"},
            "NO_CHANGE": {
                "source_already_satisfies_handoff",
                "no_grounded_change",
                "current_structure_selected",
            },
        }[self.status]
        if self.reason_code not in allowed:
            raise ValueError("handoff result reason does not match status")
        expected_policy = (
            "FIRST_HOST_VALIDATED_PROPOSAL"
            if self.status == "PUBLISHED"
            else "KEEP_CURRENT_STRUCTURE"
        )
        if self.decision_policy != expected_policy:
            raise ValueError("handoff result policy does not match status")
        return self


@dataclass(frozen=True, slots=True)
class CanonicalStructureHandoffExecutionResult:
    turn_result: NativeStructureHandoffTurnResultV1
    publication: Mapping[str, object] | None
    transport_result: CanonicalStageTransportResult


class CanonicalStructureHandoffDriver:
    def __init__(
        self,
        *,
        transport: CanonicalStructuredStageTransportPort,
        dynamic_tool_host: CanonicalNativeStructureHandoffToolHost,
        revision_publisher: NativeStructureRevisionPublisher,
    ) -> None:
        self.transport = transport
        self.dynamic_tool_host = dynamic_tool_host
        self.revision_publisher = revision_publisher

    def run(
        self,
        session: CanonicalAppServerDocumentSession,
        *,
        task_binding_sha256: str,
    ) -> CanonicalStructureHandoffExecutionResult:
        activation = session.require_inference_stage(
            AgentExtractionStage.LOGICAL_STRUCTURE,
            execution_mode=CANONICAL_STRUCTURE_HANDOFF_EXECUTION_MODE,
        )
        try:
            output_schema = json.loads(
                activation.runner_output_schema_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise LifecycleProbeError(
                "CANONICAL_STRUCTURE_HANDOFF_SCHEMA_INVALID"
            ) from exc
        if not isinstance(output_schema, Mapping):
            raise LifecycleProbeError("CANONICAL_STRUCTURE_HANDOFF_SCHEMA_INVALID")
        invocation = build_canonical_stage_invocation(
            activation,
            task_binding_sha256=task_binding_sha256,
            turn_output_schema_id=NATIVE_STRUCTURE_HANDOFF_RESULT_SCHEMA_ID,
            turn_output_schema=dict(output_schema),
            expected_final_artifact_schema_id="logical-structure-proposal/v2",
            expected_validator_id="reqpilotx-logical-structure-validator",
            expected_validator_version="1.2.0",
            dynamic_tool_specs=self.dynamic_tool_host.specs,
            task_directive=(
                "This is one Host-bound structure handoff. Call "
                "reqpilotx_structure_get_handoff_context exactly once. Use "
                "reqpilotx_structure_inspect_scope only when the returned evidence "
                "cannot decide a requested change. If a grounded change is needed, "
                "publish one complete replacement with "
                "reqpilotx_structure_publish_revision; never publish micro-updates. "
                "Always make one working decision. If ambiguity remains and a "
                "source-grounded change can be expressed with supported Host "
                "operations, publish the first Host-valid reversible proposal without "
                "waiting for user confirmation and mark ambiguous work items "
                "KEPT_UNRESOLVED. If no change should be published, explicitly select "
                "the current structure with NO_CHANGE/current_structure_selected. "
                "Evidence may be SUFFICIENT, CONFLICTING, or INSUFFICIENT, but it is "
                "metadata and never a per-item workflow blocker. Never return an "
                "UNRESOLVED terminal status. "
                "Return PUBLISHED only after that Host tool succeeds. A published "
                "candidate remains verification-required."
                " Final status/reason mapping is exact: PUBLISHED uses "
                "revision_published; NO_CHANGE uses "
                "source_already_satisfies_handoff, no_grounded_change, or "
                "current_structure_selected. PUBLISHED uses policy "
                "FIRST_HOST_VALIDATED_PROPOSAL; NO_CHANGE uses "
                "KEEP_CURRENT_STRUCTURE."
            ),
        )
        result = self.transport.invoke(invocation)
        try:
            turn_result = NativeStructureHandoffTurnResultV1.model_validate(
                result.structured_output
            )
        except Exception as exc:
            raise LifecycleProbeError(
                "CANONICAL_STRUCTURE_HANDOFF_OUTPUT_INVALID"
            ) from exc
        publication = self.revision_publisher.last_result
        if self.dynamic_tool_host.context_call_count != 1:
            raise LifecycleProbeError(
                "CANONICAL_STRUCTURE_HANDOFF_CONTEXT_CALL_INVALID"
            )
        if (turn_result.status == "PUBLISHED") != (publication is not None):
            raise LifecycleProbeError(
                "CANONICAL_STRUCTURE_HANDOFF_PUBLICATION_MISMATCH"
            )
        if publication is not None and publication.get("status") != (
            "VERIFICATION_REQUIRED"
        ):
            raise LifecycleProbeError(
                "CANONICAL_STRUCTURE_HANDOFF_PUBLICATION_STATUS_INVALID"
            )
        if publication is not None and (
            publication.get("evidence_state") != turn_result.evidence_state
            or publication.get("decision_policy") != turn_result.decision_policy
        ):
            raise LifecycleProbeError(
                "CANONICAL_STRUCTURE_HANDOFF_DECISION_METADATA_MISMATCH"
            )
        return CanonicalStructureHandoffExecutionResult(
            turn_result=turn_result,
            publication=publication,
            transport_result=result,
        )


__all__ = [
    "CanonicalStructureHandoffDriver",
    "CanonicalStructureHandoffExecutionResult",
    "NATIVE_STRUCTURE_HANDOFF_RESULT_SCHEMA_ID",
    "NativeStructureHandoffTurnResultV1",
]
