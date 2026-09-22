from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


AGENT_KIT_IDENTITY_SCHEMA_VERSION = "reqpilotx-agent-kit-identity/v1"
AGENT_EXECUTION_REQUEST_SCHEMA_VERSION = "reqpilotx-agent-execution-request/v1"
AGENT_RUN_EVENT_SCHEMA_VERSION = "reqpilotx-agent-run-event/v1"
AGENT_STAGE_ARTIFACT_SCHEMA_VERSION = "reqpilotx-agent-stage-artifact/v1"
AGENT_EXECUTION_RESULT_SCHEMA_VERSION = "reqpilotx-agent-execution-result/v1"
AGENT_STAGE_FINAL_RECEIPT_SCHEMA_VERSION = "agent-stage-final-receipt/v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AgentExtractionStage(StrEnum):
    SOURCE_PREFLIGHT = "D0_SOURCE_PREFLIGHT"
    DOCUMENT_UNDERSTANDING = "D1_DOCUMENT_UNDERSTANDING"
    LOGICAL_STRUCTURE = "D2_LOGICAL_STRUCTURE"
    PARENT_PROVISION_RECOGNITION = "D3_PARENT_PROVISION_RECOGNITION"
    REQUIREMENT_DECOMPOSITION = "D4_REQUIREMENT_DECOMPOSITION"
    COMPLETENESS_VERIFICATION = "D5_COMPLETENESS_VERIFICATION"
    RESULT_ASSEMBLY = "D6_RESULT_ASSEMBLY"


AGENT_EXTRACTION_STAGE_ORDER = tuple(AgentExtractionStage)


class AgentWorkflowMode(StrEnum):
    """The two product workflows intentionally have different stage orders."""

    BASELINE = "BASELINE"
    ATOMIZATION = "ATOMIZATION"


AGENT_BASELINE_STAGE_ORDER = (
    AgentExtractionStage.SOURCE_PREFLIGHT,
    AgentExtractionStage.DOCUMENT_UNDERSTANDING,
    AgentExtractionStage.LOGICAL_STRUCTURE,
    AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
    AgentExtractionStage.COMPLETENESS_VERIFICATION,
    AgentExtractionStage.RESULT_ASSEMBLY,
)
AGENT_ATOMIZATION_STAGE_ORDER = (
    AgentExtractionStage.REQUIREMENT_DECOMPOSITION,
    AgentExtractionStage.COMPLETENESS_VERIFICATION,
    AgentExtractionStage.RESULT_ASSEMBLY,
)
AGENT_WORKFLOW_STAGE_ORDERS = {
    AgentWorkflowMode.BASELINE: AGENT_BASELINE_STAGE_ORDER,
    AgentWorkflowMode.ATOMIZATION: AGENT_ATOMIZATION_STAGE_ORDER,
}


class AgentRunnerKind(StrEnum):
    DESKTOP_REFERENCE = "DESKTOP_REFERENCE"
    APP_SERVER_PRODUCT = "APP_SERVER_PRODUCT"


class AgentExecutionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentStageArtifactStatus(StrEnum):
    VALIDATED = "VALIDATED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


class AgentRunEventKind(StrEnum):
    RUN_STARTED = "RUN_STARTED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_PROGRESS = "STAGE_PROGRESS"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    ARTIFACT_VALIDATED = "ARTIFACT_VALIDATED"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_BLOCKED = "RUN_BLOCKED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"


class AgentProgressBasis(StrEnum):
    SOURCE = "SOURCE"
    PAGES = "PAGES"
    LOGICAL_CONTAINERS = "LOGICAL_CONTAINERS"
    PARENT_PROVISIONS = "PARENT_PROVISIONS"
    ASSERTIONS = "ASSERTIONS"
    SOURCE_SCOPES = "SOURCE_SCOPES"


class AgentKitIdentityV1(_StrictFrozenModel):
    """Exact, runner-neutral identity of one Portable Agent Kit bundle."""

    schema_version: Literal["reqpilotx-agent-kit-identity/v1"] = (
        AGENT_KIT_IDENTITY_SCHEMA_VERSION
    )
    kit_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    kit_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    policy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    tool_registry_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    tool_registry_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")


class AgentRunnerIdentityV1(_StrictFrozenModel):
    runner_profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    runner_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_kind: AgentRunnerKind
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    adapter_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    runtime_version: str = Field(min_length=1, max_length=160)


