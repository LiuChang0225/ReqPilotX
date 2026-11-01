from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import hashlib
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictInt,
    field_validator,
    model_serializer,
    model_validator,
)

from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage
from reqpilotx_server.infrastructure.storage_lock import acquire_file_lease


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class WorkflowConditionDisposition(StrEnum):
    HARD_BLOCKER = "HARD_BLOCKER"
    CONTINUE_WITH_RISK = "CONTINUE_WITH_RISK"
    RECOVERABLE_FAILURE = "RECOVERABLE_FAILURE"
    INFO = "INFO"


class WorkflowConditionState(StrEnum):
    ACTIVE = "ACTIVE"
    RECOVERING = "RECOVERING"
    RESOLVED = "RESOLVED"
    DEFERRED = "DEFERRED"


class WorkflowCapability(StrEnum):
    READ_AUTHORIZED_SOURCE = "READ_AUTHORIZED_SOURCE"
    EXECUTE_SCOPE = "EXECUTE_SCOPE"
    VALIDATE_STAGE = "VALIDATE_STAGE"
    APPLY_PROPOSAL = "APPLY_PROPOSAL"
    PUBLISH_DRAFT = "PUBLISH_DRAFT"
    LOCK_FINAL = "LOCK_FINAL"


class WorkflowCapabilityDecision(StrEnum):
    ALLOW = "ALLOW"
    ALLOW_WITH_RISKS = "ALLOW_WITH_RISKS"
    WAIT_RECOVERY = "WAIT_RECOVERY"
    BLOCK = "BLOCK"


class WorkflowAcknowledgementPolicy(StrEnum):
    NONE = "NONE"
    BUNDLED_STAGE_TRANSITION = "BUNDLED_STAGE_TRANSITION"
    FINAL_CONFIRMATION = "FINAL_CONFIRMATION"


class WorkflowRunOutcomeV2(StrEnum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_RISKS = "COMPLETED_WITH_RISKS"
    PARTIAL_CONTINUABLE = "PARTIAL_CONTINUABLE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentActivityActor(StrEnum):
    AGENT = "AGENT"
    HOST = "HOST"
    TOOL = "TOOL"
    VALIDATOR = "VALIDATOR"
    USER = "USER"


class AgentActivityFamily(StrEnum):
    RUN = "RUN"
    PLAN = "PLAN"
    STAGE = "STAGE"
    ACTIVITY = "ACTIVITY"
    TOOL = "TOOL"
    EVIDENCE = "EVIDENCE"
    DECISION = "DECISION"
    ARTIFACT = "ARTIFACT"
    CONDITION = "CONDITION"
    RECOVERY = "RECOVERY"
    HUMAN = "HUMAN"


class AgentActivityVisibility(StrEnum):
    PRIMARY = "PRIMARY"
    DETAIL = "DETAIL"
    AUDIT = "AUDIT"


_ALL_CAPABILITIES = frozenset(WorkflowCapability)
_HARD_BLOCK_CAPABILITY_REQUIREMENTS = {
    "source_not_authorized": _ALL_CAPABILITIES,
    "source_identity_drift": _ALL_CAPABILITIES,
    "source_input_corrupt": _ALL_CAPABILITIES,
    "source_input_unavailable": _ALL_CAPABILITIES,
    "proposal_ungrounded": frozenset(
        {
            WorkflowCapability.APPLY_PROPOSAL,
            WorkflowCapability.PUBLISH_DRAFT,
            WorkflowCapability.LOCK_FINAL,
        }
    ),
    "irreversible_write_conflict": frozenset(
        {
            WorkflowCapability.APPLY_PROPOSAL,
            WorkflowCapability.PUBLISH_DRAFT,
            WorkflowCapability.LOCK_FINAL,
        }
    ),
}
_HARD_BLOCK_CODES = frozenset(_HARD_BLOCK_CAPABILITY_REQUIREMENTS)
_FORBIDDEN_PARAMETER_TOKENS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "authorization_header",
        "authorization_bearer",
        "auth_header",
        "auth_token",
        "bearer_token",
        "chain_of_thought",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "developer_prompt",
        "id_token",
        "inner_monologue",
        "oauth_token",
        "password",
        "private_key",
        "raw_reasoning",
        "refresh_token",
        "secret",
        "session_cookie",
        "system_prompt",
        "token",
        "x_api_key",
    }
)
_FORBIDDEN_PARAMETER_COMPACT_SUFFIXES = frozenset(
    {
        "accesstoken",
        "apikey",
        "apitoken",
        "authorizationbearer",
        "authorizationheader",
        "authheader",
        "authtoken",
        "bearertoken",
        "chainofthought",
        "clientsecret",
        "credential",
        "credentialvalue",
        "credentials",
        "developerprompt",
        "idtoken",
        "innermonologue",
        "oauthtoken",
        "password",
        "privatekey",
        "rawreasoning",
        "refreshtoken",
        "sessioncookie",
        "secret",
        "secretkey",
        "systemprompt",
        "tokenvalue",
        "xapikey",
    }
)
_MAX_AUTHORIZED_PARAMETER_BYTES = 16_384
_MAX_AUTHORIZED_PARAMETER_DEPTH = 8
_MAX_AUTHORIZED_PARAMETER_ITEMS = 256
_MAX_ACTIVITY_REFS = 256
_MAX_ACTIVITY_REF_LENGTH = 500
_MAX_ACTIVITY_EVENT_BYTES = 131_072


