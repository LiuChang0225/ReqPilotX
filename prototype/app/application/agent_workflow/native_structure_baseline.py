"""Compile one verified native structure into the canonical D3 region input.

The native tree remains the boundary authority.  SourceIR is used only as the
Host evidence ledger: exact text ranges, table cells, image placements and
locators are rebound to the Agent-selected hierarchy and row regions before the
existing D2/V2 and D3 validators are run.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalObjectV1,
    D2LogicalStructureProposalV1,
    D2LogicalStructureStatus,
    D2LogicalStructureValidationReceiptV1,
    D2PhysicalTableDispositionKind,
    D2PhysicalTableDispositionV1,
    D2SourceEvidenceDispositionKind,
    D2SourceEvidenceDispositionV1,
    D2SourceHandleKind,
    D2SourceSegmentV1,
    D2ValidatedD1ArtifactIdentityV1,
    stable_d2_derived_container_id,
    stable_d2_logical_object_id,
    stable_d2_logical_object_sha256,
    stable_d2_logical_structure_proposal_sha256,
    stable_d2_source_disposition_id,
    stable_d2_source_disposition_sha256,
    stable_d2_source_segment_id,
    stable_d2_source_segment_sha256,
    stable_d2_table_disposition_sha256,
    validate_d2_logical_structure_proposal,
)
from app.application.agent_workflow.d2_logical_structure_v2 import (
    D2BaseTableReplacementCandidateV1,
    D2BaseTableReplacementDisposition,
    D2LogicalStructureCompilationV2,
    D2LogicalStructureProposalV2,
    D2LogicalStructureValidationReceiptV2,
    D2RegionLogicalObjectPlacementMode,
    D2RegionLogicalObjectPlacementV1,
    D2RegionLogicalScopePlacementV1,
    D2RetainedBaseObjectPlacementV1,
    D2SupplementalObjectOwnerReferenceKindV1,
    D2TableSupplementalEvidencePlacementV1,
    build_d2_region_placement_plan,
    build_d2_table_supplemental_object_owner_placements,
    compile_d2_logical_structure_v2,
)
from app.application.agent_workflow.d2_table_region_parent_input import (
    D2TableRegionParentInputCompilationV1,
    D2TableRegionParentInputReceiptV1,
    D2TableRegionParentInputV1,
    build_d2_table_region_parent_input,
)
from app.application.agent_workflow.d2_table_regions import (
    D2EmptyPhysicalTableDispositionCandidateV1,
    D2EmptyPhysicalTableDispositionKind,
    D2LogicalObjectPartTableRegionCandidateV1,
    D2LogicalScopePartTableRegionCandidateV1,
    D2NestedValueTableRegionCandidateV1,
    D2NonBusinessFalseTableRegionCandidateV1,
    D2PhysicalTableCellOrdinalSpanV1,
    D2TableLogicalScopeCandidateV1,
    D2TableRegionArtifactV1,
    D2TableRegionCandidateArtifactV1,
    D2TableRegionDispositionKind,
    D2TableRegionGapPolicy,
    compile_d2_table_regions,
    d2_table_region_source_ir_canonical_sha256,
)
from app.application.agent_workflow.d3_parent_provisions import (
    canonical_json_sha256,
)
from app.application.agent_workflow.d3_region_stage_input import (
    D3RegionStageInputCompilationV1,
    D3RegionStageInputLineageV1,
    build_d3_region_stage_input_lineage,
    materialize_d3_region_stage_input,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentKitIdentityV1,
    AgentStageArtifactStatus,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    AgentLogicalTableScopeV1,
    build_agent_logical_table_scope,
    source_ir_canonical_sha256,
)
from app.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
)
from app.domain.extraction.structure_revision import canonical_model_sha256


NATIVE_STRUCTURE_ACCEPTED_BASELINE_SCHEMA_VERSION = (
    "native-structure-accepted-baseline/v1"
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,159}$")


class NativeStructureBaselineError(ValueError):
    """Fail-closed error raised by the native-structure/D3 bridge."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class NativeStructureAcceptedBaselineV1(_StrictFrozenModel):
    """Content-free accepted identity for one native structure projection."""

    schema_version: Literal["native-structure-accepted-baseline/v1"] = (
        NATIVE_STRUCTURE_ACCEPTED_BASELINE_SCHEMA_VERSION
    )
    baseline_id: str = Field(pattern=r"^native-structure-baseline-[0-9a-f]{32}$")
    status: Literal["ACCEPTED"] = "ACCEPTED"
    project_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    document_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_artifact_id: str = Field(min_length=1, max_length=200)
    native_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=200)
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    accepted_structure_artifact_id: str = Field(
        pattern=r"^agent-artifact-[0-9a-f]{32}$"
    )
    accepted_structure_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_structure_validator_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d3_stage_input_manifest_id: str = Field(
        pattern=r"^d3-region-stage-input-[0-9a-f]{32}$"
    )
    d3_stage_input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d3_source_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_state: Literal["SUFFICIENT", "CONFLICTING", "INSUFFICIENT"]
    decision_policy: Literal[
        "FIRST_HOST_VALIDATED_PROPOSAL",
        "KEEP_CURRENT_STRUCTURE",
        "NONE",
    ]
    logical_object_count: StrictInt = Field(ge=1)
    exact_source_segment_count: StrictInt = Field(ge=1)
    physical_table_count: StrictInt = Field(ge=0)
    table_region_count: StrictInt = Field(ge=0)
    baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> NativeStructureAcceptedBaselineV1:
        payload = self.model_dump(
            mode="json",
            exclude={"baseline_id", "baseline_sha256"},
        )
        expected = canonical_json_sha256(payload)
        if self.baseline_sha256 != expected:
            raise ValueError("native accepted baseline SHA drifted")
        if self.baseline_id != f"native-structure-baseline-{expected[:32]}":
            raise ValueError("native accepted baseline ID drifted")
        return self


class NativeStructureBaselineArtifactV1(_StrictFrozenModel):
    """Private content-bearing replay artifact; never placed in job JSON."""

    schema_version: Literal["native-structure-baseline-artifact/v1"] = (
        "native-structure-baseline-artifact/v1"
    )
    content_bearing: Literal[True] = True
    private_object_store_only: Literal[True] = True
    identity: NativeStructureAcceptedBaselineV1
    source_ir: SourceIRSnapshot
    d2_proposal: D2LogicalStructureProposalV2
    d2_validation_receipt: D2LogicalStructureValidationReceiptV2
    parent_input: D2TableRegionParentInputV1
    parent_input_receipt: D2TableRegionParentInputReceiptV1
    expected_lineage: D3RegionStageInputLineageV1
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_artifact(self) -> NativeStructureBaselineArtifactV1:
        identity = self.identity
        if (
            self.source_ir.source_sha256 != identity.source_sha256
            or self.source_ir.source_ir_id != identity.source_ir_id
            or source_ir_canonical_sha256(self.source_ir)
            != identity.source_ir_canonical_sha256
            or self.d2_proposal.source_sha256 != identity.source_sha256
            or self.d2_proposal.source_ir_id != identity.source_ir_id
            or self.d2_proposal.kit_identity != identity.kit_identity
            or self.d2_validation_receipt.artifact_id
            != identity.accepted_structure_artifact_id
            or self.d2_validation_receipt.artifact_sha256
            != identity.accepted_structure_artifact_sha256
            or self.d2_validation_receipt.receipt_sha256
            != identity.accepted_structure_validator_receipt_sha256
            or self.expected_lineage.v2_artifact_id
            != identity.accepted_structure_artifact_id
            or self.expected_lineage.v2_artifact_sha256
            != identity.accepted_structure_artifact_sha256
            or self.expected_lineage.v2_validation_receipt_sha256
            != identity.accepted_structure_validator_receipt_sha256
        ):
            raise ValueError("native baseline artifact lineage drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"artifact_sha256"})
        )
        if self.artifact_sha256 != expected:
            raise ValueError("native baseline private artifact SHA drifted")
        return self


@dataclass(frozen=True, slots=True)
class NativeStructureBaselineCompilationV1:
    identity: NativeStructureAcceptedBaselineV1
    source_ir: SourceIRSnapshot
    base_proposal: D2LogicalStructureProposalV1
    base_validation_receipt: D2LogicalStructureValidationReceiptV1
    table_region_artifact: D2TableRegionArtifactV1
    v2_compilation: D2LogicalStructureCompilationV2
    parent_input: D2TableRegionParentInputCompilationV1
    d3_stage_input_lineage: D3RegionStageInputLineageV1
    d3_stage_input: D3RegionStageInputCompilationV1


