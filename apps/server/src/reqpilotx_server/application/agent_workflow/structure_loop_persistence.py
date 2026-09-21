from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from reqpilotx_server.application.agent_workflow.activity import (
    AgentActivityActor,
    AgentActivityDisplayV2,
    AgentActivityEventV2,
    AgentActivityFamily,
    AgentActivityPayloadV2,
    AgentActivityScopeV2,
    AgentActivityVisibility,
    stable_activity_event_id,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage


class AgentStructureLoopActivityDraftV1(BaseModel):
    """Content-safe activity draft; persistence assigns stream sequence and ID."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    actor: AgentActivityActor
    family: AgentActivityFamily
    kind: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,119}$")
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)
    scope_kind: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{0,79}$",
    )
    scope_ref_id: str = Field(default="", max_length=200)
    page_numbers: tuple[int, ...] = ()
    visibility: AgentActivityVisibility = AgentActivityVisibility.PRIMARY
    status: str = Field(default="", max_length=80)
    outcome_code: str = Field(default="", max_length=160)
    authorized_parameters: dict[str, object] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    correlation_id: str = Field(default="", max_length=200)
    causation_id: str = Field(default="", max_length=200)
    occurred_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("scope_ref_id")
    @classmethod
    def validate_scope_ref(cls, value: str, info) -> str:  # type: ignore[no-untyped-def]
        scope_kind = info.data.get("scope_kind")
        if bool(scope_kind) != bool(value):
            raise ValueError("activity scope kind and ref must be supplied together")
        return value


def stable_structure_loop_run_id(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    handoff_id: str,
) -> str:
    digest = _canonical_sha256(
        {
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": source_sha256,
            "artifact_id": artifact_id,
            "handoff_id": handoff_id,
        }
    )
    return f"agent-run-{digest[:32]}"


def materialize_structure_loop_activity(
    draft: AgentStructureLoopActivityDraftV1,
    *,
    run_id: str,
    source_sha256: str,
    sequence: int,
) -> AgentActivityEventV2:
    scope = (
        AgentActivityScopeV2(
            kind=str(draft.scope_kind),
            ref_id=draft.scope_ref_id,
            page_numbers=draft.page_numbers,
        )
        if draft.scope_kind is not None
        else None
    )
    event = AgentActivityEventV2(
        event_id=stable_activity_event_id(
            run_id=run_id,
            sequence=sequence,
            actor=draft.actor,
            family=draft.family,
            kind=draft.kind,
        ),
        run_id=run_id,
        source_sha256=source_sha256,
        sequence=sequence,
        occurred_at_utc=draft.occurred_at_utc,
        actor=draft.actor,
        family=draft.family,
        kind=draft.kind,
        stage=AgentExtractionStage.LOGICAL_STRUCTURE,
        scope=scope,
        visibility=draft.visibility,
        correlation_id=draft.correlation_id,
        causation_id=draft.causation_id,
        display=AgentActivityDisplayV2(
            title=draft.title,
            summary=draft.summary,
        ),
        evidence_refs=draft.evidence_refs,
        artifact_refs=draft.artifact_refs,
        payload=AgentActivityPayloadV2(
            status=draft.status,
            outcome_code=draft.outcome_code,
            authorized_parameters=draft.authorized_parameters,
        ),
    )
    return AgentActivityEventV2.model_validate(event.model_dump(mode="python"))


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "AgentStructureLoopActivityDraftV1",
    "materialize_structure_loop_activity",
    "stable_structure_loop_run_id",
]
