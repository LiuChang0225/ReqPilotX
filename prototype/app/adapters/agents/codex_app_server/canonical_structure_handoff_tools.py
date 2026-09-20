from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.adapters.agents.codex_app_server.canonical_dynamic_tools import (
    CANONICAL_DYNAMIC_TOOL_RESULT_SCHEMA_VERSION,
    MAX_CANONICAL_DYNAMIC_TOOL_RESULT_BYTES,
    CanonicalDynamicToolExecution,
    CanonicalDynamicToolSpec,
    CanonicalStructureRevisionPublishError,
    CanonicalStructureRevisionPublisher,
    CanonicalStructureRevisionSubmissionInputV1,
    StructureFailurePatternSignalsInputV1,
)
from app.adapters.agents.structure_failure_pattern_host import (
    StructureFailurePatternHost,
)
from app.application.agent_workflow import native_structure_bridge
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.agent_workflow.structure_tool_loop import (
    NativeStructureHostToolRequestV1,
    StructureToolIntent,
)
from app.application.errors import ApplicationError


TOOL_STRUCTURE_GET_HANDOFF_CONTEXT = "reqpilotx_structure_get_handoff_context"
TOOL_STRUCTURE_INSPECT_SCOPE = "reqpilotx_structure_inspect_scope"
TOOL_STRUCTURE_SELECT_HANDOFF_FAILURE_PATTERNS = (
    "reqpilotx_structure_select_failure_patterns"
)
TOOL_STRUCTURE_PUBLISH_HANDOFF_REVISION = "reqpilotx_structure_publish_revision"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class _EmptyInputV1(_StrictModel):
    pass


class CanonicalStructureInspectScopeInputV1(_StrictModel):
    scope_id: str = Field(min_length=1, max_length=200)
    intent: StructureToolIntent
    reason_signals: tuple[str, ...] = Field(default=(), max_length=3)
    logical_table_scope_id: str | None = Field(default=None, max_length=200)

    @field_validator("reason_signals")
    @classmethod
    def validate_signals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item or not item.replace("_", "a").isalnum() for item in value
        ):
            raise ValueError("inspection signals must be unique sorted machine codes")
        return value


class CanonicalNativeStructureHandoffToolHost:
    """Four high-level, Host-bound tools for one acknowledged D2 handoff."""

    def __init__(
        self,
        *,
        project_id: str,
        document_id: str,
        handoff_id: str,
        revision_publisher: CanonicalStructureRevisionPublisher,
        failure_pattern_host: StructureFailurePatternHost | None = None,
    ) -> None:
        self.project_id = project_id
        self.document_id = document_id
        self.handoff_id = handoff_id
        self.revision_publisher = revision_publisher
        self.failure_pattern_host = (
            failure_pattern_host or StructureFailurePatternHost()
        )
        self.context_call_count = 0
        self._specs = _specs()

    @property
    def supported_stages(self) -> tuple[AgentExtractionStage, ...]:
        return (AgentExtractionStage.LOGICAL_STRUCTURE,)

    @property
    def specs(self) -> tuple[CanonicalDynamicToolSpec, ...]:
        return self._specs

    def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> CanonicalDynamicToolExecution:
        try:
            if tool_name == TOOL_STRUCTURE_GET_HANDOFF_CONTEXT:
                _EmptyInputV1.model_validate(arguments)
                self.context_call_count += 1
                result = native_structure_bridge.get_native_structure_handoff_context(
                    self.project_id,
                    self.document_id,
                    self.handoff_id,
                )
            elif tool_name == TOOL_STRUCTURE_INSPECT_SCOPE:
                request = CanonicalStructureInspectScopeInputV1.model_validate(
                    arguments
                )
                context = native_structure_bridge.get_native_structure_handoff_context(
                    self.project_id,
                    self.document_id,
                    self.handoff_id,
                )
                command_id = "canonical-handoff-tool:" + _sha256(
                    {
                        "handoff_id": self.handoff_id,
                        **request.model_dump(mode="json"),
                    }
                )
                result = native_structure_bridge.invoke_native_structure_host_tool(
                    self.project_id,
                    self.document_id,
                    NativeStructureHostToolRequestV1(
                        command_id=command_id,
                        expected_source_sha256=str(context["source_sha256"]),
                        expected_artifact_id=str(context["artifact_id"]),
                        handoff_id=self.handoff_id,
                        scope_id=request.scope_id,
                        intent=request.intent,
                        reason_signals=request.reason_signals,
                        logical_table_scope_id=request.logical_table_scope_id,
                    ),
                    SimpleNamespace(headers={}),
                )
            elif tool_name == TOOL_STRUCTURE_SELECT_HANDOFF_FAILURE_PATTERNS:
                request = StructureFailurePatternSignalsInputV1.model_validate(
                    arguments
                )
                result = self.failure_pattern_host.select(request.signals)
            elif tool_name == TOOL_STRUCTURE_PUBLISH_HANDOFF_REVISION:
                request = CanonicalStructureRevisionSubmissionInputV1.model_validate(
                    arguments
                )
                result = self.revision_publisher.publish(request)
            else:
                return _failure("dynamic_tool_unknown")
        except ValidationError:
            return _failure("dynamic_tool_arguments_invalid")
        except CanonicalStructureRevisionPublishError as exc:
            return _failure(exc.reason_code)
        except ApplicationError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            return _failure(str(detail.get("code") or "dynamic_host_call_rejected"))
        return _success(tool_name, result)