def build_native_structure_baseline_artifact(
    compilation: NativeStructureBaselineCompilationV1,
) -> NativeStructureBaselineArtifactV1:
    values = {
        "content_bearing": True,
        "private_object_store_only": True,
        "identity": compilation.identity,
        "source_ir": compilation.source_ir,
        "d2_proposal": compilation.v2_compilation.proposal,
        "d2_validation_receipt": compilation.v2_compilation.validation_receipt,
        "parent_input": compilation.parent_input.stage_input,
        "parent_input_receipt": compilation.parent_input.receipt,
        "expected_lineage": compilation.d3_stage_input_lineage,
    }
    draft = NativeStructureBaselineArtifactV1.model_construct(
        **values,
        artifact_sha256="0" * 64,
    )
    return NativeStructureBaselineArtifactV1(
        **values,
        artifact_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"artifact_sha256"})
        ),
    )


def native_structure_baseline_artifact_bytes(
    artifact: NativeStructureBaselineArtifactV1,
) -> bytes:
    validated = NativeStructureBaselineArtifactV1.model_validate(
        artifact.model_dump(mode="json")
    )
    return json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def native_structure_baseline_object_key(
    identity: NativeStructureAcceptedBaselineV1,
) -> str:
    validated = NativeStructureAcceptedBaselineV1.model_validate(
        identity.model_dump(mode="json")
    )
    return (
        "runtime/agent-native-baselines/"
        f"{validated.project_id}/{validated.document_id}/"
        f"{validated.source_sha256}/{validated.baseline_sha256}.json"
    )


@dataclass(frozen=True, slots=True)
class _AllocatedRange:
    native_node_id: str
    source_node_id: str
    start_offset: int | None
    end_offset: int | None
    source_order: int
    page_numbers: tuple[int, ...]


def compile_native_structure_accepted_baseline(
    *,
    project_id: str,
    document_id: str,
    source_ir: SourceIRSnapshot,
    public_reference: dict[str, Any],
    table_previews: tuple[dict[str, Any], ...],
    kit_identity: AgentKitIdentityV1,
) -> NativeStructureBaselineCompilationV1:
    """Rebind the current native projection to validated V2/D3 contracts."""

    source = SourceIRSnapshot.model_validate(source_ir.model_dump(mode="json"))
    kit = AgentKitIdentityV1.model_validate(kit_identity.model_dump(mode="json"))
    reference = _validated_public_reference(public_reference, source=source)
    tree = reference["document_tree"]
    projection_sha256 = native_structure_projection_sha256(
        public_reference=reference,
        table_previews=table_previews,
    )
    run_id = _native_run_id(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source.source_sha256,
        native_artifact_id=str(reference["artifact_id"]),
        native_projection_sha256=projection_sha256,
        kit_bundle_sha256=kit.bundle_sha256,
    )
    d1_identity, table_map_identity = _host_predecessor_identities(
        source=source,
        reference=reference,
        run_id=run_id,
        native_projection_sha256=projection_sha256,
        kit=kit,
    )
    allocated = _allocate_native_source_ranges(source=source, tree=tree)
    base_proposal, base_receipt, native_object_ids = _compile_base_v1(
        source=source,
        tree=tree,
        allocations=allocated,
        run_id=run_id,
        d1_identity=d1_identity,
        table_map_identity=table_map_identity,
        kit=kit,
    )
    table_token_map = _map_native_table_tokens(
        source=source,
        reference=reference,
        table_previews=table_previews,
    )
    region_artifact, object_region_keys = _compile_native_table_regions(
        source=source,
        reference=reference,
        table_token_map=table_token_map,
        table_map_identity=table_map_identity,
        kit=kit,
        native_object_ids=native_object_ids,
    )
    placement_plan = _build_region_placement_plan(
        source=source,
        tree=tree,
        reference=reference,
        base_proposal=base_proposal,
        base_receipt=base_receipt,
        region_artifact=region_artifact,
        native_object_ids=native_object_ids,
        object_region_keys=object_region_keys,
    )
    v2 = compile_d2_logical_structure_v2(
        source,
        base_proposal=base_proposal,
        base_validation_receipt=base_receipt,
        table_region_artifact=region_artifact,
        placement_plan=placement_plan,
        expected_kit_identity=kit,
    )
    parent_input = build_d2_table_region_parent_input(
        source,
        region_artifact,
        expected_kit_bundle_sha256=kit.bundle_sha256,
        expected_d1_table_map_artifact_id=table_map_identity.artifact_id,
        expected_d1_table_map_artifact_sha256=table_map_identity.artifact_sha256,
        expected_region_artifact_id=region_artifact.artifact_id,
        expected_region_artifact_sha256=region_artifact.artifact_sha256,
    )
    lineage = build_d3_region_stage_input_lineage(
        proposal=v2.proposal,
        validation_receipt=v2.validation_receipt,
        parent_input=parent_input.stage_input,
        parent_input_receipt=parent_input.receipt,
    )
    d3_stage_input = materialize_d3_region_stage_input(
        source,
        proposal=v2.proposal,
        validation_receipt=v2.validation_receipt,
        parent_input=parent_input.stage_input,
        parent_input_receipt=parent_input.receipt,
        expected_lineage=lineage,
    )
    identity = _accepted_identity(
        project_id=project_id,
        document_id=document_id,
        reference=reference,
        native_projection_sha256=projection_sha256,
        source=source,
        kit=kit,
        v2=v2,
        region_artifact=region_artifact,
        d3_stage_input=d3_stage_input,
    )
    return NativeStructureBaselineCompilationV1(
        identity=identity,
        source_ir=source,
        base_proposal=base_proposal,
        base_validation_receipt=base_receipt,
        table_region_artifact=region_artifact,
        v2_compilation=v2,
        parent_input=parent_input,
        d3_stage_input_lineage=lineage,
        d3_stage_input=d3_stage_input,
    )


