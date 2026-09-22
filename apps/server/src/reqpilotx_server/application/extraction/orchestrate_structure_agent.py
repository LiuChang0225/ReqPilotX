from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reqpilotx_server.application.extraction.audit_structure import (
    StructureAuditFinding,
    StructureAuditResult,
    StructureAuditStatus,
    audit_structure_with_provider,
)
from reqpilotx_server.application.extraction.provider_source_policy import (
    ProviderSourceIRRejected,
)
from reqpilotx_server.application.extraction.revise_structure import (
    apply_structure_patch,
    create_structure_patch_proposal,
    undo_structure_revision,
    validate_structure_patch_proposal,
    validate_structure_revision_chain,
)
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot
from reqpilotx_server.domain.extraction.structure import StructureRelationKind, StructureTreeSnapshot
from reqpilotx_server.domain.extraction.structure_revision import (
    AddStructureRelationOperationV1,
    BoundarySplitStructureOperationV1,
    ContinuationMergeStructureOperationV1,
    EffectiveStructureSnapshotV1,
    LogicalReorderStructureOperationV1,
    RemoveStructureRelationOperationV1,
    ReparentStructureOperationV1,
    StructureApplyResultV1,
    StructureImpactV1,
    StructurePatchOperationV1,
    StructurePatchProposalV1,
    StructurePatchV1,
    StructureRevisionRejected,
    StructureRevisionRejectionCode,
    StructureRevisionV1,
)
from reqpilotx_server.ports.structure_audit_provider import (
    StructureAuditCategory,
    StructureAuditProposalEvidence,
    StructureAuditProviderPort,
)


STRUCTURE_REPAIR_RESULT_SCHEMA_VERSION = "structure-repair-result/v1"
STRUCTURE_DERIVATION_SCOPE_SCHEMA_VERSION = "structure-derivation-scope/v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class DocumentRequirementAgentStage(str, Enum):
    SOURCE = "SOURCE"
    BASE_STRUCTURE = "BASE_STRUCTURE"
    STRUCTURE_REPAIR = "STRUCTURE_REPAIR"
    REQUIREMENT_CLASSIFICATION = "REQUIREMENT_CLASSIFICATION"
    OPTIONAL_ATOMIZATION = "OPTIONAL_ATOMIZATION"
    OPTIONAL_REFINEMENT = "OPTIONAL_REFINEMENT"
    VALIDATION = "VALIDATION"
    READY = "READY"


DOCUMENT_REQUIREMENT_AGENT_STAGE_ORDER = tuple(DocumentRequirementAgentStage)


class DocumentRequirementAgentStageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"


class DocumentRequirementAgentStageState(_StrictFrozenModel):
    stage: DocumentRequirementAgentStage
    status: DocumentRequirementAgentStageStatus
    stale_structure_node_ids: tuple[str, ...] = ()


class StructureRepairStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNRESOLVED = "UNRESOLVED"


class StructureProposalDisposition(str, Enum):
    NO_PROPOSAL = "NO_PROPOSAL"
    LOW_EVIDENCE = "LOW_EVIDENCE"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"


class StructureRepairProposalOutcome(_StrictFrozenModel):
    finding_id: str = Field(min_length=1, max_length=160)
    provider_run_id: str = Field(min_length=1, max_length=160)
    disposition: StructureProposalDisposition
    proposal: StructurePatchProposalV1 | None = None
    patch: StructurePatchV1 | None = None
    revision: StructureRevisionV1 | None = None
    rejection_code: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_outcome(self) -> StructureRepairProposalOutcome:
        if self.disposition is StructureProposalDisposition.NO_PROPOSAL:
            if any(
                value is not None
                for value in (
                    self.proposal,
                    self.patch,
                    self.revision,
                    self.rejection_code,
                )
            ):
                raise ValueError("a no-proposal outcome cannot claim patch state")
        elif self.disposition is StructureProposalDisposition.LOW_EVIDENCE:
            if (
                self.proposal is None
                or self.patch is not None
                or self.revision is not None
                or self.rejection_code != "low_evidence"
            ):
                raise ValueError("a low-evidence proposal must remain unapplied")
        elif self.disposition is StructureProposalDisposition.REJECTED:
            if (
                self.proposal is None
                or self.patch is not None
                or self.revision is not None
                or self.rejection_code is None
            ):
                raise ValueError("a rejected proposal must retain an exact reason")
        elif (
            self.proposal is None
            or self.patch is None
            or self.revision is None
            or self.rejection_code is not None
            or self.patch.accepted_proposal_id != self.proposal.proposal_id
            or self.revision.patch != self.patch
        ):
            raise ValueError(
                "an applied proposal must retain proposal, patch, and revision"
            )
        return self