class WorkflowScopeV1(_StrictFrozenModel):
    kind: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    ref_id: str = Field(min_length=1, max_length=200)
    page_numbers: tuple[StrictInt, ...] = ()

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(page < 1 for page in value):
            raise ValueError("condition pages must be positive, unique, and sorted")
        return value


class WorkflowConditionDisplayV1(_StrictFrozenModel):
    title: str = Field(min_length=1, max_length=200)
    impact: str = Field(min_length=1, max_length=1000)
    agent_action: str = Field(min_length=1, max_length=1000)


class WorkflowRecoveryV1(_StrictFrozenModel):
    attempt: StrictInt = Field(ge=1)
    max_attempts: StrictInt = Field(ge=1, le=10)
    next_retry_at: datetime | None = None
    exhausted: bool = False

    @model_validator(mode="after")
    def validate_attempt(self) -> WorkflowRecoveryV1:
        if self.attempt > self.max_attempts:
            raise ValueError("recovery attempt cannot exceed max_attempts")
        if self.exhausted != (self.attempt == self.max_attempts):
            raise ValueError("recovery exhausted must match the last attempt")
        return self


class WorkflowConditionV1(_StrictFrozenModel):
    schema_version: Literal["reqpilotx-workflow-condition/v1"] = (
        "reqpilotx-workflow-condition/v1"
    )
    condition_id: str = Field(pattern=r"^workflow-condition-[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: AgentExtractionStage | None = None
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,159}$")
    disposition: WorkflowConditionDisposition
    state: WorkflowConditionState
    scope: WorkflowScopeV1
    blocked_capabilities: tuple[WorkflowCapability, ...] = ()
    acknowledgement_policy: WorkflowAcknowledgementPolicy = (
        WorkflowAcknowledgementPolicy.NONE
    )
    recovery: WorkflowRecoveryV1 | None = None
    display: WorkflowConditionDisplayV1
    evidence_refs: tuple[str, ...] = ()
    available_actions: tuple[str, ...] = ()
    raised_sequence: StrictInt = Field(ge=1)
    resolved_sequence: StrictInt | None = Field(default=None, ge=1)

    @field_validator(
        "blocked_capabilities",
        "evidence_refs",
        "available_actions",
    )
    @classmethod
    def validate_unique_values(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        if len(value) != len(set(value)):
            raise ValueError("condition collections must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_condition_policy(self) -> WorkflowConditionV1:
        active = self.state in {
            WorkflowConditionState.ACTIVE,
            WorkflowConditionState.RECOVERING,
            WorkflowConditionState.DEFERRED,
        }
        if self.state is WorkflowConditionState.RESOLVED:
            if self.resolved_sequence is None or self.resolved_sequence < self.raised_sequence:
                raise ValueError("resolved conditions require a valid resolved sequence")
        elif self.resolved_sequence is not None:
            raise ValueError("active conditions cannot claim a resolved sequence")
        if self.disposition is WorkflowConditionDisposition.HARD_BLOCKER:
            if self.code not in _HARD_BLOCK_CODES or not self.blocked_capabilities:
                raise ValueError("hard blockers require a Host-approved hard-block code")
            required = _HARD_BLOCK_CAPABILITY_REQUIREMENTS[self.code]
            if set(self.blocked_capabilities) != required:
                raise ValueError(
                    "hard blockers must match the exact Host capability matrix"
                )
            if self.acknowledgement_policy is not WorkflowAcknowledgementPolicy.NONE:
                raise ValueError("hard blockers cannot be acknowledged around")
            if self.recovery is not None:
                raise ValueError("hard blockers are not automatic recovery conditions")
        elif self.code in _HARD_BLOCK_CODES:
            raise ValueError("hard-block codes cannot be downgraded")
        if self.disposition is WorkflowConditionDisposition.CONTINUE_WITH_RISK:
            forbidden = {
                WorkflowCapability.READ_AUTHORIZED_SOURCE,
                WorkflowCapability.EXECUTE_SCOPE,
                WorkflowCapability.VALIDATE_STAGE,
            }
            if set(self.blocked_capabilities).intersection(forbidden):
                raise ValueError("continue-with-risk cannot block safe execution")
            if self.recovery is not None:
                raise ValueError("risk conditions do not own retry state")
        if self.disposition is WorkflowConditionDisposition.RECOVERABLE_FAILURE:
            if self.recovery is None or self.state is WorkflowConditionState.DEFERRED:
                raise ValueError("recoverable failures require active recovery state")
            if self.recovery.exhausted:
                raise ValueError(
                    "exhausted recovery must transition to risk, blocker, or terminal state"
                )
        elif self.recovery is not None:
            raise ValueError("only recoverable failures carry recovery state")
        if self.disposition is WorkflowConditionDisposition.INFO and (
            self.blocked_capabilities
            or self.acknowledgement_policy is not WorkflowAcknowledgementPolicy.NONE
        ):
            raise ValueError("informational conditions cannot gate capabilities")
        if not active and self.available_actions:
            raise ValueError("resolved conditions cannot expose pending actions")
        return self


class WorkflowCapabilityDecisionV1(_StrictFrozenModel):
    capability: WorkflowCapability
    decision: WorkflowCapabilityDecision
    condition_ids: tuple[str, ...] = ()


class WorkflowGateSnapshotV1(_StrictFrozenModel):
    schema_version: Literal["reqpilotx-workflow-gate-snapshot/v1"] = (
        "reqpilotx-workflow-gate-snapshot/v1"
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decisions: tuple[WorkflowCapabilityDecisionV1, ...]
    active_condition_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_capability_coverage(self) -> WorkflowGateSnapshotV1:
        if tuple(item.capability for item in self.decisions) != tuple(
            WorkflowCapability
        ):
            raise ValueError("gate snapshot must cover every capability in order")
        if len(self.active_condition_ids) != len(set(self.active_condition_ids)):
            raise ValueError("gate snapshot condition identities must be unique")
        return self


class AgentActivityScopeV2(_StrictFrozenModel):
    kind: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    ref_id: str = Field(min_length=1, max_length=200)
    page_numbers: tuple[StrictInt, ...] = ()

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(page < 1 for page in value):
            raise ValueError("activity pages must be positive, unique, and sorted")
        return value


class AgentActivityDisplayV2(_StrictFrozenModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)


class AgentActivityPayloadV2(_StrictFrozenModel):
    action_label: str = Field(default="", max_length=200)
    plan_step_id: str = Field(default="", max_length=160)
    tool_id: str = Field(default="", max_length=160)
    script_id: str = Field(default="", max_length=160)
    status: str = Field(default="", max_length=80)
    input_summary: str = Field(default="", max_length=2000)
    decision_summary: str = Field(default="", max_length=2000)
    uncertainty_summary: str = Field(default="", max_length=1000)
    duration_milliseconds: StrictInt = Field(default=0, ge=0)
    outcome_code: str = Field(default="", max_length=160)
    authorized_parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("authorized_parameters")
    @classmethod
    def reject_secrets_and_hidden_reasoning(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_public_parameters(value)
        return _copy_json_value(value)

    @model_serializer(mode="wrap")
    def serialize_public_payload(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> Any:
        # Frozen Pydantic models do not deep-freeze nested dictionaries. Recheck at
        # every serialization boundary so a caller cannot mutate a validated dict
        # and then persist credentials or hidden reasoning.
        _validate_public_parameters(self.authorized_parameters)
        return handler(self)


class AgentActivityEventV2(_StrictFrozenModel):
    schema_version: Literal["reqpilotx-agent-activity-event/v2"] = (
        "reqpilotx-agent-activity-event/v2"
    )
    event_id: str = Field(pattern=r"^agent-activity-[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: StrictInt = Field(ge=1)
    occurred_at_utc: datetime
    attempt: StrictInt = Field(default=1, ge=1)
    actor: AgentActivityActor
    family: AgentActivityFamily
    kind: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,119}$")
    stage: AgentExtractionStage | None = None
    scope: AgentActivityScopeV2 | None = None
    visibility: AgentActivityVisibility = AgentActivityVisibility.PRIMARY
    correlation_id: str = Field(default="", max_length=200)
    causation_id: str = Field(default="", max_length=200)
    display: AgentActivityDisplayV2
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    condition_refs: tuple[str, ...] = ()
    payload: AgentActivityPayloadV2 = Field(default_factory=AgentActivityPayloadV2)

    @field_validator("occurred_at_utc")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("activity event timestamps must be timezone-aware")
        return value

    @field_validator("evidence_refs", "artifact_refs", "condition_refs")
    @classmethod
    def validate_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("activity references must not contain duplicates")
        if len(value) > _MAX_ACTIVITY_REFS or any(
            not item or len(item) > _MAX_ACTIVITY_REF_LENGTH for item in value
        ):
            raise ValueError("activity references exceed the public event budget")
        return value


def derive_workflow_gate_snapshot(
    *,
    run_id: str,
    source_sha256: str,
    conditions: tuple[WorkflowConditionV1, ...],
) -> WorkflowGateSnapshotV1:
    condition_ids: set[str] = set()
    for condition in conditions:
        if condition.run_id != run_id or condition.source_sha256 != source_sha256:
            raise ValueError("gate conditions must match the run and source identity")
        if condition.condition_id in condition_ids:
            raise ValueError("gate conditions must have unique identities")
        condition_ids.add(condition.condition_id)
    active = tuple(
        condition
        for condition in conditions
        if condition.state is not WorkflowConditionState.RESOLVED
    )
    decisions: list[WorkflowCapabilityDecisionV1] = []
    for capability in WorkflowCapability:
        related = tuple(
            condition
            for condition in active
            if capability in condition.blocked_capabilities
        )
        hard = tuple(
            condition
            for condition in related
            if condition.disposition is WorkflowConditionDisposition.HARD_BLOCKER
        )
        recovering = tuple(
            condition
            for condition in related
            if condition.disposition
            is WorkflowConditionDisposition.RECOVERABLE_FAILURE
        )
        risks = tuple(
            condition
            for condition in active
            if condition.disposition
            is WorkflowConditionDisposition.CONTINUE_WITH_RISK
            and (
                capability in condition.blocked_capabilities
                or capability
                in {
                    WorkflowCapability.PUBLISH_DRAFT,
                    WorkflowCapability.LOCK_FINAL,
                }
            )
        )
        if hard:
            decision = WorkflowCapabilityDecision.BLOCK
            used = hard
        elif recovering:
            decision = WorkflowCapabilityDecision.WAIT_RECOVERY
            used = recovering
        elif risks:
            decision = WorkflowCapabilityDecision.ALLOW_WITH_RISKS
            used = risks
        else:
            decision = WorkflowCapabilityDecision.ALLOW
            used = ()
        decisions.append(
            WorkflowCapabilityDecisionV1(
                capability=capability,
                decision=decision,
                condition_ids=tuple(item.condition_id for item in used),
            )
        )
    return WorkflowGateSnapshotV1(
        run_id=run_id,
        source_sha256=source_sha256,
        decisions=tuple(decisions),
        active_condition_ids=tuple(item.condition_id for item in active),
    )


def append_activity_event(
    path: Path,
    event: AgentActivityEventV2,
    *,
    expected_parent: Path,
    expected_run_id: str,
    expected_source_sha256: str,
) -> None:
    path = path.expanduser()
    parent = path.parent.resolve(strict=True)
    trusted_parent = expected_parent.expanduser().resolve(strict=True)
    if parent != trusted_parent or path.is_symlink() or not path.is_file():
        raise ValueError("activity ledger must be an existing regular file")
    # Revalidate a deep model dump at the persistence boundary. This catches
    # mutation of nested public-parameter dictionaries after model construction.
    event = AgentActivityEventV2.model_validate(event.model_dump(mode="python"))
    if (
        event.run_id != expected_run_id
        or event.source_sha256 != expected_source_sha256
    ):
        raise ValueError("activity event must match the Host-pinned run and source")
    flags = os.O_RDWR | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    with acquire_file_lease(trusted_parent / ".activity.lock"):
        if os.name == "nt":
            descriptor = os.open(path, flags)
        else:
            directory_descriptor = os.open(trusted_parent, directory_flags)
            try:
                descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
            finally:
                os.close(directory_descriptor)
        try:
            if path.is_symlink() or not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("activity ledger must be an existing regular file")
            with os.fdopen(descriptor, "r+", encoding="utf-8", closefd=False) as stream:
                ledger_size = os.fstat(stream.fileno()).st_size
                if ledger_size:
                    os.lseek(stream.fileno(), ledger_size - 1, os.SEEK_SET)
                if ledger_size and os.read(stream.fileno(), 1) != b"\n":
                    raise ValueError("activity ledger has a torn tail")
                stream.seek(0)
                last_sequence = 0
                event_ids: set[str] = set()
                for line in stream:
                    if not line.strip():
                        continue
                    if len(line.encode("utf-8")) > _MAX_ACTIVITY_EVENT_BYTES:
                        raise ValueError("activity ledger event exceeds the byte budget")
                    current = AgentActivityEventV2.model_validate_json(line)
                    if current.run_id != expected_run_id:
                        raise ValueError("activity ledger cannot mix run identities")
                    if current.source_sha256 != expected_source_sha256:
                        raise ValueError("activity ledger cannot mix source identities")
                    if current.sequence != last_sequence + 1:
                        raise ValueError("activity ledger history must be contiguous")
                    if current.event_id in event_ids:
                        raise ValueError("activity ledger event identities must be unique")
                    event_ids.add(current.event_id)
                    last_sequence = current.sequence
                if event.sequence != last_sequence + 1:
                    raise ValueError("activity event sequence must be contiguous")
                if event.event_id in event_ids:
                    raise ValueError("activity ledger event identities must be unique")
                stream.seek(0, os.SEEK_END)
                event_payload = event.model_dump_json()
                if len(event_payload.encode("utf-8")) > _MAX_ACTIVITY_EVENT_BYTES:
                    raise ValueError("activity ledger event exceeds the byte budget")
                stream.write(event_payload + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)


def stable_activity_event_id(
    *,
    run_id: str,
    sequence: int,
    actor: AgentActivityActor,
    family: AgentActivityFamily,
    kind: str,
) -> str:
    payload = json_bytes(
        {
            "run_id": run_id,
            "sequence": sequence,
            "actor": actor.value,
            "family": family.value,
            "kind": kind,
        }
    )
    return "agent-activity-" + hashlib.sha256(payload).hexdigest()[:32]


def json_bytes(value: object) -> bytes:
    import json

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_public_parameters(value: object) -> None:
    _reject_forbidden_payload_keys(value)
    _validate_json_shape(value, depth=0, counter=[0])
    if len(json_bytes(value)) > _MAX_AUTHORIZED_PARAMETER_BYTES:
        raise ValueError("activity public parameters exceed the byte budget")


def _reject_forbidden_payload_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip(
                "_"
            )
            if normalized in _FORBIDDEN_PARAMETER_TOKENS:
                raise ValueError("activity payload contains a forbidden secret or CoT key")
            compact = normalized.replace("_", "")
            if any(
                compact.endswith(token)
                for token in _FORBIDDEN_PARAMETER_COMPACT_SUFFIXES
            ):
                raise ValueError("activity payload contains a forbidden secret or CoT key")
            _reject_forbidden_payload_keys(nested)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_payload_keys(item)


def _validate_json_shape(value: object, *, depth: int, counter: list[int]) -> None:
    if depth > _MAX_AUTHORIZED_PARAMETER_DEPTH:
        raise ValueError("activity public parameters exceed the depth budget")
    counter[0] += 1
    if counter[0] > _MAX_AUTHORIZED_PARAMETER_ITEMS:
        raise ValueError("activity public parameters exceed the item budget")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("activity public parameter numbers must be finite")
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("activity public parameter keys must be strings")
            _validate_json_shape(nested, depth=depth + 1, counter=counter)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _validate_json_shape(nested, depth=depth + 1, counter=counter)
        return
    raise ValueError("activity public parameters must be bounded JSON values")


def _copy_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _copy_json_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json_value(item) for item in value]
    return value


__all__ = [
    "AgentActivityActor",
    "AgentActivityDisplayV2",
    "AgentActivityEventV2",
    "AgentActivityFamily",
    "AgentActivityPayloadV2",
    "AgentActivityScopeV2",
    "AgentActivityVisibility",
    "WorkflowAcknowledgementPolicy",
    "WorkflowCapability",
    "WorkflowCapabilityDecision",
    "WorkflowConditionDisposition",
    "WorkflowConditionDisplayV1",
    "WorkflowConditionState",
    "WorkflowConditionV1",
    "WorkflowRecoveryV1",
    "WorkflowScopeV1",
    "WorkflowGateSnapshotV1",
    "WorkflowRunOutcomeV2",
    "append_activity_event",
    "derive_workflow_gate_snapshot",
    "stable_activity_event_id",
]