class AgentExecutionRequestV1(_StrictFrozenModel):
    """Runner-neutral request. It intentionally contains no path or source text."""

    schema_version: Literal["reqpilotx-agent-execution-request/v1"] = (
        AGENT_EXECUTION_REQUEST_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    opaque_source_handle: str = Field(pattern=r"^source-handle-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: StrictInt = Field(ge=1)
    document_role: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    runner_profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    kit_identity: AgentKitIdentityV1
    workflow_mode: AgentWorkflowMode = AgentWorkflowMode.BASELINE
    stages: tuple[AgentExtractionStage, ...] = AGENT_BASELINE_STAGE_ORDER
    explicit_user_trigger: bool = False
    accepted_baseline_artifact_id: str | None = Field(
        default=None,
        pattern=r"^agent-artifact-[0-9a-f]{32}$",
    )
    accepted_baseline_artifact_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    selected_scope_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    customer_data_authorized: bool = False

    @model_validator(mode="after")
    def validate_workflow_stages(self) -> AgentExecutionRequestV1:
        _require_workflow_stage_prefix(
            workflow_mode=self.workflow_mode,
            stages=self.stages,
            subject="agent execution",
        )
        _require_workflow_bindings(
            workflow_mode=self.workflow_mode,
            explicit_user_trigger=self.explicit_user_trigger,
            accepted_baseline_artifact_id=self.accepted_baseline_artifact_id,
            accepted_baseline_artifact_sha256=(
                self.accepted_baseline_artifact_sha256
            ),
            selected_scope_sha256=self.selected_scope_sha256,
            subject="agent execution",
        )
        return self


class AgentStageArtifactV1(_StrictFrozenModel):
    """Content-free reference to one validated stage payload and its evidence."""

    schema_version: Literal["reqpilotx-agent-stage-artifact/v1"] = (
        AGENT_STAGE_ARTIFACT_SCHEMA_VERSION
    )
    artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: AgentExtractionStage
    status: AgentStageArtifactStatus
    proposal_schema_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}/v[0-9]+$")
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_ids: tuple[str, ...] = ()
    unresolved_scope_count: StrictInt = Field(default=0, ge=0)
    diagnostic_codes: tuple[str, ...] = ()

    @field_validator("predecessor_artifact_ids")
    @classmethod
    def validate_predecessors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            not item.startswith("agent-artifact-") for item in value
        ):
            raise ValueError("artifact predecessors must be unique artifact IDs")
        return value

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("artifact diagnostics must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_status(self) -> AgentStageArtifactV1:
        if self.status is AgentStageArtifactStatus.VALIDATED and (
            self.unresolved_scope_count or self.diagnostic_codes
        ):
            raise ValueError("validated artifacts cannot hide unresolved scopes")
        if self.status is not AgentStageArtifactStatus.VALIDATED and not (
            self.unresolved_scope_count or self.diagnostic_codes
        ):
            raise ValueError("partial or blocked artifacts require explicit evidence")
        return self


class AgentStageFinalReceiptV1(_StrictFrozenModel):
    """Small final model response bound to a Host-validated stage proposal."""

    schema_version: Literal["agent-stage-final-receipt/v1"]
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: AgentExtractionStage
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_status: AgentStageArtifactStatus
    proposal_schema_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}/v[0-9]+$")
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    validator_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")