class StructureRepairResult(_StrictFrozenModel):
    schema_version: Literal["structure-repair-result/v1"] = (
        STRUCTURE_REPAIR_RESULT_SCHEMA_VERSION
    )
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_structure_snapshot_id: str = Field(min_length=1, max_length=160)
    starting_revision_number: int = Field(ge=0)
    status: StructureRepairStatus
    audit: StructureAuditResult | None = None
    audit_error_code: str | None = Field(default=None, max_length=120)
    proposal_outcomes: tuple[StructureRepairProposalOutcome, ...] = ()
    revisions: tuple[StructureRevisionV1, ...] = ()
    effective_structure: EffectiveStructureSnapshotV1
    new_impact: StructureImpactV1

    @model_validator(mode="after")
    def validate_result(self) -> StructureRepairResult:
        if (
            self.effective_structure.source_ir_id != self.source_ir_id
            or self.effective_structure.source_sha256 != self.source_sha256
            or self.effective_structure.base_structure_snapshot_id
            != self.base_structure_snapshot_id
            or self.effective_structure.revision_number != len(self.revisions)
            or self.starting_revision_number > len(self.revisions)
        ):
            raise ValueError("Structure repair result identities are inconsistent")
        if (self.audit is None) != (self.audit_error_code is not None):
            raise ValueError("Structure repair audit failure must be explicit")
        if self.audit is not None:
            expected_status = {
                StructureAuditStatus.COMPLETE: StructureRepairStatus.COMPLETE,
                StructureAuditStatus.PARTIAL: StructureRepairStatus.PARTIAL,
                StructureAuditStatus.UNRESOLVED: StructureRepairStatus.UNRESOLVED,
            }[self.audit.status]
            if self.status is not expected_status:
                raise ValueError(
                    "Structure repair status must retain audit completeness"
                )
        elif self.status is not StructureRepairStatus.UNRESOLVED:
            raise ValueError("a failed audit leaves Structure repair unresolved")
        return self


class StructureManualMutationResult(_StrictFrozenModel):
    proposal: StructurePatchProposalV1 | None = None
    apply_result: StructureApplyResultV1
    revisions: tuple[StructureRevisionV1, ...]

    @model_validator(mode="after")
    def validate_result(self) -> StructureManualMutationResult:
        if self.revisions[-1] != self.apply_result.revision:
            raise ValueError("manual mutation must append the returned revision")
        if self.proposal is None:
            if self.apply_result.revision.patch is not None:
                raise ValueError("undo cannot contain a patch proposal")
        elif (
            self.apply_result.revision.patch is None
            or self.apply_result.revision.patch.accepted_proposal_id
            != self.proposal.proposal_id
        ):
            raise ValueError("manual apply result must bind its proposal")
        return self


class StructureDerivationScopeV1(_StrictFrozenModel):
    schema_version: Literal["structure-derivation-scope/v1"] = (
        STRUCTURE_DERIVATION_SCOPE_SCHEMA_VERSION
    )
    effective_snapshot_id: str = Field(min_length=1, max_length=160)
    stale_structure_node_ids: tuple[str, ...] = ()
    reusable_structure_node_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_partition(self) -> StructureDerivationScopeV1:
        if (
            self.stale_structure_node_ids
            != tuple(sorted(set(self.stale_structure_node_ids)))
            or self.reusable_structure_node_ids
            != tuple(sorted(set(self.reusable_structure_node_ids)))
            or set(self.stale_structure_node_ids).intersection(
                self.reusable_structure_node_ids
            )
        ):
            raise ValueError("derivation scope must be a stable disjoint partition")
        return self


