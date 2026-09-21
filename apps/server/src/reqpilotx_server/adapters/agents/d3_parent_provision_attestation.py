from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from reqpilotx_server.adapters.agents.portable_kit import (
    AgentKitValidationError,
    PortableAgentKit,
)
from reqpilotx_server.application.agent_workflow.d3_parent_provision_attestation import (
    D3ParentProvisionPolicyClosureV1,
    D3ParentProvisionPolicyFileDigestV1,
    D3ParentProvisionRunnerAuthorityV1,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import (
    AGENT_BASELINE_STAGE_ORDER,
    AgentExtractionStage,
)


_D3_AGENT_ID = "reqpilotx-requirement-baseline-curator"
_D3_SKILL_ID = "reqpilotx-parent-provision-recognition"


class _BaselineAgentProfileV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal["reqpilotx-agent-profile/v1"]
    agent_id: Literal["reqpilotx-requirement-baseline-curator"]
    role: Literal["PRIMARY_BASELINE_CURATOR"]
    stage_order: tuple[AgentExtractionStage, ...]
    authority: Literal["PROPOSAL_ONLY"]
    preserves_source_verbatim: Literal[True]
    may_atomize: Literal[False]
    may_write_business_state: Literal[False]
    completion_source: Literal["BASELINE_VERIFICATION_RECEIPT"]

    @model_validator(mode="after")
    def validate_baseline_order(self) -> _BaselineAgentProfileV1:
        if self.stage_order != AGENT_BASELINE_STAGE_ORDER:
            raise ValueError("baseline curator stage order drifted")
        return self


def compile_d3_parent_provision_policy_closure(
    kit: PortableAgentKit,
) -> D3ParentProvisionPolicyClosureV1:
    """Compile the direct, source/D2-bound D3 policy closure from one exact Kit."""

    stage_spec = next(
        (
            item
            for item in kit.manifest.stages
            if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
        ),
        None,
    )
    if stage_spec is None or stage_spec.skill_id != _D3_SKILL_ID:
        raise AgentKitValidationError("Agent Kit D3 Skill identity drifted")
    skill_spec = next(
        (item for item in kit.manifest.skills if item.skill_id == _D3_SKILL_ID),
        None,
    )
    if skill_spec is None:
        raise AgentKitValidationError("Agent Kit D3 Skill is missing")

    agent_matches: list[tuple[str, _BaselineAgentProfileV1]] = []
    for relative in kit.manifest.agents:
        payload = _read_locked_file(kit, relative)
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AgentKitValidationError("Agent Kit profile is invalid JSON") from exc
        if isinstance(raw, dict) and raw.get("agent_id") == _D3_AGENT_ID:
            try:
                agent_matches.append(
                    (relative, _BaselineAgentProfileV1.model_validate(raw))
                )
            except Exception as exc:
                raise AgentKitValidationError(
                    "Agent Kit baseline curator profile is invalid"
                ) from exc
    if len(agent_matches) != 1:
        raise AgentKitValidationError(
            "Agent Kit requires one exact baseline curator profile"
        )
    agent_path, _agent_profile = agent_matches[0]

    return D3ParentProvisionPolicyClosureV1(
        kit_identity=kit.identity,
        agent_id=_D3_AGENT_ID,
        agent_profile=_digest(kit, agent_path),
        stage_implementation_status=stage_spec.implementation_status,
        skill_id=_D3_SKILL_ID,
        skill=_digest(kit, skill_spec.path),
        policy_id=kit.manifest.policy_id,
        policy_version=kit.manifest.policy_version,
        policy=_digest(kit, kit.manifest.policy_path),
        references=tuple(
            _digest(kit, relative) for relative in stage_spec.reference_paths
        ),
    )


def compile_d3_parent_provision_runner_authority(
    kit: PortableAgentKit,
    runner_profile_id: str,
) -> D3ParentProvisionRunnerAuthorityV1:
    profile = kit.runner_profile(runner_profile_id)
    return D3ParentProvisionRunnerAuthorityV1(
        runner_profile_id=profile.runner_profile_id,
        runner_profile_sha256=kit.runner_profile_sha256[runner_profile_id],
        runner_kind=profile.runner_kind,
        adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version,
        model_id=profile.model_id,
        reasoning_effort=profile.reasoning_effort,
    )


def _digest(
    kit: PortableAgentKit,
    relative: str,
) -> D3ParentProvisionPolicyFileDigestV1:
    payload = _read_locked_file(kit, relative)
    return D3ParentProvisionPolicyFileDigestV1(
        path=relative,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _read_locked_file(kit: PortableAgentKit, relative: str) -> bytes:
    locked = {item.path: item.sha256 for item in kit.lock.files}
    expected_sha256 = locked.get(relative)
    if expected_sha256 is None:
        raise AgentKitValidationError("D3 policy file is not locked by the Agent Kit")
    path = kit.root / relative
    if path.is_symlink() or not path.is_file():
        raise AgentKitValidationError("D3 policy file must be a regular file")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise AgentKitValidationError("D3 policy file drifted from the Agent Kit lock")
    return payload


__all__ = [
    "compile_d3_parent_provision_policy_closure",
    "compile_d3_parent_provision_runner_authority",
]
