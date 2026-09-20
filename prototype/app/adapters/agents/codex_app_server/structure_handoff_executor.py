from __future__ import annotations

import hashlib
from pathlib import Path

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalProductEvidenceRequest,
    prepare_canonical_app_server_document_session,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import canonical_sha256
from app.adapters.agents.codex_app_server.canonical_stage_transport import (
    LocalCodexAppServerStructuredStageTransport,
)
from app.adapters.agents.codex_app_server.canonical_structure_handoff_driver import (
    CanonicalStructureHandoffDriver,
)
from app.adapters.agents.codex_app_server.canonical_structure_handoff_tools import (
    CanonicalNativeStructureHandoffToolHost,
)
from app.adapters.agents.codex_app_server.lifecycle import _canary_runtime_state
from app.adapters.agents.codex_app_server.native_structure_revision_publisher import (
    NativeStructureRevisionPublisher,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.ports.agent_structure_handoff_executor import (
    AgentStructureHandoffExecutionV1,
    AgentStructureHandoffTaskV1,
)


CANONICAL_STRUCTURE_HANDOFF_ADAPTER_ID = "canonical-structure-handoff"
CANONICAL_STRUCTURE_HANDOFF_ADAPTER_VERSION = "0.1.0"


class LocalCanonicalStructureHandoffExecutor:
    def __init__(
        self,
        *,
        codex_bin: Path,
        codex_home: Path,
        repository_root: Path,
    ) -> None:
        self.codex_bin = codex_bin
        self.codex_home = codex_home
        self.repository_root = repository_root

    @property
    def adapter_id(self) -> str:
        return CANONICAL_STRUCTURE_HANDOFF_ADAPTER_ID

    @property
    def adapter_version(self) -> str:
        return CANONICAL_STRUCTURE_HANDOFF_ADAPTER_VERSION

    def execute(
        self,
        task: AgentStructureHandoffTaskV1,
        *,
        progress,  # type: ignore[no-untyped-def]
        cancellation_requested,  # type: ignore[no-untyped-def]
        begin_publication,  # type: ignore[no-untyped-def]
    ) -> AgentStructureHandoffExecutionV1:
        progress("APP_SERVER_SESSION_PREPARING")
        with _canary_runtime_state(
            codex_bin=self.codex_bin,
            codex_home=self.codex_home,
            repository_root=self.repository_root,
        ) as base_runtime:
            with prepare_canonical_app_server_document_session(
                base_runtime=base_runtime,
                canonical_kit_root=self.repository_root / "agent-kit",
                stages=(AgentExtractionStage.LOGICAL_STRUCTURE,),
                evidence_request=CanonicalProductEvidenceRequest(
                    source_path=task.source_path,
                    expected_source_sha256=task.binding.source_sha256,
                    document_role=task.document_role,
                    authorized_corpus_root=task.authorized_corpus_root,
                    run_id=task.binding.run_id,
                    opaque_source_handle=(
                        "source-handle-" + task.binding.binding_sha256[:32]
                    ),
                ),
                structure_handoff=task.binding,
                repository_root=self.repository_root,
            ) as session:
                publisher = NativeStructureRevisionPublisher(
                    project_id=task.project_id,
                    document_id=task.document_id,
                    handoff_id=task.binding.handoff_id,
                    before_publish=begin_publication,
                )
                host = CanonicalNativeStructureHandoffToolHost(
                    project_id=task.project_id,
                    document_id=task.document_id,
                    handoff_id=task.binding.handoff_id,
                    revision_publisher=publisher,
                )
                transport = LocalCodexAppServerStructuredStageTransport(
                    timeout_seconds=float(task.deadline_seconds),
                    cancellation_requested=cancellation_requested,
                    dynamic_tool_host=host,
                )
                driver = CanonicalStructureHandoffDriver(
                    transport=transport,
                    dynamic_tool_host=host,
                    revision_publisher=publisher,
                )
                progress("CODEX_TURN_RUNNING")
                result = driver.run(
                    session,
                    task_binding_sha256=canonical_sha256(
                        {
                            "binding_sha256": task.binding.binding_sha256,
                            "adapter_id": self.adapter_id,
                            "adapter_version": self.adapter_version,
                        }
                    ),
                )
                progress("HOST_PUBLICATION_RECORDED")
                return AgentStructureHandoffExecutionV1(
                    status=result.turn_result.status,
                    reason_code=result.turn_result.reason_code,
                    evidence_state=result.turn_result.evidence_state,
                    decision_policy=result.turn_result.decision_policy,
                    public_summary=result.turn_result.public_summary,
                    publication=result.publication,
                    runtime_thread_id_sha256=hashlib.sha256(
                        result.transport_result.runtime_thread_id.encode("utf-8")
                    ).hexdigest(),
                    runtime_turn_id_sha256=hashlib.sha256(
                        result.transport_result.runtime_turn_id.encode("utf-8")
                    ).hexdigest(),
                    tool_activity_summary=result.transport_result.tool_activity_summary,
                )


__all__ = [
    "CANONICAL_STRUCTURE_HANDOFF_ADAPTER_ID",
    "CANONICAL_STRUCTURE_HANDOFF_ADAPTER_VERSION",
    "LocalCanonicalStructureHandoffExecutor",
]
