from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Callable, Literal, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CanonicalStructureHandoffBindingV1(BaseModel):
    """Host-neutral, content-free binding preserved across Agent adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)
    schema_version: Literal["canonical-structure-handoff-binding/v1"] = (
        "canonical-structure-handoff-binding/v1"
    )
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    handoff_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ACKNOWLEDGED"] = "ACKNOWLEDGED"
    affected_scope_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    work_item_count: int = Field(ge=1, le=256)
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_binding(self) -> CanonicalStructureHandoffBindingV1:
        if self.affected_scope_ids != tuple(sorted(set(self.affected_scope_ids))):
            raise ValueError("handoff scopes must be unique and sorted")
        payload = self.model_dump(mode="json", exclude={"binding_sha256"})
        expected = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if self.binding_sha256 != expected:
            raise ValueError("handoff binding digest drifted")
        return self


@dataclass(frozen=True, slots=True)
class AgentStructureHandoffTaskV1:
    project_id: str
    document_id: str
    source_path: Path
    authorized_corpus_root: Path
    document_role: str
    binding: CanonicalStructureHandoffBindingV1
    deadline_seconds: int = 300


@dataclass(frozen=True, slots=True)
class AgentStructureHandoffExecutionV1:
    status: str
    reason_code: str
    evidence_state: str
    decision_policy: str
    public_summary: str
    publication: Mapping[str, object] | None
    runtime_thread_id_sha256: str
    runtime_turn_id_sha256: str
    tool_activity_summary: Mapping[str, object]


@runtime_checkable
class AgentStructureHandoffExecutorPort(Protocol):
    @property
    def adapter_id(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    def execute(
        self,
        task: AgentStructureHandoffTaskV1,
        *,
        progress: Callable[[str], None],
        cancellation_requested: Callable[[], bool],
        begin_publication: Callable[[], object],
    ) -> AgentStructureHandoffExecutionV1: ...


__all__ = [
    "CanonicalStructureHandoffBindingV1",
    "AgentStructureHandoffExecutionV1",
    "AgentStructureHandoffExecutorPort",
    "AgentStructureHandoffTaskV1",
]
