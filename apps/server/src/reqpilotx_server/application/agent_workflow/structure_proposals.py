from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Callable

from reqpilotx_server.application.agent_workflow.contracts import (
    AgentRuntimeReceiptV1,
    AgentStructureApplyReceiptV1,
    AgentStructureProposalStatus,
    AgentStructureProposalV1,
    stable_agent_structure_proposal_record_id,
    structure_patch_proposal_sha256,
)
from reqpilotx_server.domain.extraction.structure_revision import StructurePatchProposalV1
from reqpilotx_server.ports.agent_structure_proposal_repository import (
    AgentStructureProposalConflict,
    AgentStructureProposalRepository,
)


ApplyStructureProposal = Callable[
    [StructurePatchProposalV1, str],
    AgentStructureApplyReceiptV1,
]


def create_ready_agent_structure_proposal(
    repository: AgentStructureProposalRepository,
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    facts_sha256: str,
    structure_patch_proposal: StructurePatchProposalV1,
    runtime_receipt: AgentRuntimeReceiptV1,
    runtime_thread_id: str,
    command_id: str,
    now: datetime | None = None,
    ttl_seconds: int = 15 * 60,
) -> AgentStructureProposalV1:
    observed_now = _utc(now or datetime.now(timezone.utc))
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 24 * 60 * 60:
        raise ValueError("proposal TTL must be between 60 seconds and 24 hours")
    latest = repository.get_latest(project_id=project_id, document_id=document_id)
    if (
        latest is not None
        and latest.source_sha256 == source_sha256
        and latest.structure_patch_proposal.proposal_id
        == structure_patch_proposal.proposal_id
    ):
        _retire_superseded_ready_proposal(
            repository,
            proposal=latest,
            command_id=command_id,
        )
        return latest
    revision = 1 if latest is None else latest.proposal_revision + 1
    supersedes = None if latest is None else latest.proposal_record_id
    record_id = stable_agent_structure_proposal_record_id(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        proposal_revision=revision,
        structure_patch_proposal_id=structure_patch_proposal.proposal_id,
    )
    proposal = AgentStructureProposalV1(
        proposal_record_id=record_id,
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        facts_sha256=facts_sha256,
        proposal_revision=revision,
        supersedes_proposal_record_id=supersedes,
        structure_patch_proposal=structure_patch_proposal,
        proposal_payload_sha256=structure_patch_proposal_sha256(
            structure_patch_proposal
        ),
        runtime_receipt=runtime_receipt,
        runtime_thread_id=runtime_thread_id,
        created_at_utc=observed_now,
        expires_at_utc=observed_now + timedelta(seconds=ttl_seconds),
    )
    persisted = repository.create(proposal, command_id=command_id)
    _retire_superseded_ready_proposal(
        repository,
        proposal=persisted,
        command_id=command_id,
    )
    return persisted


def _retire_superseded_ready_proposal(
    repository: AgentStructureProposalRepository,
    *,
    proposal: AgentStructureProposalV1,
    command_id: str,
) -> None:
    predecessor_id = proposal.supersedes_proposal_record_id
    if predecessor_id is None:
        return
    predecessor = repository.get(predecessor_id)
    if (
        predecessor is None
        or predecessor.status is not AgentStructureProposalStatus.READY
    ):
        return
    stale = AgentStructureProposalV1.model_validate(
        {
            **predecessor.model_dump(mode="json"),
            "status": AgentStructureProposalStatus.STALE.value,
            "status_reason_code": "superseded_by_revised_proposal",
        }
    )
    repository.transition(
        proposal_record_id=predecessor_id,
        expected_status=AgentStructureProposalStatus.READY,
        proposal=stale,
        command_id=(
            "supersede:"
            + hashlib.sha256(command_id.encode("utf-8")).hexdigest()
        ),
    )