class AgentRunEventV1(_StrictFrozenModel):
    """Content-free event consumed identically by every ReqPilotX runner."""

    schema_version: Literal["reqpilotx-agent-run-event/v1"] = (
        AGENT_RUN_EVENT_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    sequence: StrictInt = Field(ge=1)
    attempt: StrictInt = Field(default=1, ge=1)
    kind: AgentRunEventKind
    stage: AgentExtractionStage | None = None
    progress_basis: AgentProgressBasis | None = None
    completed_units: StrictInt = Field(default=0, ge=0)
    total_units: StrictInt = Field(default=0, ge=0)
    artifact_id: str | None = Field(
        default=None,
        pattern=r"^agent-artifact-[0-9a-f]{32}$",
    )
    message_code: str = Field(default="", pattern=r"^[a-z0-9_]{0,120}$")

    @model_validator(mode="after")
    def validate_event_shape(self) -> AgentRunEventV1:
        run_level = {
            AgentRunEventKind.RUN_STARTED,
            AgentRunEventKind.RUN_COMPLETED,
            AgentRunEventKind.RUN_BLOCKED,
            AgentRunEventKind.RUN_FAILED,
            AgentRunEventKind.RUN_CANCELLED,
        }
        if (self.kind in run_level) != (self.stage is None):
            raise ValueError("run-level and stage-level events must not be mixed")
        if self.completed_units > self.total_units:
            raise ValueError("event progress cannot exceed its total")
        if bool(self.progress_basis) != bool(self.total_units):
            raise ValueError("event progress basis and total must be supplied together")
        if (self.kind is AgentRunEventKind.ARTIFACT_VALIDATED) != bool(
            self.artifact_id
        ):
            raise ValueError("only artifact validation events bind an artifact")
        return self


class AgentExecutionResultV1(_StrictFrozenModel):
    schema_version: Literal["reqpilotx-agent-execution-result/v1"] = (
        AGENT_EXECUTION_RESULT_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    runner_identity: AgentRunnerIdentityV1
    workflow_mode: AgentWorkflowMode = AgentWorkflowMode.BASELINE
    requested_stages: tuple[AgentExtractionStage, ...]
    explicit_user_trigger: bool = False
    accepted_baseline_artifact_id: str | None = Field(
        default=None,
        pattern=r"^agent-artifact-[0-9a-f]{32}$",
    )
    accepted_baseline_artifact_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    selected_scope_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    status: AgentExecutionStatus
    events: tuple[AgentRunEventV1, ...]
    stage_artifacts: tuple[AgentStageArtifactV1, ...]
    diagnostic_codes: tuple[str, ...] = ()

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("execution diagnostics must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> AgentExecutionResultV1:
        _require_workflow_stage_prefix(
            workflow_mode=self.workflow_mode,
            stages=self.requested_stages,
            subject="agent result",
        )
        _require_workflow_bindings(
            workflow_mode=self.workflow_mode,
            explicit_user_trigger=self.explicit_user_trigger,
            accepted_baseline_artifact_id=self.accepted_baseline_artifact_id,
            accepted_baseline_artifact_sha256=(
                self.accepted_baseline_artifact_sha256
            ),
            selected_scope_sha256=self.selected_scope_sha256,
            subject="agent result",
        )
        if not self.events:
            raise ValueError("agent execution must preserve its event ledger")
        if tuple(event.sequence for event in self.events) != tuple(
            range(1, len(self.events) + 1)
        ):
            raise ValueError("agent event sequences must be contiguous")
        if any(event.run_id != self.run_id for event in self.events):
            raise ValueError("agent event run identity drifted")
        artifact_stages = tuple(item.stage for item in self.stage_artifacts)
        if artifact_stages != tuple(
            stage for stage in AGENT_EXTRACTION_STAGE_ORDER if stage in artifact_stages
        ) or len(artifact_stages) != len(set(artifact_stages)):
            raise ValueError("stage artifacts must be unique and canonically ordered")
        if artifact_stages != self.requested_stages[: len(artifact_stages)]:
            raise ValueError("stage artifacts must stay inside the requested stage prefix")
        if any(
            item.run_id != self.run_id or item.source_sha256 != self.source_sha256
            for item in self.stage_artifacts
        ):
            raise ValueError("stage artifact identity drifted")
        artifact_ids = {item.artifact_id for item in self.stage_artifacts}
        event_artifact_bindings = tuple(
            (event.artifact_id, event.stage)
            for event in self.events
            if event.artifact_id is not None
        )
        event_artifact_ids = tuple(item[0] for item in event_artifact_bindings)
        if (
            len(event_artifact_ids) != len(set(event_artifact_ids))
            or artifact_ids != set(event_artifact_ids)
        ):
            raise ValueError("event ledger must bind every stage artifact exactly once")
        artifact_stage_by_id = {
            item.artifact_id: item.stage for item in self.stage_artifacts
        }
        if any(
            artifact_stage_by_id[artifact_id] is not stage
            for artifact_id, stage in event_artifact_bindings
        ):
            raise ValueError("artifact validation event stage drifted")
        for index, artifact in enumerate(self.stage_artifacts):
            earlier_ids = {item.artifact_id for item in self.stage_artifacts[:index]}
            if any(
                predecessor not in earlier_ids
                for predecessor in artifact.predecessor_artifact_ids
            ):
                raise ValueError("artifact predecessor is missing or not yet validated")
            if index == 0 and artifact.predecessor_artifact_ids:
                raise ValueError("the first stage artifact cannot have a predecessor")
            if index > 0 and self.stage_artifacts[index - 1].artifact_id not in (
                artifact.predecessor_artifact_ids
            ):
                raise ValueError("stage artifacts must preserve immediate lineage")
        terminal_by_status = {
            AgentExecutionStatus.SUCCEEDED: AgentRunEventKind.RUN_COMPLETED,
            AgentExecutionStatus.PARTIAL: AgentRunEventKind.RUN_BLOCKED,
            AgentExecutionStatus.BLOCKED: AgentRunEventKind.RUN_BLOCKED,
            AgentExecutionStatus.FAILED: AgentRunEventKind.RUN_FAILED,
            AgentExecutionStatus.CANCELLED: AgentRunEventKind.RUN_CANCELLED,
        }
        if (
            self.events[0].kind is not AgentRunEventKind.RUN_STARTED
            or self.events[-1].kind is not terminal_by_status[self.status]
        ):
            raise ValueError("agent event ledger has an invalid start or terminal event")
        succeeded = self.status is AgentExecutionStatus.SUCCEEDED
        if succeeded and (
            artifact_stages != self.requested_stages
            or any(
                item.status is not AgentStageArtifactStatus.VALIDATED
                for item in self.stage_artifacts
            )
            or self.diagnostic_codes
        ):
            raise ValueError("successful execution requires all validated stages")
        if not succeeded and not self.diagnostic_codes:
            raise ValueError("non-success execution requires explicit diagnostics")
        return self


def _require_workflow_stage_prefix(
    *,
    workflow_mode: AgentWorkflowMode,
    stages: tuple[AgentExtractionStage, ...],
    subject: str,
) -> None:
    if not stages:
        raise ValueError(f"{subject} requires at least one stage")
    expected = AGENT_WORKFLOW_STAGE_ORDERS[workflow_mode]
    if stages != expected[: len(stages)]:
        raise ValueError(
            f"{subject} stages must be one {workflow_mode.value} workflow prefix"
        )


def _require_workflow_bindings(
    *,
    workflow_mode: AgentWorkflowMode,
    explicit_user_trigger: bool,
    accepted_baseline_artifact_id: str | None,
    accepted_baseline_artifact_sha256: str | None,
    selected_scope_sha256: str | None,
    subject: str,
) -> None:
    atomization_bindings = (
        accepted_baseline_artifact_id,
        accepted_baseline_artifact_sha256,
        selected_scope_sha256,
    )
    if workflow_mode is AgentWorkflowMode.BASELINE:
        if explicit_user_trigger or any(atomization_bindings):
            raise ValueError(
                f"{subject} baseline mode forbids atomization trigger and scope bindings"
            )
        return
    if not explicit_user_trigger or not all(atomization_bindings):
        raise ValueError(
            f"{subject} atomization requires an explicit user trigger, accepted "
            "baseline artifact, and selected scope"
        )


__all__ = [
    "AGENT_ATOMIZATION_STAGE_ORDER",
    "AGENT_BASELINE_STAGE_ORDER",
    "AGENT_EXECUTION_REQUEST_SCHEMA_VERSION",
    "AGENT_EXECUTION_RESULT_SCHEMA_VERSION",
    "AGENT_EXTRACTION_STAGE_ORDER",
    "AGENT_KIT_IDENTITY_SCHEMA_VERSION",
    "AGENT_RUN_EVENT_SCHEMA_VERSION",
    "AGENT_STAGE_ARTIFACT_SCHEMA_VERSION",
    "AGENT_STAGE_FINAL_RECEIPT_SCHEMA_VERSION",
    "AGENT_WORKFLOW_STAGE_ORDERS",
    "AgentExecutionRequestV1",
    "AgentExecutionResultV1",
    "AgentExecutionStatus",
    "AgentExtractionStage",
    "AgentKitIdentityV1",
    "AgentProgressBasis",
    "AgentRunEventKind",
    "AgentRunEventV1",
    "AgentRunnerIdentityV1",
    "AgentRunnerKind",
    "AgentStageArtifactStatus",
    "AgentStageArtifactV1",
    "AgentStageFinalReceiptV1",
    "AgentWorkflowMode",
]