def run_structure_repair_with_provider(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    provider: StructureAuditProviderPort,
    *,
    revisions: tuple[StructureRevisionV1, ...] = (),
    max_concurrency: int = 4,
) -> StructureRepairResult:
    """Audit one effective tree and apply only high-evidence, Core-valid proposals."""

    current = validate_structure_revision_chain(source_ir, base_tree, revisions)
    starting_revision_number = current.revision_number
    try:
        audit = audit_structure_with_provider(
            source_ir,
            current,
            provider,
            max_concurrency=max_concurrency,
        )
    except ProviderSourceIRRejected as exc:
        return StructureRepairResult(
            source_ir_id=source_ir.source_ir_id,
            source_sha256=source_ir.source_sha256,
            base_structure_snapshot_id=base_tree.structure_snapshot_id,
            starting_revision_number=starting_revision_number,
            status=StructureRepairStatus.UNRESOLVED,
            audit=None,
            audit_error_code=exc.code.value,
            revisions=revisions,
            effective_structure=current,
            new_impact=StructureImpactV1(),
        )
    except Exception:
        return StructureRepairResult(
            source_ir_id=source_ir.source_ir_id,
            source_sha256=source_ir.source_sha256,
            base_structure_snapshot_id=base_tree.structure_snapshot_id,
            starting_revision_number=starting_revision_number,
            status=StructureRepairStatus.UNRESOLVED,
            audit=None,
            audit_error_code="structure_audit_unresolved",
            revisions=revisions,
            effective_structure=current,
            new_impact=StructureImpactV1(),
        )

    revision_list = list(revisions)
    outcomes: list[StructureRepairProposalOutcome] = []
    impacts: list[StructureImpactV1] = []
    for finding in audit.findings:
        outcome, appended_revision, impact = _apply_audit_finding_proposal(
            source_ir,
            base_tree,
            tuple(revision_list),
            finding,
        )
        outcomes.append(outcome)
        if appended_revision is not None:
            revision_list.append(appended_revision)
        if impact is not None:
            impacts.append(impact)
    effective = validate_structure_revision_chain(
        source_ir,
        base_tree,
        tuple(revision_list),
    )
    status = {
        StructureAuditStatus.COMPLETE: StructureRepairStatus.COMPLETE,
        StructureAuditStatus.PARTIAL: StructureRepairStatus.PARTIAL,
        StructureAuditStatus.UNRESOLVED: StructureRepairStatus.UNRESOLVED,
    }[audit.status]
    return StructureRepairResult(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        base_structure_snapshot_id=base_tree.structure_snapshot_id,
        starting_revision_number=starting_revision_number,
        status=status,
        audit=audit,
        audit_error_code=None,
        proposal_outcomes=tuple(outcomes),
        revisions=tuple(revision_list),
        effective_structure=effective,
        new_impact=_merge_impacts(impacts),
    )


def initialize_structure_repair_without_provider(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    *,
    error_code: str,
    revisions: tuple[StructureRevisionV1, ...] = (),
) -> StructureRepairResult:
    """Expose the current safe effective tree when audit transport is unavailable."""

    if not error_code:
        raise ValueError("an unavailable Structure audit requires an error code")
    current = validate_structure_revision_chain(source_ir, base_tree, revisions)
    return StructureRepairResult(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        base_structure_snapshot_id=base_tree.structure_snapshot_id,
        starting_revision_number=current.revision_number,
        status=StructureRepairStatus.UNRESOLVED,
        audit=None,
        audit_error_code=error_code,
        revisions=revisions,
        effective_structure=current,
        new_impact=StructureImpactV1(),
    )


def apply_manual_structure_operations(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
    operations: tuple[StructurePatchOperationV1, ...],
    *,
    expected_revision_number: int,
    expected_effective_snapshot_id: str,
) -> StructureManualMutationResult:
    """Use the same proposal validator and apply service as AI repair."""

    current = validate_structure_revision_chain(source_ir, base_tree, revisions)
    _require_optimistic_identity(
        current,
        expected_revision_number=expected_revision_number,
        expected_effective_snapshot_id=expected_effective_snapshot_id,
    )
    proposal = create_structure_patch_proposal(
        source_ir,
        base_tree,
        revisions,
        operations,
    )
    patch = validate_structure_patch_proposal(
        proposal,
        source_ir,
        base_tree,
        revisions,
    )
    applied = apply_structure_patch(patch, source_ir, base_tree, revisions)
    next_revisions = (
        revisions if not applied.appended else (*revisions, applied.revision)
    )
    return StructureManualMutationResult(
        proposal=proposal,
        apply_result=applied,
        revisions=next_revisions,
    )


def undo_current_structure_revision(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
    *,
    expected_revision_number: int,
    expected_effective_snapshot_id: str,
) -> StructureManualMutationResult:
    """Append Core's inverse revision without deleting history."""

    applied = undo_structure_revision(
        source_ir,
        base_tree,
        revisions,
        expected_revision_number=expected_revision_number,
        expected_effective_snapshot_id=expected_effective_snapshot_id,
    )
    return StructureManualMutationResult(
        proposal=None,
        apply_result=applied,
        revisions=(*revisions, applied.revision),
    )


