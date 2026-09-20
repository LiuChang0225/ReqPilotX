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

from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalObjectV1,
)
from app.application.agent_workflow.d2_logical_structure_v2 import D2LogicalObjectV2
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


PARENT_PROVISION_OBJECT_BATCH_PLAN_SCHEMA_VERSION = (
    "parent-provision-object-batch-plan/v1"
)
PARENT_PROVISION_OBJECT_CANDIDATE_SCHEMA_VERSION = (
    "parent-provision-object-candidate/v1"
)
PARENT_PROVISION_OBJECT_WINDOW_CANDIDATE_SCHEMA_VERSION = (
    "parent-provision-object-window-candidate/v1"
)
PARENT_PROVISION_OBJECT_COMPILATION_SCHEMA_VERSION = (
    "parent-provision-object-compilation/v1"
)
PARENT_PROVISION_OBJECT_AGGREGATE_SCHEMA_VERSION = (
    "parent-provision-object-aggregate/v1"
)
_MACHINE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")
PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN = (
    r"^(?:(?:d2-logical-object|agent-logical-table|d2-table-logical-scope|"
    r"d2-v2-region-object)-[0-9a-f]{32})$"
)
_CONTAINER_KINDS = {
    D2LogicalObjectKind.DOCUMENT_UNIT,
    D2LogicalObjectKind.SECTION,
    D2LogicalObjectKind.CONTEXT,
}