def reject_agent_structure_proposal(
    repository: AgentStructureProposalRepository,
    *,
    proposal_record_id: str,
    command_id: str,
    reason_code: str = "user_rejected",
) -> AgentStructureProposalV1:
    current = _required(repository, proposal_record_id)
    if current.status is AgentStructureProposalStatus.REJECTED:
        return current
    if current.status is not AgentStructureProposalStatus.READY:
        raise AgentStructureProposalConflict("only READY proposals can be rejected")
    rejected = AgentStructureProposalV1.model_validate(
        {
            **current.model_dump(mode="json"),
            "status": AgentStructureProposalStatus.REJECTED.value,
            "status_reason_code": reason_code,
        }
    )
    return repository.transition(
        proposal_record_id=proposal_record_id,
        expected_status=AgentStructureProposalStatus.READY,
        proposal=rejected,
        command_id=command_id,
    )


def apply_agent_structure_proposal(
    repository: AgentStructureProposalRepository,
    *,
    proposal_record_id: str,
    command_id: str,
    apply_structure: ApplyStructureProposal,
    now: datetime | None = None,
) -> tuple[AgentStructureProposalV1, AgentStructureApplyReceiptV1]:
    observed_now = _utc(now or datetime.now(timezone.utc))
    current = _required(repository, proposal_record_id)
    if current.status is AgentStructureProposalStatus.APPLIED:
        if current.applied_command_id != command_id:
            raise AgentStructureProposalConflict(
                "proposal was already applied by another command"
            )
        return current, AgentStructureApplyReceiptV1(
            structure_revision_id=current.applied_structure_revision_id,
            structure_revision_number=current.applied_structure_revision_number,
            effective_structure_snapshot_id=current.applied_effective_snapshot_id,
            appended=False,
        )
    if current.status is not AgentStructureProposalStatus.READY:
        raise AgentStructureProposalConflict("proposal is not READY")
    if observed_now >= current.expires_at_utc:
        expired = AgentStructureProposalV1.model_validate(
            {
                **current.model_dump(mode="json"),
                "status": AgentStructureProposalStatus.EXPIRED.value,
                "status_reason_code": "proposal_expired",
            }
        )
        repository.transition(
            proposal_record_id=proposal_record_id,
            expected_status=AgentStructureProposalStatus.READY,
            proposal=expired,
            command_id=f"{command_id}:expire",
        )
        raise AgentStructureProposalConflict("proposal expired before Apply")
    apply_receipt = apply_structure(current.structure_patch_proposal, command_id)
    applied = AgentStructureProposalV1.model_validate(
        {
            **current.model_dump(mode="json"),
            "status": AgentStructureProposalStatus.APPLIED.value,
            "status_reason_code": "",
            "applied_structure_revision_id": apply_receipt.structure_revision_id,
            "applied_structure_revision_number": (
                apply_receipt.structure_revision_number
            ),
            "applied_effective_snapshot_id": (
                apply_receipt.effective_structure_snapshot_id
            ),
            "applied_command_id": command_id,
            "applied_at_utc": observed_now.isoformat(),
        }
    )
    try:
        persisted = repository.transition(
            proposal_record_id=proposal_record_id,
            expected_status=AgentStructureProposalStatus.READY,
            proposal=applied,
            command_id=command_id,
        )
    except AgentStructureProposalConflict:
        latest = _required(repository, proposal_record_id)
        if (
            latest.status is AgentStructureProposalStatus.APPLIED
            and latest.applied_command_id == command_id
            and latest.applied_structure_revision_id
            == apply_receipt.structure_revision_id
        ):
            persisted = latest
        else:
            raise
    return persisted, apply_receipt


def effective_agent_structure_proposal_status(
    proposal: AgentStructureProposalV1,
    *,
    now: datetime | None = None,
) -> AgentStructureProposalStatus:
    observed_now = _utc(now or datetime.now(timezone.utc))
    if (
        proposal.status is AgentStructureProposalStatus.READY
        and observed_now >= proposal.expires_at_utc
    ):
        return AgentStructureProposalStatus.EXPIRED
    return proposal.status


def _required(
    repository: AgentStructureProposalRepository,
    proposal_record_id: str,
) -> AgentStructureProposalV1:
    proposal = repository.get(proposal_record_id)
    if proposal is None:
        raise AgentStructureProposalConflict("proposal does not exist")
    return proposal


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "ApplyStructureProposal",
    "apply_agent_structure_proposal",
    "create_ready_agent_structure_proposal",
    "effective_agent_structure_proposal_status",
    "reject_agent_structure_proposal",
]