def _validated_public_reference(
    value: dict[str, Any],
    *,
    source: SourceIRSnapshot,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NativeStructureBaselineError("NATIVE_BASELINE_REFERENCE_INVALID")
    reference = json.loads(json.dumps(value, ensure_ascii=False))
    continuation = reference.get("continuation")
    validation = reference.get("validation")
    tree = reference.get("document_tree")
    if (
        reference.get("schema_version") != "native-structure-reference-public/v1"
        or reference.get("availability") != "READY"
        or reference.get("source_sha256") != source.source_sha256
        or reference.get("workflow_step") != "DOCUMENT_STRUCTURE_RECOVERY"
        or not isinstance(validation, dict)
        or validation.get("status") != "PASS"
        or not isinstance(continuation, dict)
        or continuation.get("advancement_allowed") is not True
        or continuation.get("can_continue") is True
        or not isinstance(tree, dict)
        or tree.get("schema_version") != "native-document-structure-tree/v1"
        or not tree.get("nodes")
    ):
        raise NativeStructureBaselineError("NATIVE_BASELINE_NOT_ADVANCEMENT_READY")
    if source.status is SourceIRStatus.FAILED:
        raise NativeStructureBaselineError("NATIVE_BASELINE_SOURCE_IR_FAILED")
    return reference


def native_structure_projection_sha256(
    *,
    public_reference: dict[str, Any],
    table_previews: tuple[dict[str, Any], ...],
) -> str:
    reference = json.loads(json.dumps(public_reference, ensure_ascii=False))
    return canonical_json_sha256(
        {
            "artifact_id": reference["artifact_id"],
            "source_sha256": reference["source_sha256"],
            "document_tree": reference["document_tree"],
            "logical_tables": reference.get("logical_tables", []),
            "physical_table_dispositions": reference.get(
                "physical_table_dispositions", []
            ),
            "nested_physical_tables": reference.get("nested_physical_tables", []),
            "nested_visual_tables": reference.get("nested_visual_tables", []),
            "structure_proposals": reference.get("structure_proposals", []),
            "table_previews": list(table_previews),
            "validation": reference.get("validation", {}),
            "continuation": _content_free_continuation(reference["continuation"]),
        }
    )


def _content_free_continuation(value: dict[str, Any]) -> dict[str, Any]:
    latest = value.get("latest_handoff")
    latest = latest if isinstance(latest, dict) else {}
    attempt = latest.get("latest_attempt")
    attempt = attempt if isinstance(attempt, dict) else {}
    return {
        "status": str(value.get("status") or ""),
        "advancement_allowed": value.get("advancement_allowed") is True,
        "evidence_state": str(value.get("evidence_state") or "SUFFICIENT"),
        "default_resolution_policy": str(
            value.get("default_resolution_policy") or "NONE"
        ),
        "handoff_id": str(latest.get("handoff_id") or ""),
        "handoff_status": str(latest.get("status") or ""),
        "attempt_id": str(attempt.get("attempt_id") or ""),
        "attempt_status": str(attempt.get("status") or ""),
        "rendered_output_sha256": str(attempt.get("rendered_output_sha256") or ""),
    }


def _native_run_id(**values: str) -> str:
    return f"agent-run-{canonical_json_sha256(values)[:32]}"


def _host_predecessor_identities(
    *,
    source: SourceIRSnapshot,
    reference: dict[str, Any],
    run_id: str,
    native_projection_sha256: str,
    kit: AgentKitIdentityV1,
) -> tuple[D2ValidatedD1ArtifactIdentityV1, AgentD1TableMapArtifactIdentityV1]:
    source_ir_sha = source_ir_canonical_sha256(source)
    table_nodes = _table_nodes(source)
    table_map_sha = canonical_json_sha256(
        {
            "source_ir_id": source.source_ir_id,
            "source_ir_canonical_sha256": source_ir_sha,
            "tables": [
                {
                    "table_id": item.table_payload.table_id,
                    "source_node_id": item.source_node_id,
                    "source_order": item.source_order,
                    "table_sha256": canonical_model_sha256(item.table_payload),
                }
                for item in table_nodes
                if item.table_payload is not None
            ],
        }
    )
    table_map = AgentD1TableMapArtifactIdentityV1(
        source_sha256=source.source_sha256,
        artifact_id=f"native-table-map-{table_map_sha[:32]}",
        artifact_sha256=table_map_sha,
    )
    evidence_sha = canonical_json_sha256(
        {
            "source_ir_canonical_sha256": source_ir_sha,
            "native_projection_sha256": native_projection_sha256,
            "validation": reference.get("validation", {}),
            "visual_summary": reference.get("visual_summary", {}),
            "table_map_artifact_sha256": table_map_sha,
        }
    )
    proposal_sha = canonical_json_sha256(
        {
            "schema_version": "native-structure-d1-acceptance/v1",
            "run_id": run_id,
            "source_sha256": source.source_sha256,
            "native_artifact_id": reference["artifact_id"],
            "native_projection_sha256": native_projection_sha256,
            "source_ir_canonical_sha256": source_ir_sha,
            "kit_bundle_sha256": kit.bundle_sha256,
        }
    )
    validator_receipt_sha = canonical_json_sha256(
        {
            "proposal_sha256": proposal_sha,
            "evidence_sha256": evidence_sha,
            "validator_id": "reqpilotx-native-structure-baseline-validator",
            "validator_version": "1.0.0",
        }
    )
    artifact_id = f"agent-artifact-{canonical_json_sha256({'run_id': run_id, 'proposal_sha256': proposal_sha})[:32]}"
    status = (
        AgentStageArtifactStatus.VALIDATED
        if source.status is SourceIRStatus.COMPLETE
        else AgentStageArtifactStatus.PARTIAL
    )
    return (
        D2ValidatedD1ArtifactIdentityV1(
            artifact_id=artifact_id,
            artifact_sha256=proposal_sha,
            run_id=run_id,
            source_sha256=source.source_sha256,
            status=status,
            proposal_sha256=proposal_sha,
            evidence_sha256=evidence_sha,
            validator_receipt_sha256=validator_receipt_sha,
            page_count=max(
                int(reference.get("visual_summary", {}).get("page_count") or 0),
                max(
                    (
                        int(item.native_locator.page_number or 0)
                        for item in source.nodes
                    ),
                    default=0,
                ),
            ),
            kit_bundle_sha256=kit.bundle_sha256,
        ),
        table_map,
    )


def _allocate_native_source_ranges(
    *,
    source: SourceIRSnapshot,
    tree: dict[str, Any],
) -> tuple[_AllocatedRange, ...]:
    nodes_by_id = {item.source_node_id: item for item in source.nodes}
    occupied: dict[str, list[tuple[int, int]]] = {}
    result: list[_AllocatedRange] = []
    for tree_node in tree.get("nodes", []):
        native_node_id = str(tree_node.get("node_id") or "")
        for content in tree_node.get("source_content", []):
            text = str(content.get("display_text") or content.get("text") or "")
            handles = tuple(
                sorted(
                    {
                        str(item)
                        for item in content.get("source_handles", [])
                        if str(item) in nodes_by_id
                    },
                    key=lambda item: (nodes_by_id[item].source_order, item),
                )
            )
            if not text.strip() or not handles:
                raise NativeStructureBaselineError(
                    "NATIVE_BASELINE_SOURCE_CONTENT_UNBOUND"
                )
            allocations = _match_content_ranges(
                text=text,
                handles=handles,
                nodes_by_id=nodes_by_id,
                occupied=occupied,
            )
            if not allocations:
                raise NativeStructureBaselineError(
                    "NATIVE_BASELINE_SOURCE_CONTENT_UNMATCHED"
                )
            for source_node_id, start, end in allocations:
                node = nodes_by_id[source_node_id]
                result.append(
                    _AllocatedRange(
                        native_node_id=native_node_id,
                        source_node_id=source_node_id,
                        start_offset=start,
                        end_offset=end,
                        source_order=node.source_order,
                        page_numbers=_node_pages(node),
                    )
                )
        for anchor in tree_node.get("source_anchors", []):
            if str(anchor.get("anchor_kind") or "") != "IMAGE":
                continue
            source_node = _image_anchor_source_node(
                anchor=anchor,
                source_nodes=source.nodes,
            )
            key = source_node.source_node_id
            if key in occupied:
                raise NativeStructureBaselineError(
                    "NATIVE_BASELINE_IMAGE_PLACEMENT_DUPLICATED"
                )
            occupied[key] = []
            result.append(
                _AllocatedRange(
                    native_node_id=native_node_id,
                    source_node_id=key,
                    start_offset=None,
                    end_offset=None,
                    source_order=source_node.source_order,
                    page_numbers=_node_pages(source_node),
                )
            )
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.source_order,
                -1 if item.start_offset is None else item.start_offset,
                item.source_node_id,
                item.native_node_id,
            ),
        )
    )


def _match_content_ranges(
    *,
    text: str,
    handles: tuple[str, ...],
    nodes_by_id: dict[str, SourceNode],
    occupied: dict[str, list[tuple[int, int]]],
) -> tuple[tuple[str, int, int], ...]:
    direct: list[tuple[int, str, int, int]] = []
    for handle in handles:
        raw = nodes_by_id[handle].raw_text
        for start in _substring_occurrences(raw, text):
            end = start + len(text)
            if not _overlaps(occupied.get(handle, ()), start, end):
                direct.append((nodes_by_id[handle].source_order, handle, start, end))
    if direct:
        _, handle, start, end = min(direct)
        occupied.setdefault(handle, []).append((start, end))
        return ((handle, start, end),)

    matched: list[tuple[str, int, int]] = []
    for line in (item for item in text.splitlines() if item.strip()):
        candidates: list[tuple[int, int, str, int, int]] = []
        for handle in handles:
            raw = nodes_by_id[handle].raw_text
            for start in _substring_occurrences(raw, line):
                end = start + len(line)
                if _overlaps(occupied.get(handle, ()), start, end):
                    continue
                candidates.append(
                    (nodes_by_id[handle].source_order, start, handle, start, end)
                )
        if not candidates:
            raise NativeStructureBaselineError("NATIVE_BASELINE_SOURCE_LINE_UNMATCHED")
        _, _, handle, start, end = min(candidates)
        occupied.setdefault(handle, []).append((start, end))
        matched.append((handle, start, end))
    return tuple(_merge_allocations(matched, nodes_by_id=nodes_by_id))


def _substring_occurrences(value: str, needle: str) -> tuple[int, ...]:
    if not needle:
        return ()
    result: list[int] = []
    start = 0
    while True:
        index = value.find(needle, start)
        if index < 0:
            return tuple(result)
        result.append(index)
        start = index + 1


def _overlaps(values: Any, start: int, end: int) -> bool:
    return any(
        start < other_end and other_start < end for other_start, other_end in values
    )


def _merge_allocations(
    values: list[tuple[str, int, int]],
    *,
    nodes_by_id: dict[str, SourceNode],
) -> list[tuple[str, int, int]]:
    ordered = sorted(
        values,
        key=lambda item: (nodes_by_id[item[0]].source_order, item[1], item[2]),
    )
    result: list[tuple[str, int, int]] = []
    for handle, start, end in ordered:
        if result and result[-1][0] == handle:
            previous_handle, previous_start, previous_end = result[-1]
            gap = nodes_by_id[handle].raw_text[previous_end:start]
            if previous_end <= start and not gap.strip():
                result[-1] = (previous_handle, previous_start, end)
                continue
        result.append((handle, start, end))
    return result


