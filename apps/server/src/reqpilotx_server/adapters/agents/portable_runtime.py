from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from reqpilotx_server.adapters.agents.portable_kit import PortableAgentKit
from reqpilotx_server.application.agent_workflow.extraction_runtime import (
    AgentExecutionRequestV1,
    AgentExecutionResultV1,
    AgentExecutionStatus,
    AgentKitIdentityV1,
    AgentRunEventV1,
    AgentRunnerIdentityV1,
    AgentRunnerKind,
    AgentStageArtifactV1,
)
from reqpilotx_server.ports.agent_runtime import (
    AgentRuntimeInvocationError,
    AgentRuntimeUnavailableError,
)


@dataclass(frozen=True, slots=True)
class PortableAgentDriverResult:
    runtime_version: str
    status: AgentExecutionStatus
    stage_artifacts: tuple[AgentStageArtifactV1, ...]
    diagnostic_codes: tuple[str, ...] = ()


class PortableAgentDriver(Protocol):
    def run(
        self,
        request: AgentExecutionRequestV1,
        *,
        emit_event: Callable[[AgentRunEventV1], None],
        cancellation_requested: Callable[[], bool],
    ) -> PortableAgentDriverResult: ...


class PortableAgentExecutor:
    """One domain-neutral executor used by both Codex runner adapters."""

    def __init__(
        self,
        kit: PortableAgentKit,
        *,
        runner_profile_id: str,
        driver: PortableAgentDriver,
    ) -> None:
        self._kit = kit
        self._profile = kit.runner_profile(runner_profile_id)
        if self._profile.runner_profile_id == "desktop-discovery":
            raise ValueError("discovery mode cannot be used as a conformance executor")
        self._driver = driver

    @property
    def kit_identity(self) -> AgentKitIdentityV1:
        return self._kit.identity

    @property
    def runner_profile_id(self) -> str:
        return self._profile.runner_profile_id

    @property
    def runner_kind(self) -> AgentRunnerKind:
        return self._profile.runner_kind

    def execute(
        self,
        request: AgentExecutionRequestV1,
        *,
        emit_event: Callable[[AgentRunEventV1], None],
        cancellation_requested: Callable[[], bool],
    ) -> AgentExecutionResultV1:
        if request.kit_identity != self.kit_identity:
            raise AgentRuntimeInvocationError("portable_agent_kit_identity_mismatch")
        if request.runner_profile_id != self.runner_profile_id:
            raise AgentRuntimeInvocationError("portable_agent_runner_profile_mismatch")
        if (
            request.model_id != self._profile.model_id
            or request.reasoning_effort != self._profile.reasoning_effort
        ):
            raise AgentRuntimeInvocationError("portable_agent_model_identity_mismatch")
        if request.document_role.startswith("CUSTOMER_") and not (
            request.customer_data_authorized
        ):
            raise AgentRuntimeInvocationError(
                "portable_agent_customer_data_not_authorized"
            )
        if cancellation_requested():
            raise AgentRuntimeInvocationError("portable_agent_cancelled_before_start")
        events: list[AgentRunEventV1] = []

        def collect(event: AgentRunEventV1) -> None:
            if event.run_id != request.run_id:
                raise AgentRuntimeInvocationError("portable_agent_event_identity_drift")
            if event.sequence != len(events) + 1:
                raise AgentRuntimeInvocationError("portable_agent_event_sequence_drift")
            events.append(event)
            emit_event(event)

        try:
            driver_result = self._driver.run(
                request,
                emit_event=collect,
                cancellation_requested=cancellation_requested,
            )
        except (AgentRuntimeInvocationError, AgentRuntimeUnavailableError):
            raise
        except Exception as exc:
            raise AgentRuntimeInvocationError("portable_agent_driver_failed") from exc
        if cancellation_requested():
            raise AgentRuntimeInvocationError("portable_agent_cancelled_after_run")
        runner_identity = AgentRunnerIdentityV1(
            runner_profile_id=self._profile.runner_profile_id,
            runner_profile_sha256=self._kit.runner_profile_sha256[
                self._profile.runner_profile_id
            ],
            runner_kind=self._profile.runner_kind,
            adapter_id=self._profile.adapter_id,
            adapter_version=self._profile.adapter_version,
            runtime_version=driver_result.runtime_version,
        )
        return AgentExecutionResultV1(
            run_id=request.run_id,
            source_sha256=request.source_sha256,
            kit_identity=self.kit_identity,
            runner_identity=runner_identity,
            workflow_mode=request.workflow_mode,
            requested_stages=request.stages,
            explicit_user_trigger=request.explicit_user_trigger,
            accepted_baseline_artifact_id=(
                request.accepted_baseline_artifact_id
            ),
            accepted_baseline_artifact_sha256=(
                request.accepted_baseline_artifact_sha256
            ),
            selected_scope_sha256=request.selected_scope_sha256,
            status=driver_result.status,
            events=tuple(events),
            stage_artifacts=driver_result.stage_artifacts,
            diagnostic_codes=driver_result.diagnostic_codes,
        )


class DesktopReferenceAgentExecutor(PortableAgentExecutor):
    def __init__(self, kit: PortableAgentKit, driver: PortableAgentDriver) -> None:
        super().__init__(kit, runner_profile_id="desktop-reference", driver=driver)
        if self._profile.runner_kind is not AgentRunnerKind.DESKTOP_REFERENCE:
            raise ValueError("desktop reference profile has the wrong runner kind")


class CodexAppServerProductAgentExecutor(PortableAgentExecutor):
    def __init__(self, kit: PortableAgentKit, driver: PortableAgentDriver) -> None:
        super().__init__(kit, runner_profile_id="app-server-product", driver=driver)
        if self._profile.runner_kind is not AgentRunnerKind.APP_SERVER_PRODUCT:
            raise ValueError("App Server product profile has the wrong runner kind")


__all__ = [
    "CodexAppServerProductAgentExecutor",
    "DesktopReferenceAgentExecutor",
    "PortableAgentDriver",
    "PortableAgentDriverResult",
    "PortableAgentExecutor",
]
