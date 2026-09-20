"""Deterministic Host contracts for adjacent object reconciliation.

Object-local parent recognition deliberately cannot claim source spans owned by
an adjacent logical object.  When a validated local candidate asks for
adjacent reconciliation, this module builds the smallest permitted groups,
invalidates every local result in those groups, compiles one replacement
candidate per group, and replays the canonical whole-document compiler.

Execution failure is intentionally absent from the candidate schema.  A
transport failure, cancellation, or rejected output has no reconciliation
candidate and therefore keeps aggregation blocked instead of manufacturing a
semantic unresolved disposition.
"""

from __future__ import annotations

from enum import StrEnum
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from app.application.agent_workflow.d3_parent_provision_compiler import (
    D3CandidateSourceSpanV2,
    D3HostCompiledParentProvisionDecisionV1,
    D3ParentBoundaryClassificationCandidateV2,
    D3ParentBoundaryClassificationEntryV2,
    compile_d3_parent_provision_decision,
)
from app.application.agent_workflow.d3_parent_provisions import (
    D3ExactSourceSegmentV1,
    D3ValidatedD2ArtifactIdentityV1,
    canonical_json_sha256,
    d3_source_scope_sha256,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from app.application.agent_workflow.parent_provision_object_batches import (
    PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN,
    ParentProvisionObjectBatchCompilationV1,
    ParentProvisionObjectBatchPlanV1,
    ParentProvisionObjectExecutionMode,
    ParentProvisionObjectScopeStatus,
    compile_parent_provision_object_candidate,
)


PARENT_PROVISION_OBJECT_RECONCILIATION_PLAN_SCHEMA_VERSION = (
    "parent-provision-object-reconciliation-plan/v1"
)
PARENT_PROVISION_OBJECT_RECONCILIATION_CANDIDATE_SCHEMA_VERSION = (
    "parent-provision-object-reconciliation-candidate/v1"
)
PARENT_PROVISION_OBJECT_RECONCILIATION_COMPILATION_SCHEMA_VERSION = (
    "parent-provision-object-reconciliation-compilation/v1"
)
PARENT_PROVISION_OBJECT_RECONCILIATION_AGGREGATE_SCHEMA_VERSION = (
    "parent-provision-object-reconciliation-aggregate/v1"
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")
PARENT_PROVISION_OBJECT_RECONCILIATION_SEMANTIC_DIAGNOSTIC_CODES = frozenset(
    {
        "ADJACENT_BOUNDARY_AMBIGUOUS",
        "CONFLICTING_ADJACENT_LABELS",
        "CROSS_OBJECT_PARENT_AMBIGUOUS",
        "LABEL_CONTINUATION_AMBIGUOUS",
        "SEMANTIC_SCOPE_AMBIGUOUS",
        "SOURCE_BOUNDARY_AMBIGUOUS",
        "TABLE_CONTINUATION_AMBIGUOUS",
        "UNRESOLVED_PARENT_BOUNDARY",
    }
)


class ParentProvisionObjectReconciliationError(ValueError):
    """Fail-closed Host boundary for adjacent object reconciliation."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ParentProvisionObjectReconciliationStatus(StrEnum):
    RESOLVED_WITHIN_GROUP = "RESOLVED_WITHIN_GROUP"
    UNRESOLVED = "UNRESOLVED"


class ParentProvisionObjectCoverageSource(StrEnum):
    LOCAL_RESULT = "LOCAL_RESULT"
    RECONCILIATION_REPLACEMENT = "RECONCILIATION_REPLACEMENT"


class ParentProvisionObjectReconciliationReplacementV1(_StrictFrozenModel):
    batch_id: str = Field(pattern=r"^parent-provision-object-batch-[0-9a-f]{32}$")
    batch_order: StrictInt = Field(ge=0, le=8191)
    logical_object_id: str = Field(pattern=PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN)
    target_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    local_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    marked_for_reconciliation: bool


class ParentProvisionObjectReconciliationGroupV1(_StrictFrozenModel):
    group_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-group-[0-9a-f]{32}$"
    )
    group_order: StrictInt = Field(ge=0, le=8191)
    first_batch_order: StrictInt = Field(ge=0, le=8191)
    last_batch_order: StrictInt = Field(ge=0, le=8191)
    replacements: tuple[ParentProvisionObjectReconciliationReplacementV1, ...] = Field(
        min_length=1, max_length=8192
    )
    marked_batch_ids: tuple[str, ...] = Field(min_length=1, max_length=8192)
    global_segment_ordinals: tuple[StrictInt, ...] = Field(
        min_length=1,
        max_length=65536,
    )
    group_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("marked_batch_ids")
    @classmethod
    def validate_marked_batch_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"parent-provision-object-batch-[0-9a-f]{32}", item) is None
            for item in value
        ):
            raise ValueError("reconciliation marked batch IDs are invalid")
        return value

    @field_validator("global_segment_ordinals")
    @classmethod
    def validate_global_ordinals(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(item < 0 for item in value):
            raise ValueError(
                "reconciliation segment ordinals must be sorted and unique"
            )
        return value

    @model_validator(mode="after")
    def validate_group(self) -> ParentProvisionObjectReconciliationGroupV1:
        orders = tuple(item.batch_order for item in self.replacements)
        if orders != tuple(sorted(orders)) or len(orders) != len(set(orders)):
            raise ValueError("reconciliation replacements must preserve batch order")
        if self.first_batch_order != orders[0] or self.last_batch_order != orders[-1]:
            raise ValueError("reconciliation group boundaries drifted")
        if len({item.batch_id for item in self.replacements}) != len(
            self.replacements
        ) or len({item.logical_object_id for item in self.replacements}) != len(
            self.replacements
        ):
            raise ValueError("reconciliation replacements must be unique")
        expected_marked = tuple(
            item.batch_id
            for item in self.replacements
            if item.marked_for_reconciliation
        )
        if self.marked_batch_ids != expected_marked:
            raise ValueError("reconciliation marked scope drifted from replacements")
        expected_scope_sha256 = canonical_json_sha256(
            self.model_dump(
                mode="json",
                exclude={"group_id", "group_scope_sha256"},
            )
        )
        if self.group_scope_sha256 != expected_scope_sha256:
            raise ValueError("reconciliation group scope SHA drifted")
        if self.group_id != (
            f"parent-provision-object-reconciliation-group-{expected_scope_sha256[:32]}"
        ):
            raise ValueError("reconciliation group identity drifted")
        return self


class ParentProvisionObjectReconciliationPlanV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-object-reconciliation-plan/v1"] = (
        PARENT_PROVISION_OBJECT_RECONCILIATION_PLAN_SCHEMA_VERSION
    )
    reconciliation_plan_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-plan-[0-9a-f]{32}$"
    )
    reconciliation_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_batch_plan_id: str = Field(
        pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$"
    )
    object_batch_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    groups: tuple[ParentProvisionObjectReconciliationGroupV1, ...] = Field(
        min_length=1,
        max_length=8192,
    )
    invalidated_local_result_sha256s: tuple[str, ...] = Field(
        min_length=1,
        max_length=8192,
    )

    @model_validator(mode="after")
    def validate_plan(self) -> ParentProvisionObjectReconciliationPlanV1:
        member_batch_ids = tuple(
            replacement.batch_id
            for group in self.groups
            for replacement in group.replacements
        )
        if len(member_batch_ids) != len(set(member_batch_ids)):
            raise ValueError("reconciliation groups overlap")
        if tuple(item.group_order for item in self.groups) != tuple(
            range(len(self.groups))
        ):
            raise ValueError("reconciliation group order must be contiguous")
        if tuple(item.first_batch_order for item in self.groups) != tuple(
            sorted(item.first_batch_order for item in self.groups)
        ):
            raise ValueError("reconciliation groups must preserve source order")
        expected_invalidated = tuple(
            replacement.local_result_sha256
            for group in self.groups
            for replacement in group.replacements
        )
        if self.invalidated_local_result_sha256s != expected_invalidated:
            raise ValueError("reconciliation invalidation inventory drifted")
        payload = self.model_dump(
            mode="json",
            exclude={"reconciliation_plan_id", "reconciliation_plan_sha256"},
        )
        expected_sha256 = canonical_json_sha256(payload)
        if self.reconciliation_plan_sha256 != expected_sha256:
            raise ValueError("reconciliation plan SHA drifted")
        if self.reconciliation_plan_id != (
            f"parent-provision-object-reconciliation-plan-{expected_sha256[:32]}"
        ):
            raise ValueError("reconciliation plan identity drifted")
        return self


class ParentProvisionObjectReconciliationCandidateV1(_StrictFrozenModel):
    """Strict runner output; source spans remain global ordinal selections."""

    schema_version: Literal["parent-provision-object-reconciliation-candidate/v1"] = (
        PARENT_PROVISION_OBJECT_RECONCILIATION_CANDIDATE_SCHEMA_VERSION
    )
    status: ParentProvisionObjectReconciliationStatus
    entries: tuple[D3ParentBoundaryClassificationEntryV2, ...] = Field(
        default=(),
        max_length=65536,
    )
    unresolved_logical_object_ids: tuple[str, ...] = Field(
        default=(),
        max_length=8192,
    )
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=256)

    @field_validator("unresolved_logical_object_ids")
    @classmethod
    def validate_unresolved_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("reconciliation unresolved object IDs must be unique")
        return value

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _MACHINE_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("reconciliation diagnostics must be machine codes")
        if not set(value).issubset(
            PARENT_PROVISION_OBJECT_RECONCILIATION_SEMANTIC_DIAGNOSTIC_CODES
        ):
            raise ValueError(
                "reconciliation diagnostics must use the semantic allowlist; "
                "execution failure cannot be encoded as semantic reconciliation"
            )
        return value

    @model_validator(mode="after")
    def validate_candidate(self) -> ParentProvisionObjectReconciliationCandidateV1:
        if (
            self.status
            is ParentProvisionObjectReconciliationStatus.RESOLVED_WITHIN_GROUP
        ):
            if self.unresolved_logical_object_ids or self.diagnostic_codes:
                raise ValueError(
                    "resolved reconciliation cannot carry unresolved diagnostics"
                )
        elif not self.unresolved_logical_object_ids or not self.diagnostic_codes:
            raise ValueError(
                "semantic unresolved reconciliation requires exact scope and diagnostics"
            )
        return self

    @property
    def candidate_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


class ParentProvisionObjectReconciliationCompilationV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-object-reconciliation-compilation/v1"] = (
        PARENT_PROVISION_OBJECT_RECONCILIATION_COMPILATION_SCHEMA_VERSION
    )
    reconciliation_plan_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-plan-[0-9a-f]{32}$"
    )
    reconciliation_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_batch_plan_id: str = Field(
        pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$"
    )
    object_batch_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    group_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-group-[0-9a-f]{32}$"
    )
    group_order: StrictInt = Field(ge=0, le=8191)
    group_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replaced_local_result_sha256s: tuple[str, ...] = Field(min_length=1)
    model_candidate: ParentProvisionObjectReconciliationCandidateV1
    model_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled_decision: D3HostCompiledParentProvisionDecisionV1
    group_disposition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_compilation(
        self,
    ) -> ParentProvisionObjectReconciliationCompilationV1:
        if any(
            _SHA256_PATTERN.fullmatch(item) is None
            for item in self.replaced_local_result_sha256s
        ):
            raise ValueError("reconciliation replacement result SHA is invalid")
        if self.model_candidate_sha256 != self.model_candidate.candidate_sha256:
            raise ValueError("reconciliation candidate SHA drifted")
        if self.compiled_decision.unresolved_logical_object_ids != (
            self.model_candidate.unresolved_logical_object_ids
        ):
            raise ValueError("reconciliation compiled disposition drifted")
        expected_disposition_sha256 = canonical_json_sha256(
            {
                "group_scope_sha256": self.group_scope_sha256,
                "replaced_local_result_sha256s": (self.replaced_local_result_sha256s),
                "model_candidate_sha256": self.model_candidate_sha256,
                "compiled_decision_sha256": self.compiled_decision.decision_sha256,
            }
        )
        if self.group_disposition_sha256 != expected_disposition_sha256:
            raise ValueError("reconciliation group disposition SHA drifted")
        payload = self.model_dump(mode="json", exclude={"result_sha256"})
        if self.result_sha256 != canonical_json_sha256(payload):
            raise ValueError("reconciliation compilation result SHA drifted")
        return self


class ParentProvisionObjectFinalCoverageV1(_StrictFrozenModel):
    batch_id: str = Field(pattern=r"^parent-provision-object-batch-[0-9a-f]{32}$")
    batch_order: StrictInt = Field(ge=0, le=8191)
    logical_object_id: str = Field(pattern=PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN)
    coverage_source: ParentProvisionObjectCoverageSource
    selected_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replaced_local_result_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    reconciliation_group_id: str | None = Field(
        default=None,
        pattern=r"^parent-provision-object-reconciliation-group-[0-9a-f]{32}$",
    )

    @model_validator(mode="after")
    def validate_coverage(self) -> ParentProvisionObjectFinalCoverageV1:
        if self.coverage_source is ParentProvisionObjectCoverageSource.LOCAL_RESULT:
            if self.replaced_local_result_sha256 or self.reconciliation_group_id:
                raise ValueError("local coverage cannot claim a replacement")
        elif not self.replaced_local_result_sha256 or not self.reconciliation_group_id:
            raise ValueError("reconciliation coverage requires replacement proof")
        return self


class ParentProvisionObjectReconciliationAggregateV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-object-reconciliation-aggregate/v1"] = (
        PARENT_PROVISION_OBJECT_RECONCILIATION_AGGREGATE_SCHEMA_VERSION
    )
    object_batch_plan_id: str = Field(
        pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$"
    )
    object_batch_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_plan_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-plan-[0-9a-f]{32}$"
    )
    reconciliation_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordered_model_target_coverage: tuple[ParentProvisionObjectFinalCoverageV1, ...]
    invalidated_local_result_sha256s: tuple[str, ...]
    ordered_reconciliation_result_sha256s: tuple[str, ...]
    global_candidate: D3ParentBoundaryClassificationCandidateV2
    global_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled_decision: D3HostCompiledParentProvisionDecisionV1
    aggregate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_aggregate(self) -> ParentProvisionObjectReconciliationAggregateV1:
        if len({item.batch_id for item in self.ordered_model_target_coverage}) != len(
            self.ordered_model_target_coverage
        ):
            raise ValueError("aggregate model target coverage is not exact")
        if tuple(
            item.batch_order for item in self.ordered_model_target_coverage
        ) != tuple(
            sorted(item.batch_order for item in self.ordered_model_target_coverage)
        ):
            raise ValueError("aggregate model target coverage is not source ordered")
        if any(
            _SHA256_PATTERN.fullmatch(item) is None
            for item in (
                *self.invalidated_local_result_sha256s,
                *self.ordered_reconciliation_result_sha256s,
            )
        ):
            raise ValueError("aggregate reconciliation SHA inventory is invalid")
        if self.global_candidate_sha256 != self.global_candidate.candidate_sha256:
            raise ValueError("aggregate global candidate SHA drifted")
        payload = self.model_dump(mode="json", exclude={"aggregate_sha256"})
        if self.aggregate_sha256 != canonical_json_sha256(payload):
            raise ValueError("reconciliation aggregate SHA drifted")
        return self


def build_parent_provision_object_reconciliation_plan(
    *,
    object_batch_plan: ParentProvisionObjectBatchPlanV1,
    expected_object_batch_plan_sha256: str,
    local_results: tuple[ParentProvisionObjectBatchCompilationV1, ...],
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    kit_identity: AgentKitIdentityV1,
) -> ParentProvisionObjectReconciliationPlanV1:
    """Build minimal neighbor groups from Host-replayed object-local results."""

    plan, identity, segments, kit, result_by_id = _validated_local_result_set(
        object_batch_plan=object_batch_plan,
        expected_object_batch_plan_sha256=expected_object_batch_plan_sha256,
        local_results=local_results,
        d2_identity=d2_identity,
        source_segments=source_segments,
        kit_identity=kit_identity,
    )
    model_targets = tuple(
        item
        for item in plan.targets
        if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
    )
    marked_indexes = tuple(
        index
        for index, target in enumerate(model_targets)
        if result_by_id[target.batch_id].model_candidate.scope_status
        is ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
    )
    if not marked_indexes:
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_NOT_REQUIRED"
        )

    intervals = _minimal_neighbor_intervals(
        marked_indexes=marked_indexes,
        target_count=len(model_targets),
    )
    groups: list[ParentProvisionObjectReconciliationGroupV1] = []
    for group_order, (start, end) in enumerate(intervals):
        selected_targets = model_targets[start : end + 1]
        replacements = tuple(
            ParentProvisionObjectReconciliationReplacementV1(
                batch_id=target.batch_id,
                batch_order=target.batch_order,
                logical_object_id=target.logical_object_id,
                target_scope_sha256=target.target_scope_sha256,
                local_result_sha256=result_by_id[target.batch_id].result_sha256,
                marked_for_reconciliation=(
                    result_by_id[target.batch_id].model_candidate.scope_status
                    is ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
                ),
            )
            for target in selected_targets
        )
        group_values = {
            "group_order": group_order,
            "first_batch_order": selected_targets[0].batch_order,
            "last_batch_order": selected_targets[-1].batch_order,
            "replacements": replacements,
            "marked_batch_ids": tuple(
                item.batch_id for item in replacements if item.marked_for_reconciliation
            ),
            "global_segment_ordinals": tuple(
                sorted(
                    ordinal
                    for target in selected_targets
                    for ordinal in target.segment_ordinals
                )
            ),
        }
        scope_sha256 = canonical_json_sha256(
            ParentProvisionObjectReconciliationGroupV1.model_construct(
                group_id="parent-provision-object-reconciliation-group-" + "0" * 32,
                group_scope_sha256="0" * 64,
                **group_values,
            ).model_dump(mode="json", exclude={"group_id", "group_scope_sha256"})
        )
        groups.append(
            ParentProvisionObjectReconciliationGroupV1(
                group_id=(
                    "parent-provision-object-reconciliation-group-" + scope_sha256[:32]
                ),
                group_scope_sha256=scope_sha256,
                **group_values,
            )
        )

    plan_values = {
        "object_batch_plan_id": plan.plan_id,
        "object_batch_plan_sha256": plan.plan_sha256,
        "run_id": identity.run_id,
        "source_sha256": identity.source_sha256,
        "kit_bundle_sha256": kit.bundle_sha256,
        "groups": tuple(groups),
        "invalidated_local_result_sha256s": tuple(
            replacement.local_result_sha256
            for group in groups
            for replacement in group.replacements
        ),
    }
    serializable = ParentProvisionObjectReconciliationPlanV1.model_construct(
        reconciliation_plan_id=(
            "parent-provision-object-reconciliation-plan-" + "0" * 32
        ),
        reconciliation_plan_sha256="0" * 64,
        **plan_values,
    ).model_dump(
        mode="json",
        exclude={"reconciliation_plan_id", "reconciliation_plan_sha256"},
    )
    plan_sha256 = canonical_json_sha256(serializable)
    return ParentProvisionObjectReconciliationPlanV1(
        reconciliation_plan_id=(
            "parent-provision-object-reconciliation-plan-" + plan_sha256[:32]
        ),
        reconciliation_plan_sha256=plan_sha256,
        **plan_values,
    )


def compile_parent_provision_object_reconciliation_candidate(
    *,
    object_batch_plan: ParentProvisionObjectBatchPlanV1,
    expected_object_batch_plan_sha256: str,
    reconciliation_plan: ParentProvisionObjectReconciliationPlanV1,
    expected_reconciliation_plan_sha256: str,
    group_id: str,
    local_results: tuple[ParentProvisionObjectBatchCompilationV1, ...],
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    kit_identity: AgentKitIdentityV1,
    candidate: ParentProvisionObjectReconciliationCandidateV1,
) -> ParentProvisionObjectReconciliationCompilationV1:
    """Compile one replacement group through a scoped canonical compiler."""

    plan, identity, segments, kit, _ = _validated_local_result_set(
        object_batch_plan=object_batch_plan,
        expected_object_batch_plan_sha256=expected_object_batch_plan_sha256,
        local_results=local_results,
        d2_identity=d2_identity,
        source_segments=source_segments,
        kit_identity=kit_identity,
    )
    canonical_reconciliation_plan = build_parent_provision_object_reconciliation_plan(
        object_batch_plan=plan,
        expected_object_batch_plan_sha256=plan.plan_sha256,
        local_results=local_results,
        d2_identity=identity,
        source_segments=segments,
        kit_identity=kit,
    )
    reconciliation_plan = _validated_reconciliation_plan(
        reconciliation_plan,
        expected_reconciliation_plan_sha256=expected_reconciliation_plan_sha256,
    )
    if reconciliation_plan != canonical_reconciliation_plan:
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_PLAN_SCOPE_DRIFT"
        )
    group = _group_by_id(reconciliation_plan, group_id)
    candidate = ParentProvisionObjectReconciliationCandidateV1.model_validate(
        candidate.model_dump(mode="json")
    )
    group_object_ids = tuple(item.logical_object_id for item in group.replacements)
    unresolved_set = set(candidate.unresolved_logical_object_ids)
    if not unresolved_set.issubset(group_object_ids):
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_UNRESOLVED_SCOPE_ESCAPES_GROUP"
        )
    expected_unresolved = tuple(
        object_id for object_id in group_object_ids if object_id in unresolved_set
    )
    if candidate.unresolved_logical_object_ids != expected_unresolved:
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_UNRESOLVED_SCOPE_ORDER_DRIFT"
        )

    ordinal_map = {
        global_ordinal: local_ordinal
        for local_ordinal, global_ordinal in enumerate(group.global_segment_ordinals)
    }
    translated_entries = tuple(
        _translate_entry_to_group(
            entry,
            group_ordinals=set(group.global_segment_ordinals),
            ordinal_map=ordinal_map,
            source_segments=segments,
        )
        for entry in candidate.entries
    )
    scoped_segments = tuple(
        segments[ordinal] for ordinal in group.global_segment_ordinals
    )
    scoped_identity = D3ValidatedD2ArtifactIdentityV1(
        artifact_id=identity.artifact_id,
        artifact_sha256=identity.artifact_sha256,
        validator_receipt_sha256=identity.validator_receipt_sha256,
        run_id=identity.run_id,
        source_sha256=identity.source_sha256,
        source_ir_id=identity.source_ir_id,
        source_ir_canonical_sha256=identity.source_ir_canonical_sha256,
        kit_identity=identity.kit_identity,
        status=identity.status,
        logical_object_ids=group_object_ids,
        source_segment_ids=tuple(item.segment_id for item in scoped_segments),
        source_scope_sha256=d3_source_scope_sha256(scoped_segments),
    )
    compiled = compile_d3_parent_provision_decision(
        d2_identity=scoped_identity,
        source_segments=scoped_segments,
        kit_identity=kit,
        candidate=D3ParentBoundaryClassificationCandidateV2(
            entries=translated_entries,
            unresolved_logical_object_ids=candidate.unresolved_logical_object_ids,
        ),
    )
    if set(compiled.declared_logical_object_ids) | set(
        compiled.unresolved_logical_object_ids
    ) != set(group_object_ids):
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_GROUP_DISPOSITION_INCOMPLETE"
        )
    group_disposition_sha256 = canonical_json_sha256(
        {
            "group_scope_sha256": group.group_scope_sha256,
            "replaced_local_result_sha256s": tuple(
                item.local_result_sha256 for item in group.replacements
            ),
            "model_candidate_sha256": candidate.candidate_sha256,
            "compiled_decision_sha256": compiled.decision_sha256,
        }
    )
    values = {
        "reconciliation_plan_id": reconciliation_plan.reconciliation_plan_id,
        "reconciliation_plan_sha256": (reconciliation_plan.reconciliation_plan_sha256),
        "object_batch_plan_id": plan.plan_id,
        "object_batch_plan_sha256": plan.plan_sha256,
        "group_id": group.group_id,
        "group_order": group.group_order,
        "group_scope_sha256": group.group_scope_sha256,
        "replaced_local_result_sha256s": tuple(
            item.local_result_sha256 for item in group.replacements
        ),
        "model_candidate": candidate,
        "model_candidate_sha256": candidate.candidate_sha256,
        "compiled_decision": compiled,
        "group_disposition_sha256": group_disposition_sha256,
    }
    serializable = ParentProvisionObjectReconciliationCompilationV1.model_construct(
        result_sha256="0" * 64,
        **values,
    ).model_dump(mode="json", exclude={"result_sha256"})
    return ParentProvisionObjectReconciliationCompilationV1(
        **values,
        result_sha256=canonical_json_sha256(serializable),
    )


def aggregate_parent_provision_objects_with_reconciliation(
    *,
    object_batch_plan: ParentProvisionObjectBatchPlanV1,
    expected_object_batch_plan_sha256: str,
    reconciliation_plan: ParentProvisionObjectReconciliationPlanV1,
    expected_reconciliation_plan_sha256: str,
    local_results: tuple[ParentProvisionObjectBatchCompilationV1, ...],
    reconciliation_results: tuple[
        ParentProvisionObjectReconciliationCompilationV1, ...
    ],
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    kit_identity: AgentKitIdentityV1,
) -> ParentProvisionObjectReconciliationAggregateV1:
    """Replace coordinated local results and replay exact whole-document proof."""

    plan, identity, segments, kit, local_by_id = _validated_local_result_set(
        object_batch_plan=object_batch_plan,
        expected_object_batch_plan_sha256=expected_object_batch_plan_sha256,
        local_results=local_results,
        d2_identity=d2_identity,
        source_segments=source_segments,
        kit_identity=kit_identity,
    )
    canonical_reconciliation_plan = build_parent_provision_object_reconciliation_plan(
        object_batch_plan=plan,
        expected_object_batch_plan_sha256=plan.plan_sha256,
        local_results=local_results,
        d2_identity=identity,
        source_segments=segments,
        kit_identity=kit,
    )
    reconciliation_plan = _validated_reconciliation_plan(
        reconciliation_plan,
        expected_reconciliation_plan_sha256=expected_reconciliation_plan_sha256,
    )
    if reconciliation_plan != canonical_reconciliation_plan:
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_PLAN_SCOPE_DRIFT"
        )

    validated_results = tuple(
        ParentProvisionObjectReconciliationCompilationV1.model_validate(
            item.model_dump(mode="json")
        )
        for item in reconciliation_results
    )
    if len({item.group_id for item in validated_results}) != len(validated_results):
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_DUPLICATE_RESULT"
        )
    result_by_group = {item.group_id: item for item in validated_results}
    if set(result_by_group) != {item.group_id for item in reconciliation_plan.groups}:
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_RESULT_SET_INCOMPLETE"
        )
    ordered_reconciliation_results: list[
        ParentProvisionObjectReconciliationCompilationV1
    ] = []
    for group in reconciliation_plan.groups:
        result = result_by_group[group.group_id]
        replayed = compile_parent_provision_object_reconciliation_candidate(
            object_batch_plan=plan,
            expected_object_batch_plan_sha256=plan.plan_sha256,
            reconciliation_plan=reconciliation_plan,
            expected_reconciliation_plan_sha256=(
                reconciliation_plan.reconciliation_plan_sha256
            ),
            group_id=group.group_id,
            local_results=local_results,
            d2_identity=identity,
            source_segments=segments,
            kit_identity=kit,
            candidate=result.model_candidate,
        )
        if replayed != result:
            raise ParentProvisionObjectReconciliationError(
                "OBJECT_RECONCILIATION_RESULT_REPLAY_DRIFT"
            )
        ordered_reconciliation_results.append(result)

    group_by_batch_id = {
        replacement.batch_id: group
        for group in reconciliation_plan.groups
        for replacement in group.replacements
    }
    model_targets = tuple(
        item
        for item in plan.targets
        if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
    )
    entries: list[D3ParentBoundaryClassificationEntryV2] = []
    unresolved_ids: set[str] = set()
    coverages: list[ParentProvisionObjectFinalCoverageV1] = []
    emitted_groups: set[str] = set()
    for target in model_targets:
        local_result = local_by_id[target.batch_id]
        group = group_by_batch_id.get(target.batch_id)
        if group is None:
            if (
                local_result.model_candidate.scope_status
                is ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
            ):
                raise ParentProvisionObjectReconciliationError(
                    "OBJECT_RECONCILIATION_TARGET_NOT_REPLACED"
                )
            if local_result.compiled_decision is None:
                raise ParentProvisionObjectReconciliationError(
                    "OBJECT_RECONCILIATION_LOCAL_RESULT_NOT_COMPILED"
                )
            if (
                local_result.model_candidate.scope_status
                is ParentProvisionObjectScopeStatus.UNRESOLVED
            ):
                unresolved_ids.add(target.logical_object_id)
            else:
                entries.extend(local_result.model_candidate.entries)
            coverages.append(
                ParentProvisionObjectFinalCoverageV1(
                    batch_id=target.batch_id,
                    batch_order=target.batch_order,
                    logical_object_id=target.logical_object_id,
                    coverage_source=ParentProvisionObjectCoverageSource.LOCAL_RESULT,
                    selected_result_sha256=local_result.result_sha256,
                )
            )
            continue

        reconciliation_result = result_by_group[group.group_id]
        replacement = next(
            item for item in group.replacements if item.batch_id == target.batch_id
        )
        if replacement.local_result_sha256 != local_result.result_sha256:
            raise ParentProvisionObjectReconciliationError(
                "OBJECT_RECONCILIATION_REPLACEMENT_LINEAGE_DRIFT"
            )
        if group.group_id not in emitted_groups:
            entries.extend(reconciliation_result.model_candidate.entries)
            unresolved_ids.update(
                reconciliation_result.model_candidate.unresolved_logical_object_ids
            )
            emitted_groups.add(group.group_id)
        coverages.append(
            ParentProvisionObjectFinalCoverageV1(
                batch_id=target.batch_id,
                batch_order=target.batch_order,
                logical_object_id=target.logical_object_id,
                coverage_source=(
                    ParentProvisionObjectCoverageSource.RECONCILIATION_REPLACEMENT
                ),
                selected_result_sha256=reconciliation_result.result_sha256,
                replaced_local_result_sha256=local_result.result_sha256,
                reconciliation_group_id=group.group_id,
            )
        )

    if len(coverages) != len(model_targets):
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_MODEL_TARGET_COVERAGE_INCOMPLETE"
        )
    global_candidate = D3ParentBoundaryClassificationCandidateV2(
        entries=tuple(sorted(entries, key=_candidate_entry_source_key)),
        unresolved_logical_object_ids=tuple(
            object_id
            for object_id in identity.logical_object_ids
            if object_id in unresolved_ids
        ),
    )
    compiled = compile_d3_parent_provision_decision(
        d2_identity=identity,
        source_segments=segments,
        kit_identity=kit,
        candidate=global_candidate,
    )
    values = {
        "object_batch_plan_id": plan.plan_id,
        "object_batch_plan_sha256": plan.plan_sha256,
        "reconciliation_plan_id": reconciliation_plan.reconciliation_plan_id,
        "reconciliation_plan_sha256": (reconciliation_plan.reconciliation_plan_sha256),
        "ordered_model_target_coverage": tuple(coverages),
        "invalidated_local_result_sha256s": (
            reconciliation_plan.invalidated_local_result_sha256s
        ),
        "ordered_reconciliation_result_sha256s": tuple(
            item.result_sha256 for item in ordered_reconciliation_results
        ),
        "global_candidate": global_candidate,
        "global_candidate_sha256": global_candidate.candidate_sha256,
        "compiled_decision": compiled,
    }
    serializable = ParentProvisionObjectReconciliationAggregateV1.model_construct(
        aggregate_sha256="0" * 64,
        **values,
    ).model_dump(mode="json", exclude={"aggregate_sha256"})
    return ParentProvisionObjectReconciliationAggregateV1(
        **values,
        aggregate_sha256=canonical_json_sha256(serializable),
    )


def _candidate_entry_source_key(
    entry: D3ParentBoundaryClassificationEntryV2,
) -> tuple[int, int, int, int]:
    return min(
        (
            span.start_segment_ordinal,
            span.start_offset,
            span.end_segment_ordinal,
            span.end_offset,
        )
        for span in entry.source_spans
    )


def _validated_local_result_set(
    *,
    object_batch_plan: ParentProvisionObjectBatchPlanV1,
    expected_object_batch_plan_sha256: str,
    local_results: tuple[ParentProvisionObjectBatchCompilationV1, ...],
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    kit_identity: AgentKitIdentityV1,
) -> tuple[
    ParentProvisionObjectBatchPlanV1,
    D3ValidatedD2ArtifactIdentityV1,
    tuple[D3ExactSourceSegmentV1, ...],
    AgentKitIdentityV1,
    dict[str, ParentProvisionObjectBatchCompilationV1],
]:
    plan = ParentProvisionObjectBatchPlanV1.model_validate(
        object_batch_plan.model_dump(mode="json")
    )
    if plan.plan_sha256 != expected_object_batch_plan_sha256:
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_OBJECT_PLAN_PIN_MISMATCH"
        )
    identity = D3ValidatedD2ArtifactIdentityV1.model_validate(
        d2_identity.model_dump(mode="json")
    )
    segments = tuple(
        D3ExactSourceSegmentV1.model_validate(item.model_dump(mode="json"))
        for item in source_segments
    )
    kit = AgentKitIdentityV1.model_validate(kit_identity.model_dump(mode="json"))
    if (
        plan.run_id != identity.run_id
        or plan.source_sha256 != identity.source_sha256
        or plan.kit_bundle_sha256 != kit.bundle_sha256
        or identity.kit_identity != kit
        or plan.predecessor_artifact_id != identity.artifact_id
        or plan.predecessor_artifact_sha256 != identity.artifact_sha256
        or plan.predecessor_validator_receipt_sha256
        != identity.validator_receipt_sha256
        or plan.source_scope_sha256 != identity.source_scope_sha256
        or tuple(item.segment_id for item in segments) != identity.source_segment_ids
        or d3_source_scope_sha256(segments) != identity.source_scope_sha256
    ):
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_RUNTIME_LINEAGE_DRIFT"
        )
    if any(
        target.execution_mode is ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED
        for target in plan.targets
    ):
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_EVIDENCE_RECOVERY_REQUIRED"
        )
    model_targets = tuple(
        item
        for item in plan.targets
        if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
    )
    validated_results = tuple(
        ParentProvisionObjectBatchCompilationV1.model_validate(
            item.model_dump(mode="json")
        )
        for item in local_results
    )
    if len({item.batch_id for item in validated_results}) != len(validated_results):
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_DUPLICATE_LOCAL_RESULT"
        )
    result_by_id = {item.batch_id: item for item in validated_results}
    if set(result_by_id) != {item.batch_id for item in model_targets}:
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_LOCAL_RESULT_SET_INCOMPLETE"
        )
    for target in model_targets:
        result = result_by_id[target.batch_id]
        if (
            result.plan_id != plan.plan_id
            or result.plan_sha256 != plan.plan_sha256
            or result.batch_order != target.batch_order
            or result.logical_object_id != target.logical_object_id
            or result.target_scope_sha256 != target.target_scope_sha256
        ):
            raise ParentProvisionObjectReconciliationError(
                "OBJECT_RECONCILIATION_LOCAL_RESULT_LINEAGE_DRIFT"
            )
        replayed = compile_parent_provision_object_candidate(
            plan=plan,
            expected_plan_sha256=plan.plan_sha256,
            batch_id=target.batch_id,
            d2_identity=identity,
            source_segments=segments,
            kit_identity=kit,
            candidate=result.model_candidate,
        )
        if replayed != result:
            raise ParentProvisionObjectReconciliationError(
                "OBJECT_RECONCILIATION_LOCAL_RESULT_REPLAY_DRIFT"
            )
    return plan, identity, segments, kit, result_by_id


def _minimal_neighbor_intervals(
    *,
    marked_indexes: tuple[int, ...],
    target_count: int,
) -> tuple[tuple[int, int], ...]:
    raw = tuple(
        (max(0, index - 1), min(target_count - 1, index + 1))
        for index in marked_indexes
    )
    merged: list[tuple[int, int]] = []
    for start, end in raw:
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def _validated_reconciliation_plan(
    plan: ParentProvisionObjectReconciliationPlanV1,
    *,
    expected_reconciliation_plan_sha256: str,
) -> ParentProvisionObjectReconciliationPlanV1:
    try:
        validated = ParentProvisionObjectReconciliationPlanV1.model_validate(
            plan.model_dump(mode="json")
        )
    except ValueError as exc:
        if "reconciliation groups overlap" in str(exc):
            raise ParentProvisionObjectReconciliationError(
                "OBJECT_RECONCILIATION_GROUPS_OVERLAP"
            ) from exc
        raise
    if validated.reconciliation_plan_sha256 != expected_reconciliation_plan_sha256:
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_PLAN_PIN_MISMATCH"
        )
    return validated


def _group_by_id(
    plan: ParentProvisionObjectReconciliationPlanV1,
    group_id: str,
) -> ParentProvisionObjectReconciliationGroupV1:
    try:
        return next(item for item in plan.groups if item.group_id == group_id)
    except StopIteration as exc:
        raise ParentProvisionObjectReconciliationError(
            "OBJECT_RECONCILIATION_GROUP_UNKNOWN"
        ) from exc


def _translate_entry_to_group(
    entry: D3ParentBoundaryClassificationEntryV2,
    *,
    group_ordinals: set[int],
    ordinal_map: dict[int, int],
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
) -> D3ParentBoundaryClassificationEntryV2:
    translated_spans: list[D3CandidateSourceSpanV2] = []
    for span in entry.source_spans:
        if (
            span.end_segment_ordinal < span.start_segment_ordinal
            or span.start_segment_ordinal >= len(source_segments)
            or span.end_segment_ordinal >= len(source_segments)
        ):
            raise ParentProvisionObjectReconciliationError(
                "OBJECT_RECONCILIATION_SPAN_INVALID"
            )
        start_segment = source_segments[span.start_segment_ordinal]
        end_segment = source_segments[span.end_segment_ordinal]
        if span.start_offset > len(start_segment.source_text) or span.end_offset > len(
            end_segment.source_text
        ):
            raise ParentProvisionObjectReconciliationError(
                "OBJECT_RECONCILIATION_SPAN_INVALID"
            )
        contributing_values: list[tuple[int, int, int]] = []
        for ordinal in range(
            span.start_segment_ordinal,
            span.end_segment_ordinal + 1,
        ):
            start_offset = (
                span.start_offset if ordinal == span.start_segment_ordinal else 0
            )
            end_offset = (
                span.end_offset
                if ordinal == span.end_segment_ordinal
                else len(source_segments[ordinal].source_text)
            )
            if end_offset > start_offset:
                contributing_values.append((ordinal, start_offset, end_offset))
        contributing = tuple(contributing_values)
        selected = {item[0] for item in contributing}
        if not selected or not selected.issubset(group_ordinals):
            raise ParentProvisionObjectReconciliationError(
                "OBJECT_RECONCILIATION_SPAN_ESCAPES_GROUP"
            )
        first_ordinal, first_offset, _ = contributing[0]
        last_ordinal, _, last_offset = contributing[-1]
        translated_spans.append(
            D3CandidateSourceSpanV2(
                start_segment_ordinal=ordinal_map[first_ordinal],
                start_offset=first_offset,
                end_segment_ordinal=ordinal_map[last_ordinal],
                end_offset=last_offset,
            )
        )
    payload = entry.model_dump(mode="json")
    payload["source_spans"] = [
        item.model_dump(mode="json") for item in translated_spans
    ]
    return D3ParentBoundaryClassificationEntryV2.model_validate(payload)