def _image_anchor_source_node(
    *,
    anchor: dict[str, Any],
    source_nodes: tuple[SourceNode, ...],
) -> SourceNode:
    asset_id = str(anchor.get("asset_id") or "")
    page = int(anchor.get("page") or 0)
    bbox = tuple(float(item) for item in anchor.get("bbox", ()))
    candidates = tuple(
        item
        for item in source_nodes
        if item.asset_ref == asset_id
        and item.native_locator.page_number == page
        and item.native_locator.bbox is not None
        and _bbox_close(item.native_locator.bbox, bbox)
    )
    if len(candidates) != 1:
        raise NativeStructureBaselineError("NATIVE_BASELINE_IMAGE_PLACEMENT_UNBOUND")
    return candidates[0]


def _bbox_close(left: tuple[float, ...], right: tuple[float, ...]) -> bool:
    return len(left) == len(right) == 4 and all(
        abs(float(a) - float(b)) <= 0.02 for a, b in zip(left, right, strict=True)
    )


def _compile_base_v1(
    *,
    source: SourceIRSnapshot,
    tree: dict[str, Any],
    allocations: tuple[_AllocatedRange, ...],
    run_id: str,
    d1_identity: D2ValidatedD1ArtifactIdentityV1,
    table_map_identity: AgentD1TableMapArtifactIdentityV1,
    kit: AgentKitIdentityV1,
) -> tuple[
    D2LogicalStructureProposalV1,
    D2LogicalStructureValidationReceiptV1,
    dict[str, str],
]:
    source_by_id = {item.source_node_id: item for item in source.nodes}
    allocations_by_native: dict[str, list[_AllocatedRange]] = {}
    for item in allocations:
        allocations_by_native.setdefault(item.native_node_id, []).append(item)
    tree_nodes = tuple(item for item in tree.get("nodes", []) if isinstance(item, dict))
    tree_by_id = {str(item.get("node_id") or ""): item for item in tree_nodes}
    child_ids: dict[str, list[str]] = {}
    for item in tree_nodes:
        parent = str(item.get("parent_id") or "")
        if parent:
            child_ids.setdefault(parent, []).append(str(item["node_id"]))

    segments_by_native: dict[str, tuple[D2SourceSegmentV1, ...]] = {}
    for native_node_id, selected in allocations_by_native.items():
        segments_by_native[native_node_id] = tuple(
            _source_segment(source, source_by_id[item.source_node_id], item)
            for item in sorted(
                selected,
                key=lambda value: (
                    value.source_order,
                    -1 if value.start_offset is None else value.start_offset,
                    value.source_node_id,
                ),
            )
        )

    retained: set[str] = set()

    def retain(node_id: str) -> bool:
        keep = bool(segments_by_native.get(node_id))
        for child in child_ids.get(node_id, ()):
            keep = retain(child) or keep
        if keep:
            retained.add(node_id)
        return keep

    for root_id in tree.get("root_ids", []):
        retain(str(root_id))
    if not retained:
        raise NativeStructureBaselineError("NATIVE_BASELINE_NO_TEXTUAL_OBJECTS")

    descendant_segments: dict[str, tuple[D2SourceSegmentV1, ...]] = {}

    def descendant(node_id: str) -> tuple[D2SourceSegmentV1, ...]:
        values = tuple(
            segment
            for child in child_ids.get(node_id, ())
            if child in retained
            for segment in (
                *segments_by_native.get(child, ()),
                *descendant(child),
            )
        )
        canonical = tuple(
            sorted(
                {item.segment_id: item for item in values}.values(),
                key=lambda item: (
                    item.source_order,
                    item.source_handle,
                    -1 if item.start_offset is None else item.start_offset,
                    item.segment_id,
                ),
            )
        )
        descendant_segments[node_id] = canonical
        return canonical

    for root_id in tree.get("root_ids", []):
        if str(root_id) in retained:
            descendant(str(root_id))

    native_object_ids: dict[str, str] = {}

    def nearest_retained_parent(node_id: str) -> str | None:
        parent = tree_by_id[node_id].get("parent_id")
        while parent is not None:
            parent_id = str(parent)
            if parent_id in retained:
                return parent_id
            parent = tree_by_id[parent_id].get("parent_id")
        return None

    pending = set(retained)
    while pending:
        progressed = False
        for native_node_id in sorted(pending):
            parent_native = nearest_retained_parent(native_node_id)
            if parent_native is not None and parent_native not in native_object_ids:
                continue
            segments = segments_by_native.get(native_node_id, ())
            kind = _native_object_kind(tree_by_id[native_node_id])
            parent_id = (
                None if parent_native is None else native_object_ids[parent_native]
            )
            if segments:
                object_id = stable_d2_logical_object_id(
                    source_sha256=source.source_sha256,
                    object_kind=kind,
                    source_segment_ids=tuple(item.segment_id for item in segments),
                )
            else:
                object_id = stable_d2_derived_container_id(
                    source_sha256=source.source_sha256,
                    object_kind=kind,
                    descendant_source_segment_ids=tuple(
                        item.segment_id
                        for item in descendant_segments.get(native_node_id, ())
                    ),
                    descendant_logical_table_scope_ids=(),
                    parent_logical_object_id=parent_id,
                )
            native_object_ids[native_node_id] = object_id
            pending.remove(native_node_id)
            progressed = True
        if not progressed:
            raise NativeStructureBaselineError("NATIVE_BASELINE_TREE_PARENT_CYCLE")

    logical_objects: list[D2LogicalObjectV1] = []
    for native_node_id in retained:
        tree_node = tree_by_id[native_node_id]
        segments = segments_by_native.get(native_node_id, ())
        evidence_segments = segments or descendant_segments[native_node_id]
        pages = tuple(
            sorted({page for item in evidence_segments for page in item.page_numbers})
        )
        parent_native = nearest_retained_parent(native_node_id)
        values = {
            "logical_object_id": native_object_ids[native_node_id],
            "object_kind": _native_object_kind(tree_node),
            "parent_logical_object_id": (
                None if parent_native is None else native_object_ids[parent_native]
            ),
            "source_order": min(item.source_order for item in evidence_segments),
            "page_numbers": pages,
            "source_segments": segments,
            "logical_table_scope_ids": (),
            "reason_codes": ("native_agent_structure_boundary",),
        }
        logical_objects.append(
            D2LogicalObjectV1(
                **values,
                object_sha256=stable_d2_logical_object_sha256(**values),
            )
        )

    table_scopes: list[AgentLogicalTableScopeV1] = []
    physical_dispositions: list[D2PhysicalTableDispositionV1] = []
    for table_node in _table_nodes(source):
        assert table_node.table_payload is not None
        table_id = table_node.table_payload.table_id
        scope = build_agent_logical_table_scope(
            source,
            d1_table_map_artifact=table_map_identity,
            physical_table_ids=(table_id,),
            reason_codes=("base_physical_table_for_region_replacement",),
        )
        table_scopes.append(scope)
        table_values = {
            "logical_object_id": scope.logical_object_id,
            "object_kind": D2LogicalObjectKind.TABLE,
            "parent_logical_object_id": None,
            "source_order": table_node.source_order,
            "page_numbers": tuple(
                sorted(
                    {
                        page
                        for part in scope.physical_parts
                        for page in part.page_numbers
                    }
                )
            ),
            "source_segments": (),
            "logical_table_scope_ids": (scope.logical_object_id,),
            "reason_codes": ("base_physical_table_for_region_replacement",),
        }
        logical_objects.append(
            D2LogicalObjectV1(
                **table_values,
                object_sha256=stable_d2_logical_object_sha256(**table_values),
            )
        )
        disposition_values = {
            "table_id": table_id,
            "disposition": D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART,
            "logical_table_scope_id": scope.logical_object_id,
            "nested_owner_table_id": None,
            "reason_codes": ("base_physical_table_for_region_replacement",),
            "evidence_refs": (f"document-table-map:{table_id}",),
        }
        physical_dispositions.append(
            D2PhysicalTableDispositionV1(
                **disposition_values,
                disposition_sha256=stable_d2_table_disposition_sha256(
                    **disposition_values
                ),
            )
        )

    object_by_id = {item.logical_object_id: item for item in logical_objects}
    source_dispositions = _source_dispositions(
        source=source,
        allocations=allocations,
        native_object_ids=native_object_ids,
        segments_by_native=segments_by_native,
        object_by_id=object_by_id,
    )
    ordered_objects = tuple(
        sorted(
            logical_objects,
            key=lambda item: (item.source_order, item.logical_object_id),
        )
    )
    status = (
        D2LogicalStructureStatus.COMPLETE
        if source.status is SourceIRStatus.COMPLETE
        and d1_identity.status is AgentStageArtifactStatus.VALIDATED
        else D2LogicalStructureStatus.PARTIAL
    )
    values = {
        "run_id": run_id,
        "source_sha256": source.source_sha256,
        "source_ir_id": source.source_ir_id,
        "source_ir_canonical_sha256": source_ir_canonical_sha256(source),
        "kit_identity": kit,
        "predecessor_d1_artifact": d1_identity,
        "d1_table_map_artifact": table_map_identity,
        "status": status,
        "logical_objects": ordered_objects,
        "logical_table_scopes": tuple(table_scopes),
        "source_evidence_dispositions": source_dispositions,
        "physical_table_dispositions": tuple(physical_dispositions),
        "unresolved_codes": (
            ()
            if status is D2LogicalStructureStatus.COMPLETE
            else ("source_ir_partial",)
        ),
    }
    draft = D2LogicalStructureProposalV1.model_construct(
        **values,
        proposal_sha256="0" * 64,
    )
    proposal = D2LogicalStructureProposalV1(
        **values,
        proposal_sha256=stable_d2_logical_structure_proposal_sha256(draft),
    )
    receipt = validate_d2_logical_structure_proposal(
        source,
        proposal,
        expected_d1_artifact=d1_identity,
        expected_d1_table_map_artifact=table_map_identity,
        expected_kit_identity=kit,
    )
    return proposal, receipt, native_object_ids