class ParentProvisionObjectBatchError(ValueError):
    """Fail-closed Host boundary for object-scoped parent recognition."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ParentProvisionObjectExecutionMode(StrEnum):
    MODEL_TARGET = "MODEL_TARGET"
    HOST_EMPTY_CONTAINER = "HOST_EMPTY_CONTAINER"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class ParentProvisionObjectScopeStatus(StrEnum):
    CLOSED_WITHIN_OBJECT = "CLOSED_WITHIN_OBJECT"
    UNRESOLVED = "UNRESOLVED"
    ADJACENT_RECONCILIATION_REQUIRED = "ADJACENT_RECONCILIATION_REQUIRED"


class ParentProvisionObjectSegmentBindingV1(_StrictFrozenModel):
    segment_ordinal: StrictInt = Field(ge=0)
    segment_id: str = Field(min_length=1, max_length=200)
    source_text_length: StrictInt = Field(ge=0)
    source_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d2_segment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ParentProvisionObjectBatchTargetV1(_StrictFrozenModel):
    batch_id: str = Field(pattern=r"^parent-provision-object-batch-[0-9a-f]{32}$")
    batch_order: StrictInt = Field(ge=0)
    logical_object_id: str = Field(pattern=PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN)
    object_kind: D2LogicalObjectKind
    parent_logical_object_id: str | None = Field(
        default=None,
        pattern=PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN,
    )
    stage_input_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_input_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    runtime_adapter_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    global_source_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    segment_ordinals: tuple[StrictInt, ...]
    segment_bindings: tuple[ParentProvisionObjectSegmentBindingV1, ...]
    execution_mode: ParentProvisionObjectExecutionMode
    target_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("segment_ordinals")
    @classmethod
    def validate_segment_ordinals(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(item < 0 for item in value):
            raise ValueError("object batch segment ordinals must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_target(self) -> ParentProvisionObjectBatchTargetV1:
        if (self.stage_input_manifest_sha256 is None) != (
            self.runtime_adapter_sha256 is None
        ):
            raise ValueError("region runtime target pins must be supplied together")
        if self.segment_ordinals != tuple(
            item.segment_ordinal for item in self.segment_bindings
        ):
            raise ValueError("object batch segment bindings drifted from ordinals")
        if (
            self.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
            and not self.segment_ordinals
        ):
            raise ValueError("model targets require source-segment ordinals")
        if (
            self.execution_mode
            is ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER
            and self.segment_ordinals
        ):
            raise ValueError("Host-empty targets cannot own source-segment ordinals")
        if (
            self.execution_mode
            is ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER
            and self.object_kind not in _CONTAINER_KINDS
        ):
            raise ValueError("only structural containers may be Host-empty targets")
        expected_evidence_sha256 = _target_evidence_sha256(
            object_kind=self.object_kind,
            parent_logical_object_id=self.parent_logical_object_id,
            segment_bindings=self.segment_bindings,
            runtime_adapter_sha256=self.runtime_adapter_sha256,
        )
        if self.target_evidence_sha256 != expected_evidence_sha256:
            raise ValueError("object batch target evidence SHA drifted")
        expected_scope_sha256 = _target_scope_sha256(
            batch_order=self.batch_order,
            logical_object_id=self.logical_object_id,
            stage_input_bundle_sha256=self.stage_input_bundle_sha256,
            global_source_scope_sha256=self.global_source_scope_sha256,
            target_evidence_sha256=self.target_evidence_sha256,
            execution_mode=self.execution_mode,
            stage_input_manifest_sha256=self.stage_input_manifest_sha256,
            runtime_adapter_sha256=self.runtime_adapter_sha256,
        )
        if self.target_scope_sha256 != expected_scope_sha256:
            raise ValueError("object batch target scope SHA drifted")
        if self.batch_id != (
            f"parent-provision-object-batch-{expected_scope_sha256[:32]}"
        ):
            raise ValueError("object batch identity drifted")
        return self


class ParentProvisionObjectBatchPlanV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-object-batch-plan/v1"] = (
        PARENT_PROVISION_OBJECT_BATCH_PLAN_SCHEMA_VERSION
    )
    plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_validator_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_input_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_input_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    runtime_adapter_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    segment_ordinal_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    targets: tuple[ParentProvisionObjectBatchTargetV1, ...] = Field(min_length=1)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> ParentProvisionObjectBatchPlanV1:
        if (self.stage_input_manifest_sha256 is None) != (
            self.runtime_adapter_sha256 is None
        ):
            raise ValueError("region runtime plan pins must be supplied together")
        if tuple(item.batch_order for item in self.targets) != tuple(
            range(len(self.targets))
        ):
            raise ValueError("object batch plan order must be contiguous")
        if len({item.batch_id for item in self.targets}) != len(self.targets):
            raise ValueError("object batch IDs must be unique")
        if len({item.logical_object_id for item in self.targets}) != len(self.targets):
            raise ValueError("object batch targets must be unique")
        if any(
            item.stage_input_bundle_sha256 != self.stage_input_bundle_sha256
            or item.global_source_scope_sha256 != self.source_scope_sha256
            or item.stage_input_manifest_sha256 != self.stage_input_manifest_sha256
            or item.runtime_adapter_sha256 != self.runtime_adapter_sha256
            for item in self.targets
        ):
            raise ValueError("object batch target lineage drifted from its plan")
        ordinals = tuple(
            ordinal for target in self.targets for ordinal in target.segment_ordinals
        )
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("source-segment ordinals cannot enter multiple batches")
        payload = self.model_dump(
            mode="json",
            exclude={"plan_id", "plan_sha256"},
        )
        expected_sha256 = canonical_json_sha256(payload)
        if self.plan_sha256 != expected_sha256:
            raise ValueError("object batch plan SHA drifted")
        if self.plan_id != f"parent-provision-object-plan-{expected_sha256[:32]}":
            raise ValueError("object batch plan identity drifted")
        return self


class ParentProvisionObjectCandidateV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-object-candidate/v1"] = (
        PARENT_PROVISION_OBJECT_CANDIDATE_SCHEMA_VERSION
    )
    scope_status: ParentProvisionObjectScopeStatus
    entries: tuple[D3ParentBoundaryClassificationEntryV2, ...] = Field(
        default=(),
        max_length=65536,
    )
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=256)

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostic_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _MACHINE_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError(
                "object candidate diagnostics must be sorted machine codes"
            )
        return value

    @model_validator(mode="after")
    def validate_candidate(self) -> ParentProvisionObjectCandidateV1:
        if self.scope_status is ParentProvisionObjectScopeStatus.CLOSED_WITHIN_OBJECT:
            if self.diagnostic_codes:
                raise ValueError("closed object candidates cannot carry diagnostics")
        elif self.entries or not self.diagnostic_codes:
            raise ValueError(
                "unresolved or reconciliation candidates require diagnostics and no entries"
            )
        return self

    @property
    def candidate_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


class ParentProvisionObjectWindowItemV1(_StrictFrozenModel):
    batch_id: str = Field(pattern=r"^parent-provision-object-batch-[0-9a-f]{32}$")
    candidate: ParentProvisionObjectCandidateV1


class ParentProvisionObjectWindowCandidateV1(_StrictFrozenModel):
    """One Agent turn over several Host-pinned objects; outputs stay object-local."""

    schema_version: Literal["parent-provision-object-window-candidate/v1"] = (
        PARENT_PROVISION_OBJECT_WINDOW_CANDIDATE_SCHEMA_VERSION
    )
    items: tuple[ParentProvisionObjectWindowItemV1, ...] = Field(
        min_length=1,
        max_length=32,
    )

    @model_validator(mode="after")
    def validate_items(self) -> ParentProvisionObjectWindowCandidateV1:
        batch_ids = tuple(item.batch_id for item in self.items)
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("window candidate batch IDs must be unique")
        return self

    @property
    def candidate_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


class ParentProvisionObjectBatchCompilationV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-object-compilation/v1"] = (
        PARENT_PROVISION_OBJECT_COMPILATION_SCHEMA_VERSION
    )
    plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_id: str = Field(pattern=r"^parent-provision-object-batch-[0-9a-f]{32}$")
    batch_order: StrictInt = Field(ge=0)
    logical_object_id: str = Field(pattern=PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN)
    target_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_candidate: ParentProvisionObjectCandidateV1
    model_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled_decision: D3HostCompiledParentProvisionDecisionV1 | None = None
    target_coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_compilation(self) -> ParentProvisionObjectBatchCompilationV1:
        if self.model_candidate_sha256 != self.model_candidate.candidate_sha256:
            raise ValueError("object batch candidate SHA drifted")
        needs_decision = self.model_candidate.scope_status is not (
            ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
        )
        if needs_decision != (self.compiled_decision is not None):
            raise ValueError("object batch compiled decision does not match its status")
        payload = self.model_dump(mode="json", exclude={"result_sha256"})
        if self.result_sha256 != canonical_json_sha256(payload):
            raise ValueError("object batch result SHA drifted")
        return self


class ParentProvisionObjectBatchAggregateV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-object-aggregate/v1"] = (
        PARENT_PROVISION_OBJECT_AGGREGATE_SCHEMA_VERSION
    )
    plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordered_batch_result_sha256s: tuple[str, ...]
    global_candidate: D3ParentBoundaryClassificationCandidateV2
    global_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled_decision: D3HostCompiledParentProvisionDecisionV1
    aggregate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_aggregate(self) -> ParentProvisionObjectBatchAggregateV1:
        if any(
            re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in (self.ordered_batch_result_sha256s)
        ):
            raise ValueError("aggregate batch result SHAs are invalid")
        if self.global_candidate_sha256 != self.global_candidate.candidate_sha256:
            raise ValueError("aggregate global candidate SHA drifted")
        payload = self.model_dump(mode="json", exclude={"aggregate_sha256"})
        if self.aggregate_sha256 != canonical_json_sha256(payload):
            raise ValueError("object batch aggregate SHA drifted")
        return self


def build_parent_provision_object_batch_plan(
    *,
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    logical_objects: tuple[D2LogicalObjectV1 | D2LogicalObjectV2, ...],
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    stage_input_bundle_sha256: str,
    stage_input_manifest_sha256: str | None = None,
    runtime_adapter_sha256: str | None = None,
) -> ParentProvisionObjectBatchPlanV1:
    """Build one deterministic, content-free plan over every logical object."""

    identity, objects, segments = _validated_inputs(
        d2_identity=d2_identity,
        logical_objects=logical_objects,
        source_segments=source_segments,
    )
    if re.fullmatch(r"[0-9a-f]{64}", stage_input_bundle_sha256) is None:
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_STAGE_INPUT_SHA_INVALID")
    _validate_runtime_binding_mode(
        objects=objects,
        stage_input_manifest_sha256=stage_input_manifest_sha256,
        runtime_adapter_sha256=runtime_adapter_sha256,
    )
    ordinals_by_object: dict[str, list[int]] = {
        object_id: [] for object_id in identity.logical_object_ids
    }
    for ordinal, segment in enumerate(segments):
        ordinals_by_object[segment.logical_object_id].append(ordinal)
    parent_ids = {
        item.parent_logical_object_id
        for item in objects
        if item.parent_logical_object_id is not None
    }
    targets: list[ParentProvisionObjectBatchTargetV1] = []
    for batch_order, logical_object in enumerate(objects):
        ordinals = tuple(ordinals_by_object[logical_object.logical_object_id])
        bindings = tuple(
            _segment_binding(ordinal, segments[ordinal]) for ordinal in ordinals
        )
        if ordinals:
            mode = ParentProvisionObjectExecutionMode.MODEL_TARGET
        elif (
            logical_object.object_kind in _CONTAINER_KINDS
            and logical_object.logical_object_id in parent_ids
        ):
            mode = ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER
        else:
            mode = ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED
        evidence_sha256 = _target_evidence_sha256(
            object_kind=logical_object.object_kind,
            parent_logical_object_id=logical_object.parent_logical_object_id,
            segment_bindings=bindings,
            runtime_adapter_sha256=runtime_adapter_sha256,
        )
        scope_sha256 = _target_scope_sha256(
            batch_order=batch_order,
            logical_object_id=logical_object.logical_object_id,
            stage_input_bundle_sha256=stage_input_bundle_sha256,
            global_source_scope_sha256=identity.source_scope_sha256,
            target_evidence_sha256=evidence_sha256,
            execution_mode=mode,
            stage_input_manifest_sha256=stage_input_manifest_sha256,
            runtime_adapter_sha256=runtime_adapter_sha256,
        )
        targets.append(
            ParentProvisionObjectBatchTargetV1(
                batch_id=f"parent-provision-object-batch-{scope_sha256[:32]}",
                batch_order=batch_order,
                logical_object_id=logical_object.logical_object_id,
                object_kind=logical_object.object_kind,
                parent_logical_object_id=logical_object.parent_logical_object_id,
                stage_input_bundle_sha256=stage_input_bundle_sha256,
                stage_input_manifest_sha256=stage_input_manifest_sha256,
                runtime_adapter_sha256=runtime_adapter_sha256,
                global_source_scope_sha256=identity.source_scope_sha256,
                segment_ordinals=ordinals,
                segment_bindings=bindings,
                execution_mode=mode,
                target_evidence_sha256=evidence_sha256,
                target_scope_sha256=scope_sha256,
            )
        )
    plan_payload = {
        "run_id": identity.run_id,
        "source_sha256": identity.source_sha256,
        "kit_bundle_sha256": identity.kit_identity.bundle_sha256,
        "predecessor_artifact_id": identity.artifact_id,
        "predecessor_artifact_sha256": identity.artifact_sha256,
        "predecessor_validator_receipt_sha256": (identity.validator_receipt_sha256),
        "source_scope_sha256": identity.source_scope_sha256,
        "stage_input_bundle_sha256": stage_input_bundle_sha256,
        "segment_ordinal_ledger_sha256": _segment_ordinal_ledger_sha256(segments),
        "targets": tuple(targets),
    }
    if stage_input_manifest_sha256 is not None:
        plan_payload["stage_input_manifest_sha256"] = stage_input_manifest_sha256
        plan_payload["runtime_adapter_sha256"] = runtime_adapter_sha256
    serializable = ParentProvisionObjectBatchPlanV1.model_construct(
        plan_id="parent-provision-object-plan-" + "0" * 32,
        plan_sha256="0" * 64,
        **plan_payload,
    ).model_dump(mode="json", exclude={"plan_id", "plan_sha256"})
    plan_sha256 = canonical_json_sha256(serializable)
    return ParentProvisionObjectBatchPlanV1(
        plan_id=f"parent-provision-object-plan-{plan_sha256[:32]}",
        plan_sha256=plan_sha256,
        **plan_payload,
    )


def compile_parent_provision_object_candidate(
    *,
    plan: ParentProvisionObjectBatchPlanV1,
    expected_plan_sha256: str,
    batch_id: str,
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    kit_identity: AgentKitIdentityV1,
    candidate: ParentProvisionObjectCandidateV1,
) -> ParentProvisionObjectBatchCompilationV1:
    """Compile one object-local candidate without granting it adjacent scope."""

    plan = _validated_plan(plan, expected_plan_sha256=expected_plan_sha256)
    identity, segments, kit = _validate_runtime_context(
        plan=plan,
        d2_identity=d2_identity,
        source_segments=source_segments,
        kit_identity=kit_identity,
    )
    candidate = ParentProvisionObjectCandidateV1.model_validate(
        candidate.model_dump(mode="json")
    )
    target = _target_by_id(plan, batch_id)
    if target.execution_mode is not ParentProvisionObjectExecutionMode.MODEL_TARGET:
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_NOT_MODEL_TARGET")

    compiled: D3HostCompiledParentProvisionDecisionV1 | None = None
    if (
        candidate.scope_status
        is not ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
    ):
        scoped_segments = tuple(segments[index] for index in target.segment_ordinals)
        ordinal_map = {
            global_ordinal: local_ordinal
            for local_ordinal, global_ordinal in enumerate(target.segment_ordinals)
        }
        translated_entries = tuple(
            _translate_entry_to_target(
                entry,
                target_ordinals=set(target.segment_ordinals),
                ordinal_map=ordinal_map,
                source_segments=segments,
            )
            for entry in candidate.entries
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
            logical_object_ids=(target.logical_object_id,),
            source_segment_ids=tuple(item.segment_id for item in scoped_segments),
            source_scope_sha256=d3_source_scope_sha256(scoped_segments),
        )
        local_candidate = D3ParentBoundaryClassificationCandidateV2(
            entries=translated_entries,
            unresolved_logical_object_ids=(
                (target.logical_object_id,)
                if candidate.scope_status is ParentProvisionObjectScopeStatus.UNRESOLVED
                else ()
            ),
        )
        compiled = compile_d3_parent_provision_decision(
            d2_identity=scoped_identity,
            source_segments=scoped_segments,
            kit_identity=kit,
            candidate=local_candidate,
        )
    coverage_sha256 = canonical_json_sha256(
        {
            "target_scope_sha256": target.target_scope_sha256,
            "scope_status": candidate.scope_status.value,
            "compiled_decision_sha256": (
                compiled.decision_sha256 if compiled is not None else None
            ),
        }
    )
    result_payload = {
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "batch_id": target.batch_id,
        "batch_order": target.batch_order,
        "logical_object_id": target.logical_object_id,
        "target_scope_sha256": target.target_scope_sha256,
        "model_candidate": candidate,
        "model_candidate_sha256": candidate.candidate_sha256,
        "compiled_decision": compiled,
        "target_coverage_sha256": coverage_sha256,
    }
    serializable = ParentProvisionObjectBatchCompilationV1.model_construct(
        result_sha256="0" * 64,
        **result_payload,
    ).model_dump(mode="json", exclude={"result_sha256"})
    return ParentProvisionObjectBatchCompilationV1(
        **result_payload,
        result_sha256=canonical_json_sha256(serializable),
    )


def require_parent_provision_object_evidence_recovery(
    *,
    plan: ParentProvisionObjectBatchPlanV1,
    expected_plan_sha256: str,
    logical_object_id: str,
) -> ParentProvisionObjectBatchPlanV1:
    """Replace one model target with a recovery target without changing evidence."""

    plan = _validated_plan(plan, expected_plan_sha256=expected_plan_sha256)
    targets: list[ParentProvisionObjectBatchTargetV1] = []
    found = False
    for target in plan.targets:
        if target.logical_object_id != logical_object_id:
            targets.append(target)
            continue
        found = True
        if target.execution_mode is not ParentProvisionObjectExecutionMode.MODEL_TARGET:
            raise ParentProvisionObjectBatchError(
                "OBJECT_BATCH_RECOVERY_TARGET_NOT_MODEL_READY"
            )
        scope_sha256 = _target_scope_sha256(
            batch_order=target.batch_order,
            logical_object_id=target.logical_object_id,
            stage_input_bundle_sha256=target.stage_input_bundle_sha256,
            global_source_scope_sha256=target.global_source_scope_sha256,
            target_evidence_sha256=target.target_evidence_sha256,
            execution_mode=ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED,
            stage_input_manifest_sha256=target.stage_input_manifest_sha256,
            runtime_adapter_sha256=target.runtime_adapter_sha256,
        )
        targets.append(
            ParentProvisionObjectBatchTargetV1(
                batch_id=f"parent-provision-object-batch-{scope_sha256[:32]}",
                batch_order=target.batch_order,
                logical_object_id=target.logical_object_id,
                object_kind=target.object_kind,
                parent_logical_object_id=target.parent_logical_object_id,
                stage_input_bundle_sha256=target.stage_input_bundle_sha256,
                stage_input_manifest_sha256=target.stage_input_manifest_sha256,
                runtime_adapter_sha256=target.runtime_adapter_sha256,
                global_source_scope_sha256=target.global_source_scope_sha256,
                segment_ordinals=target.segment_ordinals,
                segment_bindings=target.segment_bindings,
                target_evidence_sha256=target.target_evidence_sha256,
                execution_mode=ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED,
                target_scope_sha256=scope_sha256,
            )
        )
    if not found:
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_RECOVERY_TARGET_UNKNOWN")
    payload = plan.model_dump(mode="python", exclude={"plan_id", "plan_sha256"})
    payload["targets"] = tuple(targets)
    serializable = ParentProvisionObjectBatchPlanV1.model_construct(
        plan_id="parent-provision-object-plan-" + "0" * 32,
        plan_sha256="0" * 64,
        **payload,
    ).model_dump(mode="json", exclude={"plan_id", "plan_sha256"})
    plan_sha256 = canonical_json_sha256(serializable)
    return ParentProvisionObjectBatchPlanV1(
        plan_id=f"parent-provision-object-plan-{plan_sha256[:32]}",
        plan_sha256=plan_sha256,
        **payload,
    )


def aggregate_parent_provision_object_batches(
    *,
    plan: ParentProvisionObjectBatchPlanV1,
    expected_plan_sha256: str,
    results: tuple[ParentProvisionObjectBatchCompilationV1, ...],
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    kit_identity: AgentKitIdentityV1,
) -> ParentProvisionObjectBatchAggregateV1:
    """Aggregate exact object results, then rerun the existing global compiler."""

    plan = _validated_plan(plan, expected_plan_sha256=expected_plan_sha256)
    identity, segments, kit = _validate_runtime_context(
        plan=plan,
        d2_identity=d2_identity,
        source_segments=source_segments,
        kit_identity=kit_identity,
    )
    recovery_required = tuple(
        item
        for item in plan.targets
        if item.execution_mode is ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED
    )
    if recovery_required:
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_EVIDENCE_RECOVERY_REQUIRED")
    expected_targets = tuple(
        item
        for item in plan.targets
        if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
    )
    validated_results = tuple(
        ParentProvisionObjectBatchCompilationV1.model_validate(
            item.model_dump(mode="json")
        )
        for item in results
    )
    if len({item.batch_id for item in validated_results}) != len(validated_results):
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_DUPLICATE_RESULT")
    result_by_id = {item.batch_id: item for item in validated_results}
    if set(result_by_id) != {item.batch_id for item in expected_targets}:
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_RESULT_SET_INCOMPLETE")

    ordered_results: list[ParentProvisionObjectBatchCompilationV1] = []
    entries: list[D3ParentBoundaryClassificationEntryV2] = []
    unresolved_ids: set[str] = set()
    for target in expected_targets:
        result = result_by_id[target.batch_id]
        if (
            result.plan_id != plan.plan_id
            or result.plan_sha256 != plan.plan_sha256
            or result.batch_order != target.batch_order
            or result.logical_object_id != target.logical_object_id
            or result.target_scope_sha256 != target.target_scope_sha256
        ):
            raise ParentProvisionObjectBatchError("OBJECT_BATCH_RESULT_LINEAGE_DRIFT")
        recompiled = compile_parent_provision_object_candidate(
            plan=plan,
            expected_plan_sha256=expected_plan_sha256,
            batch_id=target.batch_id,
            d2_identity=identity,
            source_segments=segments,
            kit_identity=kit,
            candidate=result.model_candidate,
        )
        if recompiled != result:
            raise ParentProvisionObjectBatchError("OBJECT_BATCH_RESULT_REPLAY_DRIFT")
        if (
            result.model_candidate.scope_status
            is ParentProvisionObjectScopeStatus.ADJACENT_RECONCILIATION_REQUIRED
        ):
            raise ParentProvisionObjectBatchError(
                "OBJECT_BATCH_ADJACENT_RECONCILIATION_REQUIRED"
            )
        if result.compiled_decision is None:
            raise ParentProvisionObjectBatchError("OBJECT_BATCH_RESULT_NOT_COMPILED")
        if (
            result.model_candidate.scope_status
            is ParentProvisionObjectScopeStatus.UNRESOLVED
        ):
            unresolved_ids.add(target.logical_object_id)
        else:
            entries.extend(result.model_candidate.entries)
        ordered_results.append(result)

    global_candidate = D3ParentBoundaryClassificationCandidateV2(
        entries=tuple(sorted(entries, key=_candidate_entry_source_key)),
        unresolved_logical_object_ids=tuple(
            object_id
            for object_id in identity.logical_object_ids
            if object_id in unresolved_ids
        ),
    )
    decision = compile_d3_parent_provision_decision(
        d2_identity=identity,
        source_segments=segments,
        kit_identity=kit,
        candidate=global_candidate,
    )
    aggregate_payload = {
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "ordered_batch_result_sha256s": tuple(
            item.result_sha256 for item in ordered_results
        ),
        "global_candidate": global_candidate,
        "global_candidate_sha256": global_candidate.candidate_sha256,
        "compiled_decision": decision,
    }
    serializable = ParentProvisionObjectBatchAggregateV1.model_construct(
        aggregate_sha256="0" * 64,
        **aggregate_payload,
    ).model_dump(mode="json", exclude={"aggregate_sha256"})
    return ParentProvisionObjectBatchAggregateV1(
        **aggregate_payload,
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


def _validated_inputs(
    *,
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    logical_objects: tuple[D2LogicalObjectV1 | D2LogicalObjectV2, ...],
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
) -> tuple[
    D3ValidatedD2ArtifactIdentityV1,
    tuple[D2LogicalObjectV1 | D2LogicalObjectV2, ...],
    tuple[D3ExactSourceSegmentV1, ...],
]:
    identity = D3ValidatedD2ArtifactIdentityV1.model_validate(
        d2_identity.model_dump(mode="json")
    )
    objects: tuple[D2LogicalObjectV1 | D2LogicalObjectV2, ...] = tuple(
        (
            D2LogicalObjectV2.model_validate(item.model_dump(mode="json"))
            if isinstance(item, D2LogicalObjectV2)
            else D2LogicalObjectV1.model_validate(item.model_dump(mode="json"))
        )
        for item in logical_objects
    )
    segments = tuple(
        D3ExactSourceSegmentV1.model_validate(item.model_dump(mode="json"))
        for item in source_segments
    )
    if tuple(item.logical_object_id for item in objects) != identity.logical_object_ids:
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_LOGICAL_SCOPE_DRIFT")
    _validate_segment_scope(identity, segments)
    return identity, objects, segments


def _validate_runtime_binding_mode(
    *,
    objects: tuple[D2LogicalObjectV1 | D2LogicalObjectV2, ...],
    stage_input_manifest_sha256: str | None,
    runtime_adapter_sha256: str | None,
) -> None:
    if (stage_input_manifest_sha256 is None) != (runtime_adapter_sha256 is None):
        raise ParentProvisionObjectBatchError(
            "OBJECT_BATCH_REGION_RUNTIME_PINS_INCOMPLETE"
        )
    uses_v2 = tuple(isinstance(item, D2LogicalObjectV2) for item in objects)
    if any(uses_v2) and not all(uses_v2):
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_OBJECT_VERSION_MIXED")
    if all(uses_v2):
        if stage_input_manifest_sha256 is None:
            raise ParentProvisionObjectBatchError(
                "OBJECT_BATCH_REGION_RUNTIME_PINS_REQUIRED"
            )
    elif stage_input_manifest_sha256 is not None:
        raise ParentProvisionObjectBatchError(
            "OBJECT_BATCH_LEGACY_RUNTIME_PINS_FORBIDDEN"
        )


def _validate_runtime_context(
    *,
    plan: ParentProvisionObjectBatchPlanV1,
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    kit_identity: AgentKitIdentityV1,
) -> tuple[
    D3ValidatedD2ArtifactIdentityV1,
    tuple[D3ExactSourceSegmentV1, ...],
    AgentKitIdentityV1,
]:
    identity = D3ValidatedD2ArtifactIdentityV1.model_validate(
        d2_identity.model_dump(mode="json")
    )
    segments = tuple(
        D3ExactSourceSegmentV1.model_validate(item.model_dump(mode="json"))
        for item in source_segments
    )
    kit = AgentKitIdentityV1.model_validate(kit_identity.model_dump(mode="json"))
    _validate_segment_scope(identity, segments)
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
        or plan.segment_ordinal_ledger_sha256
        != _segment_ordinal_ledger_sha256(segments)
        or tuple(item.logical_object_id for item in plan.targets)
        != identity.logical_object_ids
        or any(
            target.stage_input_bundle_sha256 != plan.stage_input_bundle_sha256
            or target.global_source_scope_sha256 != plan.source_scope_sha256
            or target.segment_bindings
            != tuple(
                _segment_binding(ordinal, segments[ordinal])
                for ordinal in target.segment_ordinals
            )
            for target in plan.targets
        )
        or tuple(
            sorted(
                ordinal
                for target in plan.targets
                for ordinal in target.segment_ordinals
            )
        )
        != tuple(range(len(segments)))
        or any(
            segments[ordinal].logical_object_id != target.logical_object_id
            for target in plan.targets
            for ordinal in target.segment_ordinals
        )
    ):
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_RUNTIME_LINEAGE_DRIFT")
    return identity, segments, kit


def _validate_segment_scope(
    identity: D3ValidatedD2ArtifactIdentityV1,
    segments: tuple[D3ExactSourceSegmentV1, ...],
) -> None:
    if (
        tuple(item.segment_id for item in segments) != identity.source_segment_ids
        or any(
            item.logical_object_id not in identity.logical_object_ids
            for item in segments
        )
        or d3_source_scope_sha256(segments) != identity.source_scope_sha256
    ):
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_SOURCE_SCOPE_DRIFT")


def _validated_plan(
    plan: ParentProvisionObjectBatchPlanV1,
    *,
    expected_plan_sha256: str,
) -> ParentProvisionObjectBatchPlanV1:
    validated = ParentProvisionObjectBatchPlanV1.model_validate(
        plan.model_dump(mode="json")
    )
    if validated.plan_sha256 != expected_plan_sha256:
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_PLAN_PIN_MISMATCH")
    return validated


def _target_by_id(
    plan: ParentProvisionObjectBatchPlanV1,
    batch_id: str,
) -> ParentProvisionObjectBatchTargetV1:
    try:
        return next(item for item in plan.targets if item.batch_id == batch_id)
    except StopIteration as exc:
        raise ParentProvisionObjectBatchError("OBJECT_BATCH_TARGET_UNKNOWN") from exc


def _translate_entry_to_target(
    entry: D3ParentBoundaryClassificationEntryV2,
    *,
    target_ordinals: set[int],
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
            raise ParentProvisionObjectBatchError("OBJECT_BATCH_SPAN_INVALID")
        start_segment = source_segments[span.start_segment_ordinal]
        end_segment = source_segments[span.end_segment_ordinal]
        if span.start_offset > len(start_segment.source_text) or span.end_offset > len(
            end_segment.source_text
        ):
            raise ParentProvisionObjectBatchError("OBJECT_BATCH_SPAN_INVALID")
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
        if not selected or not selected.issubset(target_ordinals):
            raise ParentProvisionObjectBatchError("OBJECT_BATCH_SPAN_ESCAPES_TARGET")
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


def _target_scope_sha256(
    *,
    batch_order: int,
    logical_object_id: str,
    stage_input_bundle_sha256: str,
    global_source_scope_sha256: str,
    target_evidence_sha256: str,
    execution_mode: ParentProvisionObjectExecutionMode,
    stage_input_manifest_sha256: str | None = None,
    runtime_adapter_sha256: str | None = None,
) -> str:
    payload = {
        "batch_order": batch_order,
        "logical_object_id": logical_object_id,
        "stage_input_bundle_sha256": stage_input_bundle_sha256,
        "global_source_scope_sha256": global_source_scope_sha256,
        "target_evidence_sha256": target_evidence_sha256,
        "execution_mode": execution_mode.value,
    }
    if stage_input_manifest_sha256 is not None:
        payload["stage_input_manifest_sha256"] = stage_input_manifest_sha256
        payload["runtime_adapter_sha256"] = runtime_adapter_sha256
    return canonical_json_sha256(payload)


def _target_evidence_sha256(
    *,
    object_kind: D2LogicalObjectKind,
    parent_logical_object_id: str | None,
    segment_bindings: tuple[ParentProvisionObjectSegmentBindingV1, ...],
    runtime_adapter_sha256: str | None = None,
) -> str:
    payload = {
        "object_kind": object_kind.value,
        "parent_logical_object_id": parent_logical_object_id,
        "segment_bindings": [item.model_dump(mode="json") for item in segment_bindings],
    }
    if runtime_adapter_sha256 is not None:
        payload["runtime_adapter_sha256"] = runtime_adapter_sha256
    return canonical_json_sha256(payload)


def _segment_binding(
    ordinal: int,
    segment: D3ExactSourceSegmentV1,
) -> ParentProvisionObjectSegmentBindingV1:
    return ParentProvisionObjectSegmentBindingV1(
        segment_ordinal=ordinal,
        segment_id=segment.segment_id,
        source_text_length=len(segment.source_text),
        source_text_sha256=segment.source_text_sha256,
        d2_segment_sha256=segment.d2_segment_sha256,
    )


def _segment_ordinal_ledger_sha256(
    segments: tuple[D3ExactSourceSegmentV1, ...],
) -> str:
    return canonical_json_sha256(
        [
            {
                "segment_ordinal": ordinal,
                "segment_id": segment.segment_id,
                "logical_object_id": segment.logical_object_id,
                "source_order": segment.source_order,
                "page_numbers": segment.page_numbers,
                "source_text_length": len(segment.source_text),
                "source_text_sha256": segment.source_text_sha256,
                "d2_segment_sha256": segment.d2_segment_sha256,
            }
            for ordinal, segment in enumerate(segments)
        ]
    )


__all__ = [
    "PARENT_PROVISION_OBJECT_AGGREGATE_SCHEMA_VERSION",
    "PARENT_PROVISION_OBJECT_BATCH_PLAN_SCHEMA_VERSION",
    "PARENT_PROVISION_OBJECT_CANDIDATE_SCHEMA_VERSION",
    "PARENT_PROVISION_OBJECT_WINDOW_CANDIDATE_SCHEMA_VERSION",
    "PARENT_PROVISION_OBJECT_COMPILATION_SCHEMA_VERSION",
    "PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN",
    "ParentProvisionObjectBatchAggregateV1",
    "ParentProvisionObjectBatchCompilationV1",
    "ParentProvisionObjectBatchError",
    "ParentProvisionObjectBatchPlanV1",
    "ParentProvisionObjectBatchTargetV1",
    "ParentProvisionObjectCandidateV1",
    "ParentProvisionObjectWindowCandidateV1",
    "ParentProvisionObjectWindowItemV1",
    "ParentProvisionObjectExecutionMode",
    "ParentProvisionObjectScopeStatus",
    "ParentProvisionObjectSegmentBindingV1",
    "aggregate_parent_provision_object_batches",
    "build_parent_provision_object_batch_plan",
    "compile_parent_provision_object_candidate",
    "require_parent_provision_object_evidence_recovery",
]
