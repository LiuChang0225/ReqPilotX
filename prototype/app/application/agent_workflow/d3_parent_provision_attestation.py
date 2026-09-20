from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.application.agent_workflow.extraction_runtime import (
    AGENT_BASELINE_STAGE_ORDER,
    AgentExtractionStage,
    AgentKitIdentityV1,
    AgentRunnerIdentityV1,
    AgentRunnerKind,
    AgentWorkflowMode,
)


D3_PARENT_PROVISION_EXECUTION_RECEIPT_SCHEMA_VERSION = (
    "d3-parent-provision-execution-receipt/v1"
)
D3_PARENT_PROVISION_POLICY_ATTESTATION_SCHEMA_VERSION = (
    "d3-parent-provision-policy-attestation/v1"
)
D3_PARENT_PROVISION_DECISION_CANDIDATE_SCHEMA_VERSION = (
    "d3-parent-provision-decision-candidate/v1"
)
_D3_STAGE_PREFIX = AGENT_BASELINE_STAGE_ORDER[:4]
_MACHINE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D3ParentProvisionPolicyFileDigestV1(_StrictFrozenModel):
    path: str = Field(
        pattern=(
            r"^(?:agents/[a-z0-9-]+\.json|policy/[a-z0-9.-]+\.json|"
            r"references/[a-z0-9-]+\.md|"
            r"\.agents/skills/[a-z0-9-]+/SKILL\.md)$"
        )
    )
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class D3ParentProvisionPolicyClosureV1(_StrictFrozenModel):
    """Exact Kit-owned professional context for D3 parent recognition."""

    schema_version: Literal["d3-parent-provision-policy-closure/v1"] = (
        "d3-parent-provision-policy-closure/v1"
    )
    kit_identity: AgentKitIdentityV1
    agent_id: Literal["reqpilotx-requirement-baseline-curator"]
    agent_profile: D3ParentProvisionPolicyFileDigestV1
    stage: Literal[AgentExtractionStage.PARENT_PROVISION_RECOGNITION] = (
        AgentExtractionStage.PARENT_PROVISION_RECOGNITION
    )
    stage_implementation_status: Literal["AVAILABLE", "PLANNED"]
    skill_id: Literal["reqpilotx-parent-provision-recognition"]
    skill: D3ParentProvisionPolicyFileDigestV1
    policy_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    policy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    policy: D3ParentProvisionPolicyFileDigestV1
    references: tuple[D3ParentProvisionPolicyFileDigestV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_closure(self) -> D3ParentProvisionPolicyClosureV1:
        paths = tuple(item.path for item in self.references)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("D3 parent policy references must be unique and sorted")
        if "references/requirements-engineering-baseline.md" not in paths:
            raise ValueError("D3 parent policy requires the RE baseline reference")
        if (
            self.kit_identity.policy_id != self.policy_id
            or self.kit_identity.policy_version != self.policy_version
        ):
            raise ValueError("D3 parent policy closure drifted from the Kit identity")
        return self


class D3ParentProvisionRunnerAuthorityV1(_StrictFrozenModel):
    runner_profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    runner_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_kind: AgentRunnerKind
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    adapter_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]


class D3ParentProvisionExecutionReceiptV1(_StrictFrozenModel):
    """Content-free Host-observed proof for one direct D3 decision candidate."""

    schema_version: Literal["d3-parent-provision-execution-receipt/v1"] = (
        D3_PARENT_PROVISION_EXECUTION_RECEIPT_SCHEMA_VERSION
    )
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d2_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    d2_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    agent_id: Literal["reqpilotx-requirement-baseline-curator"] = (
        "reqpilotx-requirement-baseline-curator"
    )
    runner_identity: AgentRunnerIdentityV1
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    workflow_mode: Literal[AgentWorkflowMode.BASELINE] = AgentWorkflowMode.BASELINE
    requested_stages: tuple[AgentExtractionStage, ...]
    loaded_policy_closure: D3ParentProvisionPolicyClosureV1
    stage_session_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_provision_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=65536)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("D3 evidence refs must be non-empty, unique, and sorted")
        return value

    @model_validator(mode="after")
    def validate_execution_shape(self) -> D3ParentProvisionExecutionReceiptV1:
        if self.requested_stages != _D3_STAGE_PREFIX:
            raise ValueError("D3 receipt requires the exact baseline prefix through D3")
        if (
            self.kit_identity != self.loaded_policy_closure.kit_identity
            or self.agent_id != self.loaded_policy_closure.agent_id
        ):
            raise ValueError("D3 execution receipt drifted from its policy closure")
        return self