def _specs() -> tuple[CanonicalDynamicToolSpec, ...]:
    return (
        CanonicalDynamicToolSpec(
            name=TOOL_STRUCTURE_GET_HANDOFF_CONTEXT,
            description=(
                "Read the one Host-bound structure handoff: affected nodes, exact "
                "source content/anchors, attached logical tables, user feedback, "
                "proposal decisions, prior attempt, and current materialization hash. "
                "Call once at the start of this handoff turn."
            ),
            input_schema=_EmptyInputV1.model_json_schema(),
        ),
        CanonicalDynamicToolSpec(
            name=TOOL_STRUCTURE_INSPECT_SCOPE,
            description=(
                "Run one bounded Host evidence or advisory-candidate operation for an "
                "affected scope. Use only when the handoff context cannot decide the "
                "change; the result never overrides immutable source evidence."
            ),
            input_schema=CanonicalStructureInspectScopeInputV1.model_json_schema(),
        ),
        CanonicalDynamicToolSpec(
            name=TOOL_STRUCTURE_SELECT_HANDOFF_FAILURE_PATTERNS,
            description=(
                "Resolve at most three abstract reminders for current observed failure "
                "signals. Do not call without a current signal."
            ),
            input_schema=StructureFailurePatternSignalsInputV1.model_json_schema(),
        ),
        CanonicalDynamicToolSpec(
            name=TOOL_STRUCTURE_PUBLISH_HANDOFF_REVISION,
            description=(
                "Publish one complete replacement candidate for the Host-bound handoff. "
                "The Host injects all source, artifact, handoff, attempt, command, and "
                "persistence identities. For ambiguity, publish the first Host-valid "
                "reversible proposal and mark the work item KEPT_UNRESOLVED; user "
                "confirmation is not required. Always declare evidence_state as "
                "SUFFICIENT, CONFLICTING, or INSUFFICIENT. Success remains "
                "verification-required."
            ),
            input_schema=CanonicalStructureRevisionSubmissionInputV1.model_json_schema(),
        ),
    )


def _success(
    tool_name: str, result: Mapping[str, Any]
) -> CanonicalDynamicToolExecution:
    envelope = {
        "schema_version": CANONICAL_DYNAMIC_TOOL_RESULT_SCHEMA_VERSION,
        "tool_name": tool_name,
        "authority": "HOST_EVIDENCE_OR_VALIDATION",
        "result": dict(result),
    }
    encoded = _json(envelope).encode("utf-8")
    if len(encoded) > MAX_CANONICAL_DYNAMIC_TOOL_RESULT_BYTES:
        return _failure(
            "dynamic_tool_result_too_large",
            result_sha256=hashlib.sha256(encoded).hexdigest(),
        )
    return CanonicalDynamicToolExecution(success=True, result=envelope)


def _failure(
    reason_code: str,
    *,
    result_sha256: str | None = None,
) -> CanonicalDynamicToolExecution:
    result: dict[str, Any] = {
        "schema_version": CANONICAL_DYNAMIC_TOOL_RESULT_SCHEMA_VERSION,
        "authority": "HOST_FAILURE",
        "reason_code": reason_code,
    }
    if result_sha256 is not None:
        result["result_sha256"] = result_sha256
    return CanonicalDynamicToolExecution(success=False, result=result)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "CanonicalNativeStructureHandoffToolHost",
    "CanonicalStructureInspectScopeInputV1",
    "TOOL_STRUCTURE_GET_HANDOFF_CONTEXT",
    "TOOL_STRUCTURE_INSPECT_SCOPE",
    "TOOL_STRUCTURE_PUBLISH_HANDOFF_REVISION",
    "TOOL_STRUCTURE_SELECT_HANDOFF_FAILURE_PATTERNS",
]
