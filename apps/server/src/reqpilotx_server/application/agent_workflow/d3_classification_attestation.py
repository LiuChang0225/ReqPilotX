from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reqpilotx_server.application.agent_workflow.extraction_runtime import (
    AGENT_BASELINE_STAGE_ORDER,
    AgentExtractionStage,
    AgentKitIdentityV1,
    AgentRunnerIdentityV1,
    AgentRunnerKind,
    AgentWorkflowMode,
)
from reqpilotx_server.domain.entry_classification import (
    EntryClassificationAgentSuggestionSetV1,
)


D3_CLASSIFICATION_POLICY_CLOSURE_SCHEMA_VERSION = "d3-classification-policy-closure/v1"
D3_CLASSIFICATION_EXECUTION_RECEIPT_SCHEMA_VERSION = (
    "d3-classification-execution-receipt/v1"
)
D3_CLASSIFICATION_POLICY_ATTESTATION_SCHEMA_VERSION = (
    "d3-classification-policy-attestation/v1"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D3PolicyFileDigestV1(_StrictFrozenModel):
    path: str = Field(
        pattern=(
            r"^(?:agents/[a-z0-9-]+\.json|policy/[a-z0-9.-]+\.json|"
            r"references/[a-z0-9-]+\.md|"
            r"\.agents/skills/[a-z0-9-]+/SKILL\.md)$"
        )
    )
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class D3ClassificationPolicyClosureV1(_StrictFrozenModel):
    """Exact Kit-owned files that define one D3 professional judgment context."""

    schema_version: Literal["d3-classification-policy-closure/v1"] = (
        D3_CLASSIFICATION_POLICY_CLOSURE_SCHEMA_VERSION
    )
    kit_identity: AgentKitIdentityV1
    agent_id: Literal["reqpilotx-requirement-baseline-curator"]
    agent_profile: D3PolicyFileDigestV1
    stage: Literal[AgentExtractionStage.PARENT_PROVISION_RECOGNITION] = (
        AgentExtractionStage.PARENT_PROVISION_RECOGNITION
    )
    stage_implementation_status: Literal["AVAILABLE", "PLANNED"]
    skill_id: Literal["reqpilotx-parent-provision-recognition"]
    skill: D3PolicyFileDigestV1
    policy_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    policy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    policy: D3PolicyFileDigestV1
    references: tuple[D3PolicyFileDigestV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_closure(self) -> D3ClassificationPolicyClosureV1:
        paths = tuple(item.path for item in self.references)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("D3 policy references must be unique and sorted")
        if "references/requirements-engineering-baseline.md" not in paths:
            raise ValueError("D3 policy closure requires the RE baseline reference")
        if (
            self.kit_identity.policy_id != self.policy_id
            or self.kit_identity.policy_version != self.policy_version
        ):
            raise ValueError("D3 policy closure drifted from the Kit identity")
        return self


class D3RunnerAuthorityV1(_StrictFrozenModel):
    runner_profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    runner_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_kind: AgentRunnerKind
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    adapter_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]


class D3ClassificationExecutionReceiptV1(_StrictFrozenModel):
    """Content-free receipt emitted by a runner that loaded the exact D3 closure."""

    schema_version: Literal["d3-classification-execution-receipt/v1"] = (
        D3_CLASSIFICATION_EXECUTION_RECEIPT_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    source_artifact_stage: Literal[AgentExtractionStage.LOGICAL_STRUCTURE] = (
        AgentExtractionStage.LOGICAL_STRUCTURE
    )
    source_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    working_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_structure_id: str = Field(min_length=1, max_length=200)
    classification_policy_version: str = Field(min_length=1, max_length=200)
    kit_identity: AgentKitIdentityV1
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,159}$")
    runner_identity: AgentRunnerIdentityV1
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    workflow_mode: Literal[AgentWorkflowMode.BASELINE] = AgentWorkflowMode.BASELINE
    requested_stages: tuple[AgentExtractionStage, ...]
    loaded_policy_closure: D3ClassificationPolicyClosureV1
    suggestion_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_provision_decision_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_execution_shape(self) -> D3ClassificationExecutionReceiptV1:
        if (
            len(self.requested_stages) < 4
            or self.requested_stages
            != AGENT_BASELINE_STAGE_ORDER[: len(self.requested_stages)]
            or AgentExtractionStage.PARENT_PROVISION_RECOGNITION
            not in self.requested_stages
        ):
            raise ValueError("D3 receipt requires a baseline workflow that reached D3")
        if self.evidence_refs != tuple(sorted(set(self.evidence_refs))) or any(
            not value.strip() for value in self.evidence_refs
        ):
            raise ValueError("D3 evidence refs must be non-empty, unique, and sorted")
        return self


class D3ClassificationPolicyAttestationV1(_StrictFrozenModel):
    schema_version: Literal["d3-classification-policy-attestation/v1"] = (
        D3_CLASSIFICATION_POLICY_ATTESTATION_SCHEMA_VERSION
    )
    attestation_id: str = Field(pattern=r"^d3-policy-attestation-[0-9a-f]{32}$")
    classification_policy_attested: bool
    diagnostic_codes: tuple[str, ...]
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,159}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    source_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_closure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    suggestion_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_provision_decision_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_decision(self) -> D3ClassificationPolicyAttestationV1:
        if self.diagnostic_codes != tuple(sorted(set(self.diagnostic_codes))):
            raise ValueError("D3 attestation diagnostics must be unique and sorted")
        if self.evidence_refs != tuple(sorted(set(self.evidence_refs))) or any(
            not value.strip() for value in self.evidence_refs
        ):
            raise ValueError(
                "D3 attestation evidence refs must be non-empty, unique, and sorted"
            )
        if self.classification_policy_attested == bool(self.diagnostic_codes):
            raise ValueError("D3 attestation state must match its diagnostics")
        decision_payload = {
            "diagnostic_codes": list(self.diagnostic_codes),
            "kit_bundle_sha256": self.kit_bundle_sha256,
            "agent_id": self.agent_id,
            "source_sha256": self.source_sha256,
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_sha256": self.source_artifact_sha256,
            "policy_closure_sha256": self.policy_closure_sha256,
            "execution_receipt_sha256": self.execution_receipt_sha256,
            "suggestion_set_sha256": self.suggestion_set_sha256,
            "parent_provision_decision_sha256": (self.parent_provision_decision_sha256),
            "evidence_refs": self.evidence_refs,
        }
        expected_id = f"d3-policy-attestation-{canonical_sha256(decision_payload)[:32]}"
        if self.attestation_id != expected_id:
            raise ValueError("D3 attestation identity does not match its decision")
        return self