def derive_structure_derivation_scope(
    *,
    effective_snapshot_id: str,
    requirement_structure_node_ids: tuple[str, ...],
    impact: StructureImpactV1,
) -> StructureDerivationScopeV1:
    """Invalidate only derivations whose Structure owner is in Core impact."""

    unique_ids = set(requirement_structure_node_ids)
    affected = set(impact.affected_structure_node_ids)
    return StructureDerivationScopeV1(
        effective_snapshot_id=effective_snapshot_id,
        stale_structure_node_ids=tuple(sorted(unique_ids.intersection(affected))),
        reusable_structure_node_ids=tuple(sorted(unique_ids - affected)),
    )


def build_agent_stage_states(
    *,
    active_stage: DocumentRequirementAgentStage,
    active_status: DocumentRequirementAgentStageStatus,
    stale_structure_node_ids: tuple[str, ...] = (),
) -> tuple[DocumentRequirementAgentStageState, ...]:
    """Build the deterministic, non-ReAct stage timeline."""

    active_index = DOCUMENT_REQUIREMENT_AGENT_STAGE_ORDER.index(active_stage)
    states: list[DocumentRequirementAgentStageState] = []
    for index, stage in enumerate(DOCUMENT_REQUIREMENT_AGENT_STAGE_ORDER):
        if index < active_index:
            status = DocumentRequirementAgentStageStatus.COMPLETE
        elif index == active_index:
            status = active_status
        else:
            status = DocumentRequirementAgentStageStatus.PENDING
        states.append(
            DocumentRequirementAgentStageState(
                stage=stage,
                status=status,
                stale_structure_node_ids=(
                    tuple(sorted(set(stale_structure_node_ids)))
                    if stage is DocumentRequirementAgentStage.REQUIREMENT_CLASSIFICATION
                    else ()
                ),
            )
        )
    return tuple(states)


def _apply_audit_finding_proposal(
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
    finding: StructureAuditFinding,
) -> tuple[
    StructureRepairProposalOutcome,
    StructureRevisionV1 | None,
    StructureImpactV1 | None,
]:
    if not finding.proposed_operations:
        return (
            StructureRepairProposalOutcome(
                finding_id=finding.finding_id,
                provider_run_id=finding.provider_run_id,
                disposition=StructureProposalDisposition.NO_PROPOSAL,
            ),
            None,
            None,
        )
    proposal = create_structure_patch_proposal(
        source_ir,
        base_tree,
        revisions,
        finding.proposed_operations,
    )
    if finding.proposal_evidence is StructureAuditProposalEvidence.LOW:
        return (
            StructureRepairProposalOutcome(
                finding_id=finding.finding_id,
                provider_run_id=finding.provider_run_id,
                disposition=StructureProposalDisposition.LOW_EVIDENCE,
                proposal=proposal,
                rejection_code="low_evidence",
            ),
            None,
            None,
        )
    scope_rejection = _audit_proposal_scope_rejection(finding)
    if scope_rejection is not None:
        return (
            StructureRepairProposalOutcome(
                finding_id=finding.finding_id,
                provider_run_id=finding.provider_run_id,
                disposition=StructureProposalDisposition.REJECTED,
                proposal=proposal,
                rejection_code=scope_rejection,
            ),
            None,
            None,
        )
    try:
        patch = validate_structure_patch_proposal(
            proposal,
            source_ir,
            base_tree,
            revisions,
        )
        applied = apply_structure_patch(patch, source_ir, base_tree, revisions)
    except StructureRevisionRejected as exc:
        return (
            StructureRepairProposalOutcome(
                finding_id=finding.finding_id,
                provider_run_id=finding.provider_run_id,
                disposition=StructureProposalDisposition.REJECTED,
                proposal=proposal,
                rejection_code=exc.code.value,
            ),
            None,
            None,
        )
    return (
        StructureRepairProposalOutcome(
            finding_id=finding.finding_id,
            provider_run_id=finding.provider_run_id,
            disposition=StructureProposalDisposition.APPLIED,
            proposal=proposal,
            patch=patch,
            revision=applied.revision,
        ),
        applied.revision if applied.appended else None,
        applied.impact if applied.appended else None,
    )


