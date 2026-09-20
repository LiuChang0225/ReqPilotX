from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from app.application.agent_workflow.extraction_runtime import (
    AgentExecutionRequestV1,
    AgentExecutionResultV1,
    AgentKitIdentityV1,
    AgentRunEventV1,
    AgentRunnerKind,
)


@runtime_checkable
class AgentDocumentExecutorPort(Protocol):
    """Stage-neutral execution seam shared by desktop and embedded runners."""

    @property
    def kit_identity(self) -> AgentKitIdentityV1: ...

    @property
    def runner_profile_id(self) -> str: ...

    @property
    def runner_kind(self) -> AgentRunnerKind: ...

    def execute(
        self,
        request: AgentExecutionRequestV1,
        *,
        emit_event: Callable[[AgentRunEventV1], None],
        cancellation_requested: Callable[[], bool],
    ) -> AgentExecutionResultV1: ...


__all__ = ["AgentDocumentExecutorPort"]