def _source_segment(
    source: SourceIRSnapshot,
    node: SourceNode,
    allocation: _AllocatedRange,
) -> D2SourceSegmentV1:
    values = {
        "segment_id": stable_d2_source_segment_id(
            source_sha256=source.source_sha256,
            handle_kind=D2SourceHandleKind.SOURCE_NODE,
            source_handle=node.source_node_id,
            start_offset=allocation.start_offset,
            end_offset=allocation.end_offset,
        ),
        "source_sha256": source.source_sha256,
        "handle_kind": D2SourceHandleKind.SOURCE_NODE,
        "source_handle": node.source_node_id,
        "owner_source_node_id": node.source_node_id,
        "source_order": node.source_order,
        "page_numbers": allocation.page_numbers,
        "start_offset": allocation.start_offset,
        "end_offset": allocation.end_offset,
        "source_content_sha256": _source_node_content_sha256(node),
    }
    return D2SourceSegmentV1(
        **values,
        segment_sha256=stable_d2_source_segment_sha256(**values),
    )


def _source_dispositions(
    *,
    source: SourceIRSnapshot,
    allocations: tuple[_AllocatedRange, ...],
    native_object_ids: dict[str, str],
    segments_by_native: dict[str, tuple[D2SourceSegmentV1, ...]],
    object_by_id: dict[str, D2LogicalObjectV1],
) -> tuple[D2SourceEvidenceDispositionV1, ...]:
    segment_by_key = {
        (
            native_node_id,
            segment.owner_source_node_id,
            segment.start_offset,
            segment.end_offset,
        ): segment
        for native_node_id, segments in segments_by_native.items()
        for segment in segments
    }
    allocation_by_source: dict[str, list[_AllocatedRange]] = {}
    for item in allocations:
        allocation_by_source.setdefault(item.source_node_id, []).append(item)
    table_nodes = _table_nodes(source)
    tables_by_page: dict[int, list[tuple[float, float, float, float]]] = {}
    for item in table_nodes:
        if item.native_locator.page_number and item.native_locator.bbox:
            tables_by_page.setdefault(item.native_locator.page_number, []).append(
                item.native_locator.bbox
            )
    output: list[D2SourceEvidenceDispositionV1] = []
    for node in source.nodes:
        if node.kind is SourceNodeKind.TABLE:
            continue
        selected = sorted(
            allocation_by_source.get(node.source_node_id, ()),
            key=lambda item: -1 if item.start_offset is None else item.start_offset,
        )
        for item in selected:
            object_id = native_object_ids[item.native_node_id]
            segment = segment_by_key[
                (
                    item.native_node_id,
                    item.source_node_id,
                    item.start_offset,
                    item.end_offset,
                )
            ]
            disposition = (
                D2SourceEvidenceDispositionKind.NESTED_EVIDENCE
                if not node.raw_text
                else D2SourceEvidenceDispositionKind.LOGICAL_OBJECT_PART
            )
            output.append(
                _source_disposition(
                    source=source,
                    node=node,
                    start=item.start_offset,
                    end=item.end_offset,
                    disposition=disposition,
                    logical_object_id=object_id,
                    source_segment_id=segment.segment_id,
                    reason_codes=(
                        "native_image_evidence"
                        if not node.raw_text
                        else "native_agent_structure_boundary",
                    ),
                )
            )
        if not node.raw_text:
            if not selected:
                raise NativeStructureBaselineError(
                    "NATIVE_BASELINE_NON_TABLE_ASSET_UNDISPOSITIONED"
                )
            continue
        intervals = tuple(
            (int(item.start_offset), int(item.end_offset))
            for item in selected
            if item.start_offset is not None and item.end_offset is not None
        )
        for start, end in _complement_ranges(len(node.raw_text), intervals):
            if not node.raw_text[start:end].strip():
                reason = "unowned_whitespace_only"
            elif _inside_table_region(node, tables_by_page=tables_by_page):
                reason = "table_owned_duplicate_text"
            else:
                raise NativeStructureBaselineError(
                    "NATIVE_BASELINE_VISIBLE_TEXT_NOT_OWNED"
                )
            output.append(
                _source_disposition(
                    source=source,
                    node=node,
                    start=start,
                    end=end,
                    disposition=D2SourceEvidenceDispositionKind.NON_BUSINESS_SOURCE,
                    logical_object_id=None,
                    source_segment_id=None,
                    reason_codes=(reason,),
                )
            )
    return tuple(
        sorted(
            output,
            key=lambda item: (
                item.source_order,
                item.source_node_id,
                -1 if item.start_offset is None else item.start_offset,
                item.disposition_id,
            ),
        )
    )


def _source_disposition(
    *,
    source: SourceIRSnapshot,
    node: SourceNode,
    start: int | None,
    end: int | None,
    disposition: D2SourceEvidenceDispositionKind,
    logical_object_id: str | None,
    source_segment_id: str | None,
    reason_codes: tuple[str, ...],
) -> D2SourceEvidenceDispositionV1:
    disposition_id = stable_d2_source_disposition_id(
        source_sha256=source.source_sha256,
        source_node_id=node.source_node_id,
        start_offset=start,
        end_offset=end,
    )
    values = {
        "disposition_id": disposition_id,
        "source_sha256": source.source_sha256,
        "source_node_id": node.source_node_id,
        "source_order": node.source_order,
        "start_offset": start,
        "end_offset": end,
        "disposition": disposition,
        "logical_object_id": logical_object_id,
        "source_segment_id": source_segment_id,
        "reason_codes": tuple(sorted(reason_codes)),
        "evidence_refs": tuple(
            sorted(
                {
                    node.source_node_id,
                    *(item for item in (node.asset_ref,) if item),
                }
            )
        ),
    }
    return D2SourceEvidenceDispositionV1(
        **values,
        disposition_sha256=stable_d2_source_disposition_sha256(**values),
    )


