from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


CheckpointSink = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True, slots=True)
class SourceRequirementWindowTaskV1:
    project_id: str
    document_id: str
    source_path: Path
    document_map: Mapping[str, Any]
    scope_request: Mapping[str, Any]
    timeout_seconds: int
    max_concurrency: int
    semantic_context: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SourceRequirementWindowExecutionV1:
    candidate: Mapping[str, Any]
    run_receipt: Mapping[str, Any]


@runtime_checkable
class SourceRequirementWindowExecutorPort(Protocol):
    """Platform-neutral execution boundary for one SourceItem table scope."""

    adapter_id: str
    adapter_version: str
    transport_kind: str
    model_id: str
    reasoning_effort: str

    def readiness_reason(self) -> str | None: ...

    def execute(
        self,
        task: SourceRequirementWindowTaskV1,
        *,
        resume_checkpoint: Mapping[str, Any] | None,
        checkpoint_sink: CheckpointSink,
        cancellation_requested: Callable[[], bool],
    ) -> SourceRequirementWindowExecutionV1: ...


__all__ = [
    "CheckpointSink",
    "SourceRequirementWindowExecutionV1",
    "SourceRequirementWindowExecutorPort",
    "SourceRequirementWindowTaskV1",
]
