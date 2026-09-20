from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.application.agent_workflow.activity import AgentActivityEventV2
from app.application.agent_workflow.structure_loop_persistence import (
    AgentStructureLoopActivityDraftV1,
)
from app.application.agent_workflow.structure_tool_loop import (
    NativeStructureHandoffAttemptReceiptV1,
    NativeStructureHandoffAttemptRequestV1,
)
from app.application.agent_workflow.native_structure_working_revision import (
    NativeStructureWorkingRevisionHeadV1,
    NativeStructureWorkingRevisionObjectV1,
    NativeStructureWorkingRevisionV1,
)


class AgentStructureLoopConflict(RuntimeError):
    """Raised when an attempt/activity command replays with changed content."""


@runtime_checkable
class AgentStructureLoopRepository(Protocol):
    def append_working_revision(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        base_artifact_id: str,
        base_projection_sha256: str,
        revision: NativeStructureWorkingRevisionV1,
        object_key: str,
        object_sha256: str,
        object_size: int,
        media_type: str,
        expected_head_revision_sha256: str | None,
    ) -> tuple[NativeStructureWorkingRevisionHeadV1, bool]: ...

    def get_working_revision_head(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        base_artifact_id: str | None = None,
        base_projection_sha256: str | None = None,
    ) -> NativeStructureWorkingRevisionHeadV1 | None: ...

    def get_working_revision_object(
        self,
        *,
        project_id: str,
        document_id: str,
        run_id: str,
        revision: int,
    ) -> NativeStructureWorkingRevisionObjectV1 | None: ...

    def append_attempt(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        artifact_id: str,
        request: NativeStructureHandoffAttemptRequestV1,
        receipt: NativeStructureHandoffAttemptReceiptV1,
        expected_global_head_attempt_id: str | None = None,
        expected_global_projection_attempts_sha256: str | None = None,
    ) -> tuple[NativeStructureHandoffAttemptReceiptV1, bool]: ...

    def list_attempts(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        artifact_id: str,
        handoff_id: str | None = None,
    ) -> tuple[NativeStructureHandoffAttemptReceiptV1, ...]: ...

    def get_attempt_request(
        self,
        *,
        project_id: str,
        document_id: str,
        attempt_id: str,
    ) -> NativeStructureHandoffAttemptRequestV1 | None: ...

    def append_activity(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        artifact_id: str,
        handoff_id: str,
        command_id: str,
        draft: AgentStructureLoopActivityDraftV1,
    ) -> tuple[AgentActivityEventV2, bool]: ...

    def list_activity(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        artifact_id: str,
        handoff_id: str | None = None,
        after_sequence: int = 0,
    ) -> tuple[AgentActivityEventV2, ...]: ...


__all__ = [
    "AgentStructureLoopConflict",
    "AgentStructureLoopRepository",
]
