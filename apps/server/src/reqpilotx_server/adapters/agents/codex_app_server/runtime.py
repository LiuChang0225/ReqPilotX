from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from reqpilotx_server.application.agent_workflow.contracts import AgentRuntimeReceiptV1
from reqpilotx_server.domain.extraction.structure_revision import StructurePatchProposalV1
from reqpilotx_server.ports.agent_runtime import (
    AgentRuntimeInvocationError,
    AgentRuntimePort,
    AgentRuntimeSteeringPort,
    AgentStructureProposalResultV1,
    AgentStructureProposalTaskV1,
)


CODEX_APP_SERVER_ADAPTER_ID = "codex-app-server"
CODEX_APP_SERVER_ADAPTER_VERSION = "1.1.0"
CODEX_APP_SERVER_RUNTIME_VERSION = "codex-cli 0.153.4"


@dataclass(frozen=True, slots=True)
class CodexDriverResult:
    proposal: StructurePatchProposalV1 | None
    runtime_thread_id: str
    request_sha256: str
    response_sha256: str
    tool_receipt_sha256: str
    output_schema_sha256: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    no_proposal_reason_code: str = ""

    def __post_init__(self) -> None:
        if (self.proposal is None) != bool(self.no_proposal_reason_code):
            raise ValueError(
                "Codex driver result requires proposal or no-proposal reason"
            )


class CodexStructureProposalDriver(Protocol):
    def run(
        self,
        task: AgentStructureProposalTaskV1,
        *,
        progress: Callable[[str], None],
        cancellation_requested: Callable[[], bool],
        steering: AgentRuntimeSteeringPort | None = None,
    ) -> CodexDriverResult: ...


class CodexAppServerAgentRuntime(AgentRuntimePort):
    """Thin runtime adapter; the driver owns only App Server protocol details."""

    def __init__(
        self,
        driver: CodexStructureProposalDriver,
        *,
        runtime_version: str,
        model_id: str,
        skill_id: str = "reqpilotx-structure-tree-generation",
        skill_version: str = "1.1.0",
    ) -> None:
        if not runtime_version.strip() or not model_id.strip():
            raise ValueError("Codex runtime and model identity must be non-blank")
        self._driver = driver
        self._runtime_version = runtime_version.strip()
        self._model_id = model_id.strip()
        self._skill_id = skill_id.strip()
        self._skill_version = skill_version.strip()

    @property
    def adapter_id(self) -> str:
        return CODEX_APP_SERVER_ADAPTER_ID

    @property
    def adapter_version(self) -> str:
        return CODEX_APP_SERVER_ADAPTER_VERSION

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def skill_id(self) -> str:
        return self._skill_id

    @property
    def skill_version(self) -> str:
        return self._skill_version

    def propose_structure(
        self,
        task: AgentStructureProposalTaskV1,
        *,
        progress: Callable[[str], None],
        cancellation_requested: Callable[[], bool],
        steering: AgentRuntimeSteeringPort | None = None,
    ) -> AgentStructureProposalResultV1:
        if (
            task.model_id != self.model_id
            or task.skill_id != self.skill_id
            or task.skill_version != self.skill_version
            or task.reasoning_effort != "medium"
        ):
            raise AgentRuntimeInvocationError("agent_runtime_task_identity_mismatch")
        if cancellation_requested():
            raise AgentRuntimeInvocationError("agent_runtime_cancelled_before_start")
        result = self._driver.run(
            task,
            progress=progress,
            cancellation_requested=cancellation_requested,
            steering=steering,
        )
        if cancellation_requested():
            raise AgentRuntimeInvocationError("agent_runtime_cancelled_after_turn")
        receipt = AgentRuntimeReceiptV1(
            runtime_adapter_id=self.adapter_id,
            runtime_version=self._runtime_version,
            model_id=self.model_id,
            reasoning_effort="medium",
            skill_id=self.skill_id,
            skill_version=self.skill_version,
            output_schema_sha256=result.output_schema_sha256,
            request_sha256=result.request_sha256,
            response_sha256=result.response_sha256,
            tool_receipt_sha256=result.tool_receipt_sha256,
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
        )
        return AgentStructureProposalResultV1(
            proposal=result.proposal,
            runtime_receipt=receipt,
            runtime_thread_id=result.runtime_thread_id,
            no_proposal_reason_code=result.no_proposal_reason_code,
        )


__all__ = [
    "CODEX_APP_SERVER_ADAPTER_ID",
    "CODEX_APP_SERVER_ADAPTER_VERSION",
    "CODEX_APP_SERVER_RUNTIME_VERSION",
    "CodexAppServerAgentRuntime",
    "CodexDriverResult",
    "CodexStructureProposalDriver",
]