class D3ParentProvisionPolicyAttestationV1(_StrictFrozenModel):
    schema_version: Literal["d3-parent-provision-policy-attestation/v1"] = (
        D3_PARENT_PROVISION_POLICY_ATTESTATION_SCHEMA_VERSION
    )
    attestation_id: str = Field(pattern=r"^d3-policy-attestation-[0-9a-f]{32}$")
    policy_attested: bool
    diagnostic_codes: tuple[str, ...]
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_id: Literal["reqpilotx-requirement-baseline-curator"] = (
        "reqpilotx-requirement-baseline-curator"
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d2_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    d2_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_closure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_session_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_provision_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=65536)

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _MACHINE_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("D3 attestation diagnostics must be sorted machine codes")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("D3 attestation evidence refs must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_attestation(self) -> D3ParentProvisionPolicyAttestationV1:
        if self.policy_attested == bool(self.diagnostic_codes):
            raise ValueError("D3 attestation state must match its diagnostics")
        payload = self.model_dump(mode="json", exclude={"attestation_id"})
        expected_id = f"d3-policy-attestation-{canonical_sha256(payload)[:32]}"
        if self.attestation_id != expected_id:
            raise ValueError("D3 attestation identity does not match its decision")
        return self


def attest_d3_parent_provision_policy(
    *,
    expected_policy_closure: D3ParentProvisionPolicyClosureV1,
    expected_runner: D3ParentProvisionRunnerAuthorityV1,
    expected_run_id: str,
    expected_source_sha256: str,
    expected_d2_artifact_id: str,
    expected_d2_artifact_sha256: str,
    expected_stage_session_attestation_sha256: str,
    expected_execution_trace_sha256: str,
    expected_decision_candidate_sha256: str,
    expected_parent_provision_decision_sha256: str,
    expected_evidence_refs: tuple[str, ...],
    execution_receipt: D3ParentProvisionExecutionReceiptV1,
) -> D3ParentProvisionPolicyAttestationV1:
    """Attest the actual D3 turn against Host-pinned session and candidate facts."""

    if expected_evidence_refs != tuple(sorted(set(expected_evidence_refs))) or any(
        not item.strip() for item in expected_evidence_refs
    ):
        raise ValueError("expected D3 evidence refs must be unique and sorted")

    diagnostics: set[str] = set()
    if expected_policy_closure.stage_implementation_status != "AVAILABLE":
        diagnostics.add("STAGE_IMPLEMENTATION_NOT_AVAILABLE")
    if (
        execution_receipt.kit_identity != expected_policy_closure.kit_identity
        or execution_receipt.loaded_policy_closure != expected_policy_closure
    ):
        diagnostics.add("KIT_POLICY_CLOSURE_MISMATCH")
    if execution_receipt.agent_id != expected_policy_closure.agent_id:
        diagnostics.add("AGENT_IDENTITY_MISMATCH")
    actual_runner = execution_receipt.runner_identity
    if (
        actual_runner.runner_profile_id != expected_runner.runner_profile_id
        or actual_runner.runner_profile_sha256 != expected_runner.runner_profile_sha256
        or actual_runner.runner_kind is not expected_runner.runner_kind
        or actual_runner.adapter_id != expected_runner.adapter_id
        or actual_runner.adapter_version != expected_runner.adapter_version
        or execution_receipt.model_id != expected_runner.model_id
        or execution_receipt.reasoning_effort != expected_runner.reasoning_effort
    ):
        diagnostics.add("RUNNER_IDENTITY_MISMATCH")
    if (
        execution_receipt.run_id != expected_run_id
        or execution_receipt.source_sha256 != expected_source_sha256
        or execution_receipt.d2_artifact_id != expected_d2_artifact_id
        or execution_receipt.d2_artifact_sha256 != expected_d2_artifact_sha256
    ):
        diagnostics.add("D2_SOURCE_LINEAGE_MISMATCH")
    if (
        execution_receipt.stage_session_attestation_sha256
        != expected_stage_session_attestation_sha256
    ):
        diagnostics.add("STAGE_SESSION_ATTESTATION_MISMATCH")
    if execution_receipt.execution_trace_sha256 != expected_execution_trace_sha256:
        diagnostics.add("EXECUTION_TRACE_MISMATCH")
    if (
        execution_receipt.decision_candidate_sha256
        != expected_decision_candidate_sha256
    ):
        diagnostics.add("DECISION_CANDIDATE_MISMATCH")
    if (
        execution_receipt.parent_provision_decision_sha256
        != expected_parent_provision_decision_sha256
    ):
        diagnostics.add("PARENT_PROVISION_DECISION_MISMATCH")
    if execution_receipt.evidence_refs != expected_evidence_refs:
        diagnostics.add("EVIDENCE_REFERENCE_CLOSURE_MISMATCH")

    policy_closure_sha256 = canonical_sha256(
        expected_policy_closure.model_dump(mode="json")
    )
    execution_receipt_sha256 = canonical_sha256(
        execution_receipt.model_dump(mode="json")
    )
    payload = {
        "schema_version": D3_PARENT_PROVISION_POLICY_ATTESTATION_SCHEMA_VERSION,
        "policy_attested": not diagnostics,
        "diagnostic_codes": tuple(sorted(diagnostics)),
        "kit_bundle_sha256": expected_policy_closure.kit_identity.bundle_sha256,
        "agent_id": expected_policy_closure.agent_id,
        "run_id": expected_run_id,
        "source_sha256": expected_source_sha256,
        "d2_artifact_id": expected_d2_artifact_id,
        "d2_artifact_sha256": expected_d2_artifact_sha256,
        "policy_closure_sha256": policy_closure_sha256,
        "execution_receipt_sha256": execution_receipt_sha256,
        "stage_session_attestation_sha256": (expected_stage_session_attestation_sha256),
        "execution_trace_sha256": expected_execution_trace_sha256,
        "decision_candidate_sha256": expected_decision_candidate_sha256,
        "parent_provision_decision_sha256": (expected_parent_provision_decision_sha256),
        "evidence_refs": expected_evidence_refs,
    }
    attestation_id = f"d3-policy-attestation-{canonical_sha256(payload)[:32]}"
    return D3ParentProvisionPolicyAttestationV1(
        attestation_id=attestation_id,
        **payload,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "D3_PARENT_PROVISION_DECISION_CANDIDATE_SCHEMA_VERSION",
    "D3_PARENT_PROVISION_EXECUTION_RECEIPT_SCHEMA_VERSION",
    "D3_PARENT_PROVISION_POLICY_ATTESTATION_SCHEMA_VERSION",
    "D3ParentProvisionExecutionReceiptV1",
    "D3ParentProvisionPolicyAttestationV1",
    "D3ParentProvisionPolicyClosureV1",
    "D3ParentProvisionPolicyFileDigestV1",
    "D3ParentProvisionRunnerAuthorityV1",
    "attest_d3_parent_provision_policy",
    "canonical_sha256",
]