def _complement_ranges(
    length: int,
    intervals: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    if length < 1:
        return ()
    ordered = sorted(intervals)
    cursor = 0
    result: list[tuple[int, int]] = []
    for start, end in ordered:
        if start < cursor or end <= start or end > length:
            raise NativeStructureBaselineError("NATIVE_BASELINE_RANGE_OVERLAP")
        if cursor < start:
            result.append((cursor, start))
        cursor = end
    if cursor < length:
        result.append((cursor, length))
    return tuple(result)


def _inside_table_region(
    node: SourceNode,
    *,
    tables_by_page: dict[int, list[tuple[float, float, float, float]]],
) -> bool:
    page = node.native_locator.page_number
    bbox = node.native_locator.bbox
    if page is None or bbox is None:
        return False
    return any(
        _bbox_overlap_ratio(bbox, table_bbox) >= 0.95
        for table_bbox in tables_by_page.get(page, ())
    )


def _bbox_overlap_ratio(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (
        (x1 - x0)
        * (y1 - y0)
        / max(
            1.0,
            (left[2] - left[0]) * (left[3] - left[1]),
        )
    )


def _map_native_table_tokens(
    *,
    source: SourceIRSnapshot,
    reference: dict[str, Any],
    table_previews: tuple[dict[str, Any], ...],
) -> dict[str, SourceNode]:
    dispositions = tuple(reference.get("physical_table_dispositions", ()))
    tables = _table_nodes(source)
    if len(dispositions) != len(tables):
        raise NativeStructureBaselineError("NATIVE_BASELINE_TABLE_INVENTORY_DRIFT")
    mapping: dict[str, SourceNode] = {}
    for disposition, node in zip(dispositions, tables, strict=True):
        token = str(disposition.get("table_token") or "")
        if not re.fullmatch(r"T\d{4}", token) or token in mapping:
            raise NativeStructureBaselineError("NATIVE_BASELINE_TABLE_TOKEN_INVALID")
        if int(disposition.get("page") or 0) != node.native_locator.page_number:
            raise NativeStructureBaselineError("NATIVE_BASELINE_TABLE_PAGE_DRIFT")
        mapping[token] = node
    cells_by_token = {
        token: {
            cell.cell_id
            for cell in mapping[token].table_payload.cells  # type: ignore[union-attr]
        }
        for token in mapping
    }
    for fragment in table_previews:
        token = str(fragment.get("table_token") or "")
        if token not in mapping or any(
            str(cell.get("cell_id") or "") not in cells_by_token[token]
            for cell in fragment.get("cells", ())
        ):
            raise NativeStructureBaselineError("NATIVE_BASELINE_TABLE_CELL_DRIFT")
    return mapping


def _compile_native_table_regions(
    *,
    source: SourceIRSnapshot,
    reference: dict[str, Any],
    table_token_map: dict[str, SourceNode],
    table_map_identity: AgentD1TableMapArtifactIdentityV1,
    kit: AgentKitIdentityV1,
    native_object_ids: dict[str, str],
) -> tuple[D2TableRegionArtifactV1, dict[str, str]]:
    tree = reference["document_tree"]
    tree_nodes = tuple(tree.get("nodes", ()))
    scope_owner_node = {
        str(block.get("logical_table_scope_id") or ""): str(node.get("node_id") or "")
        for node in tree_nodes
        for block in node.get("content_blocks", ())
        if str(block.get("logical_table_scope_id") or "")
    }
    proposal_by_object = {
        str(item.get("object_id") or ""): item
        for item in reference.get("structure_proposals", ())
    }
    large_table_scope_ids = _large_table_scope_ids(
        reference=reference,
        table_token_map=table_token_map,
    )
    region_candidates: list[Any] = []
    scope_region_keys: dict[str, list[str]] = {}
    object_region_keys: dict[str, str] = {}
    region_key_by_cell_id: dict[str, str] = {}
    pending_nested: list[tuple[dict[str, Any], str, SourceNode, tuple[Any, ...]]] = []
    empty_tables: list[D2EmptyPhysicalTableDispositionCandidateV1] = []

    for physical in reference.get("physical_table_dispositions", ()):
        token = str(physical.get("table_token") or "")
        node = table_token_map[token]
        table = node.table_payload
        assert table is not None
        cells = tuple(
            sorted(table.cells, key=lambda item: (item.row, item.column, item.cell_id))
        )
        if not cells:
            empty_tables.append(
                D2EmptyPhysicalTableDispositionCandidateV1(
                    physical_table_id=table.table_id,
                    disposition=D2EmptyPhysicalTableDispositionKind.NON_BUSINESS_FALSE_REGION,
                    reason_codes=("native_empty_table_kept_non_business",),
                )
            )
            continue
        for raw_region in physical.get("regions", ()):
            start, end = (int(item) for item in raw_region.get("row_range", ()))
            selected_ordinals = tuple(
                ordinal for ordinal, cell in enumerate(cells) if start <= cell.row < end
            )
            spans = _ordinal_spans(selected_ordinals)
            if not spans:
                raise NativeStructureBaselineError("NATIVE_BASELINE_EMPTY_TABLE_REGION")
            region_key = _safe_region_key(token=token, start=start, end=end)
            for ordinal in selected_ordinals:
                region_key_by_cell_id[cells[ordinal].cell_id] = region_key
            kind = str(raw_region.get("kind") or "")
            reason = _reason_code(kind)
            scope_id = str(raw_region.get("scope_id") or "")
            if kind in {"LOGICAL_TABLE", "MIXED_SUBCELL_CONTENT"} and scope_id:
                if scope_id in large_table_scope_ids:
                    owner_node_id = scope_owner_node.get(scope_id, "")
                    for fragment_start, fragment_end in _table_record_row_windows(
                        cells=cells,
                        start_row=start,
                        end_row=end,
                    ):
                        fragment_ordinals = tuple(
                            ordinal
                            for ordinal, cell in enumerate(cells)
                            if fragment_start <= cell.row < fragment_end
                        )
                        fragment_spans = _ordinal_spans(fragment_ordinals)
                        fragment_key = _safe_region_key(
                            token=token,
                            start=fragment_start,
                            end=fragment_end,
                        )
                        object_key = _safe_object_key(
                            f"table-record:{scope_id}:{token}:"
                            f"{fragment_start}:{fragment_end}"
                        )
                        object_region_keys[object_key] = (
                            f"record-parent:{owner_node_id}"
                        )
                        for ordinal in fragment_ordinals:
                            region_key_by_cell_id[cells[ordinal].cell_id] = fragment_key
                        region_candidates.append(
                            D2LogicalObjectPartTableRegionCandidateV1(
                                region_key=fragment_key,
                                physical_table_id=table.table_id,
                                cell_spans=fragment_spans,
                                disposition=(
                                    D2TableRegionDispositionKind.LOGICAL_OBJECT_PART
                                ),
                                logical_object_key=object_key,
                                reason_codes=("native_table_record_window",),
                            )
                        )
                    continue
                region_candidates.append(
                    D2LogicalScopePartTableRegionCandidateV1(
                        region_key=region_key,
                        physical_table_id=table.table_id,
                        cell_spans=spans,
                        disposition=D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
                        logical_scope_key=_safe_scope_key(scope_id),
                        reason_codes=(reason,),
                    )
                )
                scope_region_keys.setdefault(scope_id, []).append(region_key)
            elif kind == "NESTED_TABLE":
                pending_nested.append((raw_region, region_key, node, spans))
            elif kind == "ADJACENT_PARENT_TEXT_UNRESOLVED":
                proposal = proposal_by_object.get(f"{token}-trailing-text", {})
                target_native = str(proposal.get("recommended_target_node_id") or "")
                if target_native not in native_object_ids:
                    target_native = _nearest_scope_owner(
                        raw_region=raw_region,
                        physical=physical,
                        scope_owner_node=scope_owner_node,
                        native_object_ids=native_object_ids,
                    )
                object_key = _safe_object_key(target_native)
                object_region_keys[object_key] = target_native
                region_candidates.append(
                    D2LogicalObjectPartTableRegionCandidateV1(
                        region_key=region_key,
                        physical_table_id=table.table_id,
                        cell_spans=spans,
                        disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                        logical_object_key=object_key,
                        reason_codes=("default_parent_note",),
                    )
                )
            elif kind in {
                "MIXED_FLOW_TEXT_AND_HEADING",
                "MIXED_FLOW_TEXT_AND_TABLE_NOTE",
            }:
                target_native = _best_native_text_owner(
                    cells=tuple(cells[ordinal] for ordinal in selected_ordinals),
                    page=int(physical.get("page") or 0),
                    tree_nodes=tree_nodes,
                    native_object_ids=native_object_ids,
                )
                object_key = _safe_object_key(target_native)
                object_region_keys[object_key] = target_native
                region_candidates.append(
                    D2LogicalObjectPartTableRegionCandidateV1(
                        region_key=region_key,
                        physical_table_id=table.table_id,
                        cell_spans=spans,
                        disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                        logical_object_key=object_key,
                        reason_codes=(reason,),
                    )
                )
            else:
                region_candidates.append(
                    D2NonBusinessFalseTableRegionCandidateV1(
                        region_key=region_key,
                        physical_table_id=table.table_id,
                        cell_spans=spans,
                        disposition=D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION,
                        reason_codes=(reason,),
                    )
                )

    nested_by_token = {
        str(item.get("table_token") or ""): item
        for item in reference.get("nested_physical_tables", ())
    }
    for raw_region, region_key, node, spans in pending_nested:
        table = node.table_payload
        assert table is not None
        nested = nested_by_token.get(
            next(
                token
                for token, candidate in table_token_map.items()
                if candidate.source_node_id == node.source_node_id
            ),
            {},
        )
        owner_locator = str(nested.get("owner_locator") or "")
        cell_match = re.search(r"(cell-[0-9a-f]{32})", owner_locator)
        owner_region_key = (
            region_key_by_cell_id.get(cell_match.group(1), "") if cell_match else ""
        )
        if not owner_region_key:
            owner_scope = str(raw_region.get("owner_scope_id") or "")
            owner_region_key = next(iter(scope_region_keys.get(owner_scope, ())), "")
        if not owner_region_key:
            raise NativeStructureBaselineError("NATIVE_BASELINE_NESTED_OWNER_MISSING")
        region_candidates.append(
            D2NestedValueTableRegionCandidateV1(
                region_key=region_key,
                physical_table_id=table.table_id,
                cell_spans=spans,
                disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                nested_owner_region_key=owner_region_key,
                reason_codes=("native_nested_value_table",),
            )
        )

    logical_scopes = tuple(
        D2TableLogicalScopeCandidateV1(
            logical_scope_key=_safe_scope_key(scope_id),
            ordered_region_keys=tuple(region_keys),
            reason_codes=("native_logical_table_scope",),
        )
        for scope_id, region_keys in sorted(
            scope_region_keys.items(),
            key=lambda item: min(
                next(
                    index
                    for index, candidate in enumerate(region_candidates)
                    if candidate.region_key == region_key
                )
                for region_key in item[1]
            ),
        )
    )
    candidate = D2TableRegionCandidateArtifactV1(
        source_ir_id=source.source_ir_id,
        source_sha256=source.source_sha256,
        source_ir_canonical_sha256=d2_table_region_source_ir_canonical_sha256(source),
        kit_bundle_sha256=kit.bundle_sha256,
        d1_table_map_artifact_id=table_map_identity.artifact_id,
        d1_table_map_artifact_sha256=table_map_identity.artifact_sha256,
        regions=tuple(region_candidates),
        logical_scopes=logical_scopes,
        empty_table_dispositions=tuple(empty_tables),
    )
    artifact = compile_d2_table_regions(
        source,
        candidate,
        expected_kit_bundle_sha256=kit.bundle_sha256,
        expected_d1_table_map_artifact_id=table_map_identity.artifact_id,
        expected_d1_table_map_artifact_sha256=table_map_identity.artifact_sha256,
        gap_policy=D2TableRegionGapPolicy.REJECT,
    )
    return artifact, object_region_keys


def _build_region_placement_plan(
    *,
    source: SourceIRSnapshot,
    tree: dict[str, Any],
    reference: dict[str, Any],
    base_proposal: D2LogicalStructureProposalV1,
    base_receipt: D2LogicalStructureValidationReceiptV1,
    region_artifact: D2TableRegionArtifactV1,
    native_object_ids: dict[str, str],
    object_region_keys: dict[str, str],
):  # type: ignore[no-untyped-def]
    retained = tuple(
        D2RetainedBaseObjectPlacementV1(
            base_logical_object_id=item.logical_object_id,
            parent_logical_object_id=item.parent_logical_object_id,
            reason_codes=("retain_native_structure_object",),
        )
        for item in base_proposal.logical_objects
        if item.object_kind is not D2LogicalObjectKind.TABLE
    )
    replacements = tuple(
        D2BaseTableReplacementCandidateV1(
            base_logical_object_id=item.logical_object_id,
            disposition=D2BaseTableReplacementDisposition.REPLACED_BY_REGION_TOPOLOGY,
            reason_codes=("replace_base_table_with_native_regions",),
        )
        for item in base_proposal.logical_objects
        if item.object_kind is D2LogicalObjectKind.TABLE
    )
    tree_nodes = {
        str(item.get("node_id") or ""): item for item in tree.get("nodes", ())
    }
    scope_native_owner = {
        str(block.get("logical_table_scope_id") or ""): str(node.get("node_id") or "")
        for node in tree_nodes.values()
        for block in node.get("content_blocks", ())
        if str(block.get("logical_table_scope_id") or "")
    }

    def retained_owner(native_node_id: str) -> str:
        cursor: str | None = native_node_id
        while cursor is not None:
            if cursor in native_object_ids:
                return native_object_ids[cursor]
            parent = tree_nodes.get(cursor, {}).get("parent_id")
            cursor = None if parent is None else str(parent)
        return next(
            item.logical_object_id
            for item in base_proposal.logical_objects
            if item.object_kind is not D2LogicalObjectKind.TABLE
        )

    scope_placements = tuple(
        D2RegionLogicalScopePlacementV1(
            logical_scope_id=scope.logical_scope_id,
            parent_logical_object_id=retained_owner(
                scope_native_owner.get(scope.candidate_scope_key, "")
            ),
            reason_codes=("place_native_table_under_title_tree",),
        )
        for scope in region_artifact.logical_scopes
    )
    object_placements: list[D2RegionLogicalObjectPlacementV1] = []
    for compiled in region_artifact.logical_objects:
        owner_binding = object_region_keys.get(compiled.candidate_object_key, "")
        record_window = owner_binding.startswith("record-parent:")
        native_node_id = (
            owner_binding.removeprefix("record-parent:")
            if record_window
            else owner_binding
        )
        target = None if record_window else native_object_ids.get(native_node_id)
        if target is not None:
            base = next(
                item
                for item in base_proposal.logical_objects
                if item.logical_object_id == target
            )
            object_placements.append(
                D2RegionLogicalObjectPlacementV1(
                    placement_key=_placement_key(compiled.candidate_object_key),
                    source_region_logical_object_ids=(compiled.logical_object_id,),
                    mode=D2RegionLogicalObjectPlacementMode.MERGE_RETAINED_BASE,
                    target_base_logical_object_id=target,
                    parent_logical_object_id=base.parent_logical_object_id,
                    reason_codes=("merge_native_table_text_into_title_scope",),
                )
            )
        else:
            object_placements.append(
                D2RegionLogicalObjectPlacementV1(
                    placement_key=_placement_key(compiled.candidate_object_key),
                    source_region_logical_object_ids=(compiled.logical_object_id,),
                    mode=D2RegionLogicalObjectPlacementMode.NEW_REGION_OBJECT,
                    object_kind=(
                        D2LogicalObjectKind.PROVISION_SCOPE
                        if record_window
                        else D2LogicalObjectKind.SECTION
                    ),
                    parent_logical_object_id=retained_owner(native_node_id),
                    reason_codes=(
                        "create_native_table_record_window"
                        if record_window
                        else "create_native_table_text_scope",
                    ),
                )
            )
    root_owner = next(
        item.logical_object_id
        for item in base_proposal.logical_objects
        if item.object_kind is not D2LogicalObjectKind.TABLE
        and item.parent_logical_object_id is None
    )
    supplemental: tuple[D2TableSupplementalEvidencePlacementV1, ...] = (
        build_d2_table_supplemental_object_owner_placements(
            source,
            region_artifact,
            owner_reference_kind=(
                D2SupplementalObjectOwnerReferenceKindV1.RETAINED_BASE_OBJECT
            ),
            owner_reference=root_owner,
            reason_codes=("preserve_native_table_supplemental_evidence",),
        )
    )
    return build_d2_region_placement_plan(
        base_proposal=base_proposal,
        base_validation_receipt=base_receipt,
        table_region_artifact=region_artifact,
        retained_base_objects=retained,
        base_table_replacements=replacements,
        region_logical_scope_placements=scope_placements,
        region_logical_object_placements=tuple(object_placements),
        supplemental_evidence_placements=supplemental,
    )


def _accepted_identity(
    *,
    project_id: str,
    document_id: str,
    reference: dict[str, Any],
    native_projection_sha256: str,
    source: SourceIRSnapshot,
    kit: AgentKitIdentityV1,
    v2: D2LogicalStructureCompilationV2,
    region_artifact: D2TableRegionArtifactV1,
    d3_stage_input: D3RegionStageInputCompilationV1,
) -> NativeStructureAcceptedBaselineV1:
    continuation = reference["continuation"]
    values = {
        "status": "ACCEPTED",
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source.source_sha256,
        "native_artifact_id": str(reference["artifact_id"]),
        "native_projection_sha256": native_projection_sha256,
        "source_ir_id": source.source_ir_id,
        "source_ir_canonical_sha256": source_ir_canonical_sha256(source),
        "kit_identity": kit,
        "accepted_structure_artifact_id": v2.validation_receipt.artifact_id,
        "accepted_structure_artifact_sha256": v2.validation_receipt.artifact_sha256,
        "accepted_structure_validator_receipt_sha256": (
            v2.validation_receipt.receipt_sha256
        ),
        "d3_stage_input_manifest_id": d3_stage_input.manifest.manifest_id,
        "d3_stage_input_manifest_sha256": d3_stage_input.manifest.manifest_sha256,
        "d3_source_scope_sha256": d3_stage_input.segment_set.source_scope_sha256,
        "evidence_state": str(continuation.get("evidence_state") or "SUFFICIENT"),
        "decision_policy": str(continuation.get("default_resolution_policy") or "NONE"),
        "logical_object_count": len(v2.proposal.logical_objects),
        "exact_source_segment_count": len(d3_stage_input.segment_set.segments),
        "physical_table_count": len(region_artifact.physical_table_cell_coverage),
        "table_region_count": len(region_artifact.regions),
    }
    draft = NativeStructureAcceptedBaselineV1.model_construct(
        **values,
        baseline_id="native-structure-baseline-" + "0" * 32,
        baseline_sha256="0" * 64,
    )
    digest = canonical_json_sha256(
        draft.model_dump(
            mode="json",
            exclude={"baseline_id", "baseline_sha256"},
        )
    )
    return NativeStructureAcceptedBaselineV1(
        **values,
        baseline_id=f"native-structure-baseline-{digest[:32]}",
        baseline_sha256=digest,
    )


def _table_nodes(source: SourceIRSnapshot) -> tuple[SourceNode, ...]:
    return tuple(
        sorted(
            (
                item
                for item in source.nodes
                if item.kind is SourceNodeKind.TABLE and item.table_payload is not None
            ),
            key=lambda item: (item.source_order, item.table_payload.table_id),  # type: ignore[union-attr]
        )
    )


def _node_pages(node: SourceNode) -> tuple[int, ...]:
    page = node.native_locator.page_number
    if page is None:
        raise NativeStructureBaselineError("NATIVE_BASELINE_SOURCE_PAGE_MISSING")
    return (page,)


def _source_node_content_sha256(node: SourceNode) -> str:
    if node.raw_text:
        return hashlib.sha256(node.raw_text.encode("utf-8")).hexdigest()
    if node.table_payload is not None:
        return canonical_model_sha256(node.table_payload)
    if node.asset_ref is not None:
        return canonical_model_sha256(node)
    raise NativeStructureBaselineError("NATIVE_BASELINE_SOURCE_CONTENT_MISSING")


def _native_object_kind(node: dict[str, Any]) -> D2LogicalObjectKind:
    value = str(node.get("kind") or "").upper()
    if value in {"DOCUMENT", "DOCUMENT_UNIT"}:
        return D2LogicalObjectKind.DOCUMENT_UNIT
    if value in {"ATTACHMENT", "APPENDIX"}:
        return D2LogicalObjectKind.ATTACHMENT
    if value in {"ITEM", "LIST_ITEM", "PROVISION_SCOPE"}:
        return D2LogicalObjectKind.PROVISION_SCOPE
    if value == "FIGURE":
        return D2LogicalObjectKind.FIGURE
    return D2LogicalObjectKind.SECTION


def _ordinal_spans(
    values: tuple[int, ...],
) -> tuple[D2PhysicalTableCellOrdinalSpanV1, ...]:
    if not values:
        return ()
    spans: list[D2PhysicalTableCellOrdinalSpanV1] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        spans.append(
            D2PhysicalTableCellOrdinalSpanV1(
                start_cell_ordinal=start,
                end_cell_ordinal_exclusive=previous + 1,
            )
        )
        start = previous = value
    spans.append(
        D2PhysicalTableCellOrdinalSpanV1(
            start_cell_ordinal=start,
            end_cell_ordinal_exclusive=previous + 1,
        )
    )
    return tuple(spans)


def _large_table_scope_ids(
    *,
    reference: dict[str, Any],
    table_token_map: dict[str, SourceNode],
) -> set[str]:
    counts: dict[str, tuple[int, int]] = {}
    for physical in reference.get("physical_table_dispositions", ()):
        token = str(physical.get("table_token") or "")
        node = table_token_map.get(token)
        table = node.table_payload if node is not None else None
        if table is None:
            continue
        cells = tuple(
            sorted(
                table.cells,
                key=lambda item: (item.row, item.column, item.cell_id),
            )
        )
        for region in physical.get("regions", ()):
            scope_id = str(region.get("scope_id") or "")
            if not scope_id or str(region.get("kind") or "") not in {
                "LOGICAL_TABLE",
                "MIXED_SUBCELL_CONTENT",
            }:
                continue
            start, end = (int(item) for item in region.get("row_range", ()))
            selected = tuple(cell for cell in cells if start <= cell.row < end)
            old_count, old_characters = counts.get(scope_id, (0, 0))
            counts[scope_id] = (
                old_count + len(selected),
                old_characters + sum(len(cell.raw_text) for cell in selected),
            )
    return {
        scope_id
        for scope_id, (cell_count, character_count) in counts.items()
        if cell_count >= 400 or character_count >= 8_000
    }


def _table_record_row_windows(
    *,
    cells: tuple[Any, ...],
    start_row: int,
    end_row: int,
    max_rows: int = 8,
    max_cells: int = 250,
) -> tuple[tuple[int, int], ...]:
    if end_row <= start_row:
        raise NativeStructureBaselineError("NATIVE_BASELINE_TABLE_WINDOW_INVALID")
    selected = tuple(cell for cell in cells if start_row <= cell.row < end_row)
    safe_boundaries = [start_row]
    for boundary in range(start_row + 1, end_row):
        if not any(
            cell.row < boundary < cell.row + max(1, cell.row_span) for cell in selected
        ):
            safe_boundaries.append(boundary)
    safe_boundaries.append(end_row)
    result: list[tuple[int, int]] = []
    current = start_row
    while current < end_row:
        candidates = tuple(item for item in safe_boundaries if item > current)
        eligible = tuple(
            boundary
            for boundary in candidates
            if boundary - current <= max_rows
            and sum(current <= cell.row < boundary for cell in selected) <= max_cells
        )
        boundary = max(eligible) if eligible else min(candidates)
        result.append((current, boundary))
        current = boundary
    if tuple(range(start_row, end_row)) != tuple(
        row for start, end in result for row in range(start, end)
    ):
        raise NativeStructureBaselineError("NATIVE_BASELINE_TABLE_WINDOW_COVERAGE")
    return tuple(result)


def _safe_region_key(*, token: str, start: int, end: int) -> str:
    value = f"native.{token.lower()}.{start}.{end}"
    if _SAFE_KEY_PATTERN.fullmatch(value) is None:
        raise NativeStructureBaselineError("NATIVE_BASELINE_REGION_KEY_INVALID")
    return value


def _safe_scope_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.:-]+", ".", value.lower()).strip(".")
    result = f"native.scope.{normalized}"
    if _SAFE_KEY_PATTERN.fullmatch(result) is None:
        raise NativeStructureBaselineError("NATIVE_BASELINE_SCOPE_KEY_INVALID")
    return result


def _safe_object_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.:-]+", ".", value.lower()).strip(".")
    result = f"native.object.{normalized or canonical_json_sha256(value)[:16]}"
    if _SAFE_KEY_PATTERN.fullmatch(result) is None:
        raise NativeStructureBaselineError("NATIVE_BASELINE_OBJECT_KEY_INVALID")
    return result