def attest_d3_classification_policy(
    *,
    expected_policy_closure: D3ClassificationPolicyClosureV1,
    expected_runner: D3RunnerAuthorityV1 | None,
    execution_receipt: D3ClassificationExecutionReceiptV1,
    suggestions: EntryClassificationAgentSuggestionSetV1,
) -> D3ClassificationPolicyAttestationV1:
    """Fail closed unless runner, Kit, source, suggestions, and evidence all agree."""

    diagnostics: set[str] = set()
    if (
        execution_receipt.kit_identity != expected_policy_closure.kit_identity
        or execution_receipt.loaded_policy_closure != expected_policy_closure
        or suggestions.kit_bundle_sha256
        != expected_policy_closure.kit_identity.bundle_sha256
    ):
        diagnostics.add("KIT_POLICY_CLOSURE_MISMATCH")
    if (
        execution_receipt.agent_id != expected_policy_closure.agent_id
        or suggestions.agent_id != expected_policy_closure.agent_id
    ):
        diagnostics.add("AGENT_IDENTITY_MISMATCH")
    actual_runner = execution_receipt.runner_identity
    if expected_runner is None:
        diagnostics.add("RUNNER_PROFILE_NOT_IN_KIT")
    elif (
        actual_runner.runner_profile_id != expected_runner.runner_profile_id
        or actual_runner.runner_profile_sha256 != expected_runner.runner_profile_sha256
        or actual_runner.runner_kind is not expected_runner.runner_kind
        or actual_runner.adapter_id != expected_runner.adapter_id
        or actual_runner.adapter_version != expected_runner.adapter_version
        or execution_receipt.model_id != expected_runner.model_id
        or execution_receipt.reasoning_effort != expected_runner.reasoning_effort
    ):
        diagnostics.add("RUNNER_IDENTITY_MISMATCH")
    if suggestions.source_sha256 != execution_receipt.source_sha256:
        diagnostics.add("SOURCE_IDENTITY_MISMATCH")
    if (
        suggestions.working_tree_sha256 != execution_receipt.working_tree_sha256
        or suggestions.effective_structure_id
        != execution_receipt.effective_structure_id
    ):
        diagnostics.add("STRUCTURE_IDENTITY_MISMATCH")
    if suggestions.policy_version != execution_receipt.classification_policy_version:
        diagnostics.add("CLASSIFICATION_POLICY_VERSION_MISMATCH")
    suggestion_sha256 = canonical_sha256(suggestions.model_dump(mode="json"))
    if execution_receipt.suggestion_set_sha256 != suggestion_sha256:
        diagnostics.add("SUGGESTION_SET_DIGEST_MISMATCH")
    expected_evidence_refs = tuple(
        sorted(
            {
                evidence_ref
                for entry in suggestions.entries
                for evidence_ref in entry.evidence_refs
            }
        )
    )
    if execution_receipt.evidence_refs != expected_evidence_refs:
        diagnostics.add("EVIDENCE_REFERENCE_CLOSURE_MISMATCH")

    policy_closure_sha256 = canonical_sha256(
        expected_policy_closure.model_dump(mode="json")
    )
    execution_receipt_sha256 = canonical_sha256(
        execution_receipt.model_dump(mode="json")
    )
    decision_payload = {
        "diagnostic_codes": sorted(diagnostics),
        "kit_bundle_sha256": expected_policy_closure.kit_identity.bundle_sha256,
        "agent_id": expected_policy_closure.agent_id,
        "source_sha256": execution_receipt.source_sha256,
        "source_artifact_id": execution_receipt.source_artifact_id,
        "source_artifact_sha256": execution_receipt.source_artifact_sha256,
        "policy_closure_sha256": policy_closure_sha256,
        "execution_receipt_sha256": execution_receipt_sha256,
        "suggestion_set_sha256": suggestion_sha256,
        "parent_provision_decision_sha256": (
            execution_receipt.parent_provision_decision_sha256
        ),
        "evidence_refs": expected_evidence_refs,
    }
    attestation_digest = canonical_sha256(decision_payload)
    return D3ClassificationPolicyAttestationV1(
        attestation_id=f"d3-policy-attestation-{attestation_digest[:32]}",
        classification_policy_attested=not diagnostics,
        diagnostic_codes=tuple(sorted(diagnostics)),
        kit_bundle_sha256=expected_policy_closure.kit_identity.bundle_sha256,
        agent_id=expected_policy_closure.agent_id,
        source_sha256=execution_receipt.source_sha256,
        source_artifact_id=execution_receipt.source_artifact_id,
        source_artifact_sha256=execution_receipt.source_artifact_sha256,
        policy_closure_sha256=policy_closure_sha256,
        execution_receipt_sha256=execution_receipt_sha256,
        suggestion_set_sha256=suggestion_sha256,
        parent_provision_decision_sha256=(
            execution_receipt.parent_provision_decision_sha256
        ),
        evidence_refs=expected_evidence_refs,
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
    "D3_CLASSIFICATION_EXECUTION_RECEIPT_SCHEMA_VERSION",
    "D3_CLASSIFICATION_POLICY_ATTESTATION_SCHEMA_VERSION",
    "D3_CLASSIFICATION_POLICY_CLOSURE_SCHEMA_VERSION",
    "D3ClassificationExecutionReceiptV1",
    "D3ClassificationPolicyAttestationV1",
    "D3ClassificationPolicyClosureV1",
    "D3PolicyFileDigestV1",
    "D3RunnerAuthorityV1",
    "attest_d3_classification_policy",
    "canonical_sha256",
]
