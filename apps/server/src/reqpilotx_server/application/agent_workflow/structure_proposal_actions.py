from __future__ import annotations

from typing import Any

from reqpilotx_server.application.agent_workflow.contracts import (
    AgentStructureProposalApplyRequest,
    AgentStructureProposalRejectRequest,
)
from reqpilotx_server.application.agent_workflow.structure_proposal_jobs import (
    serialize_agent_structure_proposal,
)
from reqpilotx_server.application.agent_workflow.structure_proposals import (
    apply_agent_structure_proposal,
    reject_agent_structure_proposal,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.ports.agent_structure_proposal_repository import (
    AgentStructureProposalConflict,
)


def get_agent_structure_proposal(
    project_id: str,
    document_id: str,
    proposal_record_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.runtime import (
        _ASSET_RESPONSE_HEADERS,
        _agent_structure_proposals,
    )

    _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    proposal = _required_record(
        _agent_structure_proposals(),
        project_id=project_id,
        document_id=document_id,
        proposal_record_id=proposal_record_id,
    )
    return serialize_agent_structure_proposal(proposal)


def reject_agent_structure_proposal_action(
    project_id: str,
    document_id: str,
    payload: AgentStructureProposalRejectRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.runtime import (
        _ASSET_RESPONSE_HEADERS,
        _agent_structure_proposals,
    )

    _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    repository = _agent_structure_proposals()
    current = _required_record(
        repository,
        project_id=project_id,
        document_id=document_id,
        proposal_record_id=payload.proposal_record_id,
        expected_payload_sha256=payload.expected_proposal_payload_sha256,
    )
    try:
        rejected = reject_agent_structure_proposal(
            repository,
            proposal_record_id=current.proposal_record_id,
            command_id=payload.command_id,
            reason_code=payload.reason_code,
        )
    except AgentStructureProposalConflict as exc:
        raise _proposal_conflict(str(exc)) from exc
    return serialize_agent_structure_proposal(rejected)


def apply_agent_structure_proposal_action(
    project_id: str,
    document_id: str,
    payload: AgentStructureProposalApplyRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.extraction.structure_workspace import (
        apply_stored_agent_structure_patch,
    )
    from reqpilotx_server.application.runtime import (
        _ASSET_RESPONSE_HEADERS,
        _agent_structure_proposals,
    )

    _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    repository = _agent_structure_proposals()
    current = _required_record(
        repository,
        project_id=project_id,
        document_id=document_id,
        proposal_record_id=payload.proposal_record_id,
        expected_payload_sha256=payload.expected_proposal_payload_sha256,
    )
    try:
        applied, apply_receipt = apply_agent_structure_proposal(
            repository,
            proposal_record_id=current.proposal_record_id,
            command_id=payload.command_id,
            apply_structure=lambda proposal, command_id: (
                apply_stored_agent_structure_patch(
                    project_id,
                    document_id,
                    proposal,
                    command_id=command_id,
                )
            ),
        )
    except AgentStructureProposalConflict as exc:
        raise _proposal_conflict(str(exc)) from exc
    return {
        "schema_version": "agent-structure-proposal-apply-result/v1",
        "proposal": serialize_agent_structure_proposal(applied),
        "apply_receipt": apply_receipt.model_dump(mode="json"),
    }


def _required_record(
    repository: Any,
    *,
    project_id: str,
    document_id: str,
    proposal_record_id: str,
    expected_payload_sha256: str = "",
):
    proposal = repository.get(proposal_record_id)
    if proposal is None:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "agent_structure_proposal_not_found",
                "message": "Agent Structure proposal was not found.",
            },
        )
    if proposal.project_id != project_id or proposal.document_id != document_id:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "agent_structure_proposal_not_found",
                "message": "Agent Structure proposal was not found.",
            },
        )
    if (
        expected_payload_sha256
        and proposal.proposal_payload_sha256 != expected_payload_sha256
    ):
        raise _proposal_conflict("proposal payload changed")
    return proposal


def _proposal_conflict(message: str) -> ApplicationError:
    return ApplicationError(
        status_code=409,
        detail={
            "code": "agent_structure_proposal_conflict",
            "message": message or "Agent Structure proposal changed.",
        },
    )


__all__ = [
    "apply_agent_structure_proposal_action",
    "get_agent_structure_proposal",
    "reject_agent_structure_proposal_action",
]