def _placement_key(value: str) -> str:
    return f"native.placement.{canonical_json_sha256(value)[:24]}"


def _reason_code(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return (f"native_{normalized}" if normalized else "native_region_decision")[:120]


def _nearest_scope_owner(
    *,
    raw_region: dict[str, Any],
    physical: dict[str, Any],
    scope_owner_node: dict[str, str],
    native_object_ids: dict[str, str],
) -> str:
    for scope_id in (
        str(raw_region.get("owner_scope_id") or ""),
        *(str(item.get("scope_id") or "") for item in physical.get("regions", ())),
    ):
        owner = scope_owner_node.get(scope_id, "")
        if owner in native_object_ids:
            return owner
    return next(iter(native_object_ids))


def _best_native_text_owner(
    *,
    cells: tuple[Any, ...],
    page: int,
    tree_nodes: tuple[dict[str, Any], ...],
    native_object_ids: dict[str, str],
) -> str:
    cell_text = "".join(str(item.raw_text or "") for item in cells)
    normalized_cell = re.sub(r"\s+", "", cell_text)
    candidates: list[tuple[int, int, str]] = []
    for position, node in enumerate(tree_nodes):
        node_id = str(node.get("node_id") or "")
        if node_id not in native_object_ids or page not in node.get("pages", ()):
            continue
        text = "".join(
            str(item.get("display_text") or item.get("text") or "")
            for item in node.get("source_content", ())
        )
        normalized = re.sub(r"\s+", "", text)
        score = (
            len(normalized_cell)
            if normalized_cell and normalized_cell in normalized
            else 0
        )
        candidates.append((-score, position, node_id))
    if not candidates:
        return next(iter(native_object_ids))
    return min(candidates)[2]


def _accepted_scope_owner_lookup(reference: dict[str, Any]) -> dict[str, str]:
    return {
        str(block.get("logical_table_scope_id") or ""): str(node.get("node_id") or "")
        for node in reference.get("document_tree", {}).get("nodes", ())
        for block in node.get("content_blocks", ())
    }


__all__ = [
    "NATIVE_STRUCTURE_ACCEPTED_BASELINE_SCHEMA_VERSION",
    "NativeStructureAcceptedBaselineV1",
    "NativeStructureBaselineArtifactV1",
    "NativeStructureBaselineCompilationV1",
    "NativeStructureBaselineError",
    "build_native_structure_baseline_artifact",
    "compile_native_structure_accepted_baseline",
    "native_structure_baseline_artifact_bytes",
    "native_structure_baseline_object_key",
    "native_structure_projection_sha256",
]
