from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


CheckpointSink = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True, slots=True)
class SourceItemTextTaskV1:
    project_id: str
    document_id: str
    source_path: Path
    document_map: Mapping[str, object]
    scope_requests: tuple[Mapping[str, object], ...]
    timeout_seconds: int
    max_concurrency: int = 2
    execution_context: Mapping[str, object] | None = None
    semantic_context: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SourceItemTextExecutionV1:
    batch: Mapping[str, object]
    run_receipt: Mapping[str, object]


@runtime_checkable
class SourceItemTextExecutorPort(Protocol):
    """Platform-neutral App Server boundary for direct-text SourceItems."""

    adapter_id: str
    adapter_version: str
    transport_kind: str
    model_id: str
    reasoning_effort: str

    def readiness_reason(self) -> str | None: ...

    def execute(
        self,
        task: SourceItemTextTaskV1,
        *,
        cancellation_requested: Callable[[], bool],
        resume_checkpoint: Mapping[str, Any] | None = None,
        checkpoint_sink: CheckpointSink | None = None,
    ) -> SourceItemTextExecutionV1: ...


__all__ = [
    "CheckpointSink",
    "SourceItemTextExecutionV1",
    "SourceItemTextExecutorPort",
    "SourceItemTextTaskV1",
]
