from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, runtime_checkable

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalStructureHandoffBindingV1,
)


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
    "AgentStructureHandoffExecutionV1",
    "AgentStructureHandoffExecutorPort",
    "AgentStructureHandoffTaskV1",
]
