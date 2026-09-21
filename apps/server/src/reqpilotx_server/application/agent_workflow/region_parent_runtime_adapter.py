from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reqpilotx_server.application.agent_workflow.d2_logical_structure import (
    D2LogicalStructureStatus,
)
from reqpilotx_server.application.agent_workflow.d2_logical_structure_v2 import (
    D2LogicalObjectV2,
    D2LogicalStructureProposalV2,
    D2LogicalStructureValidationReceiptV2,
)
from reqpilotx_server.application.agent_workflow.d3_parent_provisions import (
    D3ExactSourceSegmentV1,
    D3ValidatedD2ArtifactIdentityV1,
    ParentProvisionStatus,
    canonical_json_sha256,
    d3_source_scope_sha256,
)
from reqpilotx_server.application.agent_workflow.d3_region_stage_input import (
    D3EvidenceLedgerEntryV1,
    D3EvidenceLedgerKindV1,
    D3RegionStageInputCompilationV1,
    D3RegionStageInputStatus,
    D3UnresolvedEvidenceScopeV1,
    D3UnifiedEvidenceKindV3,
    D3UnifiedExactSourceSegmentV3,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from reqpilotx_server.application.agent_workflow.parent_provision_object_batches import (
    PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN,
)


REGION_PARENT_RUNTIME_ADAPTER_SCHEMA_VERSION = "region-parent-runtime-adapter/v1"
REGION_PARENT_SELECTED_PARENT_ASSIGNMENT_SCHEMA_VERSION = (
    "region-parent-selected-parent-assignment/v1"
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RegionParentRuntimeAdapterError(ValueError):
    """Fail-closed boundary from region stage input to object recognition."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class RegionParentSelectedParentAssignmentV1(_StrictFrozenModel):
    """Owned nested evidence eligible for a later selected-parent assignment."""

    schema_version: Literal["region-parent-selected-parent-assignment/v1"] = (
        REGION_PARENT_SELECTED_PARENT_ASSIGNMENT_SCHEMA_VERSION
    )
    evidence_id: str = Field(min_length=1, max_length=240)
    region_id: str = Field(pattern=r"^d2-table-region-[0-9a-f]{32}$")
    ultimate_owner_logical_object_id: str = Field(
        pattern=PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN
    )
    owner_segment_ids: tuple[str, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...]
    source_evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    assignment_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_assignment(self) -> RegionParentSelectedParentAssignmentV1:
        for values in (self.owner_segment_ids, self.evidence_refs):
            if len(values) != len(set(values)) or any(
                not item.strip() for item in values
            ):
                raise ValueError("selected-parent ledgers require unique evidence")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"assignment_sha256"})
        )
        if self.assignment_sha256 != expected:
            raise ValueError("selected-parent assignment SHA drifted")
        return self


class RegionParentRuntimeAdapterV1(_StrictFrozenModel):
    """Content-bearing, in-memory input for object parent recognition."""

    schema_version: Literal["region-parent-runtime-adapter/v1"] = (
        REGION_PARENT_RUNTIME_ADAPTER_SCHEMA_VERSION
    )
    content_bearing: Literal[True] = True
    private_stage_input_only: Literal[True] = True
    stage_input_manifest_id: str = Field(
        pattern=r"^d3-region-stage-input-[0-9a-f]{32}$"
    )
    stage_input_manifest_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    workspace_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    stage_input_status: D3RegionStageInputStatus
    d2_identity: D3ValidatedD2ArtifactIdentityV1
    logical_objects: tuple[D2LogicalObjectV2, ...] = Field(min_length=1)
    source_segments: tuple[D3ExactSourceSegmentV1, ...] = Field(min_length=1)
    evidence_ledger: tuple[D3EvidenceLedgerEntryV1, ...] = ()
    unresolved_scopes: tuple[D3UnresolvedEvidenceScopeV1, ...] = ()
    nested_evidence_ledger: tuple[D3EvidenceLedgerEntryV1, ...] = ()
    selected_parent_assignment_ledger: tuple[
        RegionParentSelectedParentAssignmentV1, ...
    ] = ()
    adapter_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_adapter(self) -> RegionParentRuntimeAdapterV1:
        object_ids = tuple(item.logical_object_id for item in self.logical_objects)
        segment_ids = tuple(item.segment_id for item in self.source_segments)
        if object_ids != self.d2_identity.logical_object_ids:
            raise ValueError("runtime objects differ from the D2 identity")
        if segment_ids != self.d2_identity.source_segment_ids:
            raise ValueError("runtime segments differ from the D2 identity")
        if any(
            item.logical_object_id not in set(object_ids)
            for item in self.source_segments
        ):
            raise ValueError("runtime segment has no logical-object owner")
        if d3_source_scope_sha256(self.source_segments) != (
            self.d2_identity.source_scope_sha256
        ):
            raise ValueError("runtime source scope SHA drifted")

        expected_nested = tuple(
            item
            for item in self.evidence_ledger
            if item.evidence_kind is D3EvidenceLedgerKindV1.NESTED_REGION
        )
        if self.nested_evidence_ledger != expected_nested:
            raise ValueError("runtime nested evidence ledger is incomplete")
        expected_stage_status = (
            D3RegionStageInputStatus.PARTIAL
            if self.unresolved_scopes
            else D3RegionStageInputStatus.COMPLETE
        )
        if self.stage_input_status is not expected_stage_status:
            raise ValueError("runtime stage status hides unresolved evidence")
        if (
            self.stage_input_status is D3RegionStageInputStatus.PARTIAL
            and self.d2_identity.status is not ParentProvisionStatus.PARTIAL
        ):
            raise ValueError("runtime partial stage did not reach D2 identity")
        eligible = tuple(
            item
            for item in self.nested_evidence_ledger
            if item.selected_parent_assignment_eligible
        )
        if tuple(item.evidence_id for item in eligible) != tuple(
            item.evidence_id for item in self.selected_parent_assignment_ledger
        ):
            raise ValueError("selected-parent eligibility ledger drifted")
        assignment_by_evidence = {
            item.evidence_id: item for item in self.selected_parent_assignment_ledger
        }
        for evidence in eligible:
            assignment = assignment_by_evidence[evidence.evidence_id]
            if (
                assignment.region_id != evidence.region_id
                or assignment.ultimate_owner_logical_object_id
                != evidence.ultimate_owner_logical_object_id
                or assignment.owner_segment_ids != evidence.owner_segment_ids
                or assignment.evidence_refs != tuple(sorted(evidence.evidence_refs))
                or assignment.source_evidence_sha256 != evidence.source_evidence_sha256
            ):
                raise ValueError("selected-parent evidence binding drifted")

        segment_by_id = {item.segment_id: item for item in self.source_segments}
        for evidence in self.evidence_ledger:
            if evidence.ultimate_owner_logical_object_id is None:
                continue
            if evidence.ultimate_owner_logical_object_id not in set(object_ids):
                raise ValueError("owned evidence references an unknown object")
            if any(
                segment_id not in segment_by_id
                or segment_by_id[segment_id].logical_object_id
                != evidence.ultimate_owner_logical_object_id
                for segment_id in evidence.owner_segment_ids
            ):
                raise ValueError("owned evidence lost its exact owner segments")
        exact_ids = set(segment_ids)
        if any(item.evidence_id in exact_ids for item in self.evidence_ledger):
            raise ValueError("evidence ledgers cannot enter exact-cover as segments")

        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"adapter_sha256"})
        )
        if self.adapter_sha256 != expected:
            raise ValueError("region runtime adapter SHA drifted")
        return self


def adapt_region_parent_runtime(
    *,
    proposal: D2LogicalStructureProposalV2,
    validation_receipt: D2LogicalStructureValidationReceiptV2,
    stage_input: D3RegionStageInputCompilationV1,
    expected_kit_identity: AgentKitIdentityV1,
    expected_source_sha256: str,
    expected_source_ir_canonical_sha256: str,
    expected_stage_input_manifest_sha256: str,
    expected_workspace_bundle_sha256: str,
) -> RegionParentRuntimeAdapterV1:
    """Adapt a validated region-aware input to the existing object runtime."""

    try:
        v2 = D2LogicalStructureProposalV2.model_validate(
            proposal.model_dump(mode="json")
        )
        receipt = D2LogicalStructureValidationReceiptV2.model_validate(
            validation_receipt.model_dump(mode="json")
        )
        region_input = D3RegionStageInputCompilationV1.model_validate(
            stage_input.model_dump(mode="json")
        )
        kit = AgentKitIdentityV1.model_validate(
            expected_kit_identity.model_dump(mode="json")
        )
    except (TypeError, ValueError) as exc:
        raise RegionParentRuntimeAdapterError(
            "REGION_PARENT_RUNTIME_PREDECESSOR_INVALID"
        ) from exc
    _require_sha256(expected_source_sha256, "expected source")
    _require_sha256(
        expected_source_ir_canonical_sha256,
        "expected SourceIR canonical",
    )
    _require_sha256(
        expected_stage_input_manifest_sha256,
        "expected stage-input manifest",
    )
    _require_sha256(expected_workspace_bundle_sha256, "expected workspace bundle")
    _validate_runtime_lineage(
        proposal=v2,
        validation_receipt=receipt,
        stage_input=region_input,
        expected_kit_identity=kit,
        expected_source_sha256=expected_source_sha256,
        expected_source_ir_canonical_sha256=expected_source_ir_canonical_sha256,
        expected_stage_input_manifest_sha256=(expected_stage_input_manifest_sha256),
    )

    objects_by_id = {item.logical_object_id: item for item in v2.logical_objects}
    exact_segments = tuple(
        _exact_runtime_segment(item, objects_by_id=objects_by_id)
        for item in region_input.segment_set.segments
    )
    if not exact_segments:
        raise RegionParentRuntimeAdapterError("REGION_PARENT_RUNTIME_NO_EXACT_SEGMENTS")
    identity_status = (
        ParentProvisionStatus.PARTIAL
        if v2.status is D2LogicalStructureStatus.PARTIAL
        or region_input.segment_set.status is D3RegionStageInputStatus.PARTIAL
        else ParentProvisionStatus.COMPLETE
    )
    identity = D3ValidatedD2ArtifactIdentityV1(
        artifact_id=receipt.artifact_id,
        artifact_sha256=receipt.artifact_sha256,
        validator_receipt_sha256=receipt.receipt_sha256,
        run_id=receipt.run_id,
        source_sha256=receipt.source_sha256,
        source_ir_id=receipt.source_ir_id,
        source_ir_canonical_sha256=receipt.source_ir_canonical_sha256,
        kit_identity=kit,
        status=identity_status,
        logical_object_ids=tuple(item.logical_object_id for item in v2.logical_objects),
        source_segment_ids=tuple(item.segment_id for item in exact_segments),
        source_scope_sha256=d3_source_scope_sha256(exact_segments),
    )
    evidence_ledger = region_input.segment_set.evidence_ledger
    nested = tuple(
        item
        for item in evidence_ledger
        if item.evidence_kind is D3EvidenceLedgerKindV1.NESTED_REGION
    )
    assignments = tuple(
        _selected_parent_assignment(item)
        for item in nested
        if item.selected_parent_assignment_eligible
    )
    values = {
        "stage_input_manifest_id": region_input.manifest.manifest_id,
        "stage_input_manifest_sha256": region_input.manifest.manifest_sha256,
        "workspace_bundle_sha256": expected_workspace_bundle_sha256,
        "stage_input_status": region_input.segment_set.status,
        "d2_identity": identity,
        "logical_objects": v2.logical_objects,
        "source_segments": exact_segments,
        "evidence_ledger": evidence_ledger,
        "unresolved_scopes": region_input.segment_set.unresolved_scopes,
        "nested_evidence_ledger": nested,
        "selected_parent_assignment_ledger": assignments,
    }
    draft = RegionParentRuntimeAdapterV1.model_construct(
        **values,
        adapter_sha256="0" * 64,
    )
    return RegionParentRuntimeAdapterV1(
        **values,
        adapter_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"adapter_sha256"})
        ),
    )


def _validate_runtime_lineage(
    *,
    proposal: D2LogicalStructureProposalV2,
    validation_receipt: D2LogicalStructureValidationReceiptV2,
    stage_input: D3RegionStageInputCompilationV1,
    expected_kit_identity: AgentKitIdentityV1,
    expected_source_sha256: str,
    expected_source_ir_canonical_sha256: str,
    expected_stage_input_manifest_sha256: str,
) -> None:
    manifest = stage_input.manifest
    segment_set = stage_input.segment_set
    lineage = manifest.lineage
    proposal_object_ids = tuple(
        item.logical_object_id for item in proposal.logical_objects
    )
    proposal_segment_ids = tuple(
        segment.segment_id
        for item in proposal.logical_objects
        for segment in item.source_segments
    )
    if (
        proposal.kit_identity != expected_kit_identity
        or validation_receipt.kit_identity != expected_kit_identity
        or proposal.source_sha256 != expected_source_sha256
        or validation_receipt.source_sha256 != expected_source_sha256
        or proposal.source_ir_canonical_sha256 != expected_source_ir_canonical_sha256
        or validation_receipt.source_ir_canonical_sha256
        != expected_source_ir_canonical_sha256
    ):
        raise RegionParentRuntimeAdapterError(
            "REGION_PARENT_RUNTIME_SOURCE_OR_KIT_DRIFT"
        )
    if (
        validation_receipt.run_id != proposal.run_id
        or validation_receipt.source_ir_id != proposal.source_ir_id
        or validation_receipt.artifact_sha256 != proposal.proposal_sha256
        or validation_receipt.proposal_sha256 != proposal.proposal_sha256
        or validation_receipt.status.value != proposal.status.value
        or validation_receipt.logical_object_ids != proposal_object_ids
        or validation_receipt.source_segment_ids != proposal_segment_ids
    ):
        raise RegionParentRuntimeAdapterError("REGION_PARENT_RUNTIME_V2_RECEIPT_DRIFT")
    if manifest.manifest_sha256 != expected_stage_input_manifest_sha256:
        raise RegionParentRuntimeAdapterError(
            "REGION_PARENT_RUNTIME_MANIFEST_PIN_DRIFT"
        )
    if (
        lineage.run_id != proposal.run_id
        or lineage.source_ir_id != proposal.source_ir_id
        or lineage.source_sha256 != proposal.source_sha256
        or lineage.source_ir_canonical_sha256 != proposal.source_ir_canonical_sha256
        or lineage.kit_bundle_sha256 != expected_kit_identity.bundle_sha256
        or lineage.v2_artifact_id != validation_receipt.artifact_id
        or lineage.v2_artifact_sha256 != validation_receipt.artifact_sha256
        or lineage.v2_validation_receipt_sha256 != validation_receipt.receipt_sha256
        or segment_set.run_id != proposal.run_id
        or segment_set.source_ir_id != proposal.source_ir_id
        or segment_set.source_sha256 != proposal.source_sha256
        or segment_set.source_ir_canonical_sha256 != proposal.source_ir_canonical_sha256
        or segment_set.kit_bundle_sha256 != expected_kit_identity.bundle_sha256
        or segment_set.predecessor_v2_artifact_id != validation_receipt.artifact_id
        or segment_set.predecessor_v2_artifact_sha256
        != validation_receipt.artifact_sha256
        or manifest.ordered_logical_object_ids != proposal_object_ids
    ):
        raise RegionParentRuntimeAdapterError(
            "REGION_PARENT_RUNTIME_STAGE_LINEAGE_DRIFT"
        )
    scopes_by_id = {
        item.logical_object_id: item for item in segment_set.logical_objects
    }
    if set(scopes_by_id) != set(proposal_object_ids):
        raise RegionParentRuntimeAdapterError(
            "REGION_PARENT_RUNTIME_OBJECT_SCOPE_DRIFT"
        )
    for item in proposal.logical_objects:
        scope = scopes_by_id[item.logical_object_id]
        if (
            scope.parent_logical_object_id != item.parent_logical_object_id
            or scope.object_kind != item.object_kind.value
            or scope.object_sha256 != item.object_sha256
            or scope.source_order != item.source_order
        ):
            raise RegionParentRuntimeAdapterError(
                "REGION_PARENT_RUNTIME_OBJECT_SCOPE_DRIFT"
            )
    if (
        proposal.status is D2LogicalStructureStatus.PARTIAL
        and segment_set.status is not D3RegionStageInputStatus.PARTIAL
    ):
        raise RegionParentRuntimeAdapterError(
            "REGION_PARENT_RUNTIME_PARTIAL_STATUS_DRIFT"
        )
    if any(
        re.fullmatch(PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN, object_id) is None
        for object_id in proposal_object_ids
    ):
        raise RegionParentRuntimeAdapterError(
            "REGION_PARENT_RUNTIME_OBJECT_ID_UNSUPPORTED"
        )


def _exact_runtime_segment(
    item: D3UnifiedExactSourceSegmentV3,
    *,
    objects_by_id: dict[str, D2LogicalObjectV2],
) -> D3ExactSourceSegmentV1:
    owner = objects_by_id.get(item.logical_object_id)
    if owner is None:
        raise RegionParentRuntimeAdapterError(
            "REGION_PARENT_RUNTIME_SEGMENT_OWNER_MISSING"
        )
    if item.evidence_kind is D3UnifiedEvidenceKindV3.BASE_TEXT:
        handle_kind = "SOURCE_NODE"
        source_handle = item.source_node_id
    elif item.evidence_kind is D3UnifiedEvidenceKindV3.TABLE_CELL:
        handle_kind = "TABLE_CELL"
        source_handle = f"{item.source_node_id}:cell-ordinal:{item.intra_node_ordinal}"
    elif item.evidence_kind is D3UnifiedEvidenceKindV3.TABLE_NODE_RAW_TEXT:
        handle_kind = "TABLE_NODE_RAW_TEXT"
        source_handle = item.supplemental_evidence_id or item.segment_id
    else:
        handle_kind = "TABLE_NOTE"
        source_handle = item.supplemental_evidence_id or item.segment_id
    if len(item.segment_id) > 200 or len(source_handle) > 240:
        raise RegionParentRuntimeAdapterError(
            "REGION_PARENT_RUNTIME_SEGMENT_ID_UNSUPPORTED"
        )
    pages = item.page_numbers or owner.page_numbers
    if not pages:
        raise RegionParentRuntimeAdapterError(
            "REGION_PARENT_RUNTIME_SEGMENT_PAGE_MISSING"
        )
    if item.source_text:
        start_offset = item.start_offset
        end_offset = item.end_offset
    else:
        start_offset = None
        end_offset = None
    evidence_refs = tuple(
        sorted(
            {
                *item.evidence_refs,
                source_handle,
                *(
                    (item.d2_v2_source_segment_id,)
                    if item.d2_v2_source_segment_id is not None
                    else ()
                ),
                *(
                    (item.region_parent_segment_id,)
                    if item.region_parent_segment_id is not None
                    else ()
                ),
                *(
                    (item.supplemental_evidence_id,)
                    if item.supplemental_evidence_id is not None
                    else ()
                ),
            }
        )
    )
    return D3ExactSourceSegmentV1(
        segment_id=item.segment_id,
        logical_object_id=item.logical_object_id,
        source_handle=source_handle,
        handle_kind=handle_kind,
        owner_source_node_id=item.source_node_id,
        source_order=item.source_node_order,
        page_numbers=pages,
        handle_start_offset=start_offset,
        handle_end_offset=end_offset,
        source_text=item.source_text,
        source_text_sha256=item.source_text_sha256,
        source_content_sha256=item.source_content_sha256,
        d2_segment_sha256=item.segment_sha256,
        evidence_refs=evidence_refs,
    )


def _selected_parent_assignment(
    item: D3EvidenceLedgerEntryV1,
) -> RegionParentSelectedParentAssignmentV1:
    if (
        item.evidence_kind is not D3EvidenceLedgerKindV1.NESTED_REGION
        or not item.selected_parent_assignment_eligible
        or item.region_id is None
        or item.ultimate_owner_logical_object_id is None
    ):
        raise RegionParentRuntimeAdapterError(
            "REGION_PARENT_RUNTIME_SELECTED_PARENT_DRIFT"
        )
    values = {
        "evidence_id": item.evidence_id,
        "region_id": item.region_id,
        "ultimate_owner_logical_object_id": (item.ultimate_owner_logical_object_id),
        "owner_segment_ids": item.owner_segment_ids,
        "evidence_refs": tuple(sorted(item.evidence_refs)),
        "source_evidence_sha256": item.source_evidence_sha256,
    }
    draft = RegionParentSelectedParentAssignmentV1.model_construct(
        **values,
        assignment_sha256="0" * 64,
    )
    return RegionParentSelectedParentAssignmentV1(
        **values,
        assignment_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"assignment_sha256"})
        ),
    )


def _require_sha256(value: str, subject: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise RegionParentRuntimeAdapterError(
            f"REGION_PARENT_RUNTIME_{subject.upper().replace(' ', '_')}_SHA_INVALID"
        )


__all__ = [
    "REGION_PARENT_RUNTIME_ADAPTER_SCHEMA_VERSION",
    "REGION_PARENT_SELECTED_PARENT_ASSIGNMENT_SCHEMA_VERSION",
    "RegionParentRuntimeAdapterError",
    "RegionParentRuntimeAdapterV1",
    "RegionParentSelectedParentAssignmentV1",
    "adapt_region_parent_runtime",
]