def _audit_proposal_scope_rejection(
    finding: StructureAuditFinding,
) -> str | None:
    cited_node_ids = {
        finding.target_structure_node_id,
        *finding.related_structure_node_ids,
    }
    evidence_refs = set(finding.evidence_source_refs)
    operation = finding.proposed_operations[0]
    if isinstance(operation, ReparentStructureOperationV1):
        valid = (
            finding.category is StructureAuditCategory.POSSIBLE_MISPLACED_PARENT
            and operation.structure_node_id == finding.target_structure_node_id
            and operation.new_parent_structure_node_id
            in finding.related_structure_node_ids
        )
    elif isinstance(operation, LogicalReorderStructureOperationV1):
        valid = (
            finding.category is StructureAuditCategory.POSSIBLE_SOURCE_ORDER_ERROR
            and operation.parent_structure_node_id in cited_node_ids
            and finding.target_structure_node_id
            in operation.ordered_child_structure_node_ids
            and set(operation.ordered_child_structure_node_ids).issubset(cited_node_ids)
        )
    elif isinstance(operation, BoundarySplitStructureOperationV1):
        valid = (
            finding.category is StructureAuditCategory.POSSIBLE_MISSED_BOUNDARY
            and operation.structure_node_id == finding.target_structure_node_id
            and all(
                span.source_ref in evidence_refs
                for part in operation.parts
                for span in part.source_spans
            )
        )
    elif isinstance(operation, ContinuationMergeStructureOperationV1):
        endpoints = {
            operation.primary_structure_node_id,
            operation.continuation_structure_node_id,
        }
        valid = (
            finding.category is StructureAuditCategory.POSSIBLE_DUPLICATE_CONTENT
            and finding.target_structure_node_id in endpoints
            and endpoints.issubset(cited_node_ids)
        )
    elif isinstance(operation, AddStructureRelationOperationV1):
        endpoints = {
            operation.source_structure_node_id,
            operation.target_structure_node_id,
        }
        valid = (
            finding.category is StructureAuditCategory.POSSIBLE_TABLE_CONTINUATION_ERROR
            and operation.kind is StructureRelationKind.CONTINUES
            and finding.target_structure_node_id in endpoints
            and endpoints.issubset(cited_node_ids)
            and set(operation.source_refs).issubset(evidence_refs)
        )
    elif isinstance(operation, RemoveStructureRelationOperationV1):
        valid = False
    else:  # pragma: no cover - Core's discriminated union is exhaustive.
        valid = False
    return None if valid else "proposal_outside_audit_scope"


def _merge_impacts(impacts: list[StructureImpactV1]) -> StructureImpactV1:
    return StructureImpactV1(
        direct_structure_node_ids=tuple(
            sorted(
                {
                    node_id
                    for impact in impacts
                    for node_id in impact.direct_structure_node_ids
                }
            )
        ),
        affected_structure_node_ids=tuple(
            sorted(
                {
                    node_id
                    for impact in impacts
                    for node_id in impact.affected_structure_node_ids
                }
            )
        ),
        affected_section_node_ids=tuple(
            sorted(
                {
                    node_id
                    for impact in impacts
                    for node_id in impact.affected_section_node_ids
                }
            )
        ),
        changed_relation_ids=tuple(
            sorted(
                {
                    relation_id
                    for impact in impacts
                    for relation_id in impact.changed_relation_ids
                }
            )
        ),
    )


def _require_optimistic_identity(
    current: EffectiveStructureSnapshotV1,
    *,
    expected_revision_number: int,
    expected_effective_snapshot_id: str,
) -> None:
    if (
        expected_revision_number != current.revision_number
        or expected_effective_snapshot_id != current.effective_snapshot_id
    ):
        raise StructureRevisionRejected(
            StructureRevisionRejectionCode.STALE_BASE_REVISION,
            "Structure mutation must bind the current effective revision",
        )


__all__ = [
    "DOCUMENT_REQUIREMENT_AGENT_STAGE_ORDER",
    "DocumentRequirementAgentStage",
    "DocumentRequirementAgentStageState",
    "DocumentRequirementAgentStageStatus",
    "STRUCTURE_DERIVATION_SCOPE_SCHEMA_VERSION",
    "STRUCTURE_REPAIR_RESULT_SCHEMA_VERSION",
    "StructureDerivationScopeV1",
    "StructureManualMutationResult",
    "StructureProposalDisposition",
    "StructureRepairProposalOutcome",
    "StructureRepairResult",
    "StructureRepairStatus",
    "apply_manual_structure_operations",
    "build_agent_stage_states",
    "derive_structure_derivation_scope",
    "initialize_structure_repair_without_provider",
    "run_structure_repair_with_provider",
    "undo_current_structure_revision",
]
