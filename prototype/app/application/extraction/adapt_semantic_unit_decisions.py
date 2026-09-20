from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from app.application.extraction.unitize_explicit_list import (
    CONTINUOUS_EXPLICIT_LIST_CANDIDATE_UNITIZER_VERSION,
)
from app.domain.extraction.semantic_unit_decisions import (
    SemanticUnitDecision,
    SemanticUnitDecisionEvidence,
    SemanticUnitDecisionRequest,
    stable_semantic_unit_decision_request_id,
)
from app.domain.extraction.semantic_units import (
    SemanticUnit,
    SemanticUnitKind,
    SemanticUnitPartitionCandidate,
    SemanticUnitPartitionDisposition,
    SemanticUnitPlan,
    stable_semantic_unit_id,
    stable_semantic_unit_plan_id,
)
from app.domain.extraction.semantics import SemanticGroundingSpan
from app.domain.extraction.source_ir import SourceIRSnapshot, TableCellIR
from app.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)
from app.domain.extraction.visual_text import VisualTextObservation
from app.ports.semantic_unit_provider import ControlledSemanticUnitProviderPort


CONTROLLED_EXPLICIT_LIST_UNITIZER_VERSION = (
    "provider-neutral-continuous-explicit-list-controlled/v1"
)


class ControlledSemanticUnitAdapterRejectionCode(str, Enum):
    INPUT_MISMATCH = "controlled_semantic_unit_input_mismatch"
    CANDIDATE_NOT_ELIGIBLE = "controlled_semantic_unit_candidate_not_eligible"
    INVALID_PARTITION = "controlled_semantic_unit_invalid_partition"
    INVALID_PARENT = "controlled_semantic_unit_invalid_parent"
    INVALID_GROUNDING = "controlled_semantic_unit_invalid_grounding"
    PROVIDER_FAILURE = "controlled_semantic_unit_provider_failure"
    PROVIDER_RESULT_MISMATCH = "controlled_semantic_unit_provider_result_mismatch"


class ControlledSemanticUnitAdapterRejected(RuntimeError):
    """Fail-closed controlled-adapter error without source text."""

    def __init__(
        self,
        code: ControlledSemanticUnitAdapterRejectionCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ControlledSemanticUnitDecisionPlan:
    semantic_unit_plan: SemanticUnitPlan
    requests: tuple[SemanticUnitDecisionRequest, ...]

    def __post_init__(self) -> None:
        if not self.requests:
            raise ValueError("controlled unit decision plans require requests")
        if tuple(item.semantic_unit_id for item in self.requests) != (
            self.semantic_unit_plan.semantic_unit_ids
        ):
            raise ValueError(
                "controlled requests must exactly cover semantic units in order"
            )
        if len({item.decision_request_id for item in self.requests}) != len(
            self.requests
        ):
            raise ValueError("controlled unit decision request IDs must be unique")
        for request in self.requests:
            if (
                request.source_ir_id != self.semantic_unit_plan.source_ir_id
                or request.structure_snapshot_id
                != self.semantic_unit_plan.structure_snapshot_id
                or request.semantic_unit_plan_id
                != self.semantic_unit_plan.semantic_unit_plan_id
                or request.unitizer_version != self.semantic_unit_plan.unitizer_version
            ):
                raise ValueError(
                    "controlled requests must retain semantic-unit plan bindings"
                )


@dataclass(frozen=True)
class ControlledSemanticUnitDecisionExecution:
    plan: ControlledSemanticUnitDecisionPlan
    provider_id: str
    provider_version: str
    model_id: str
    decisions: tuple[SemanticUnitDecision, ...]

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.provider_id, self.provider_version, self.model_id)
        ):
            raise ValueError("controlled provider identities must not be blank")
        if tuple(item.decision_request_id for item in self.decisions) != tuple(
            item.decision_request_id for item in self.plan.requests
        ):
            raise ValueError(
                "controlled decisions must exactly cover request identities in order"
            )
        if tuple(item.semantic_unit_id for item in self.decisions) != (
            self.plan.semantic_unit_plan.semantic_unit_ids
        ):
            raise ValueError(
                "controlled decisions must exactly cover semantic units in order"
            )


def build_controlled_semantic_unit_plan(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    current_plan: SemanticUnitPlan,
    candidate: SemanticUnitPartitionCandidate,
) -> SemanticUnitPlan:
    """Build an inactive multi-unit plan from one verified candidate.

    The production planner never calls this function. The input must be the
    current 1:1 full-span plan, and only the one candidate owner is expanded.
    """

    _validate_input_types_and_bindings(source_ir, tree, current_plan)
    if not isinstance(candidate, SemanticUnitPartitionCandidate):
        raise TypeError("candidate must be a SemanticUnitPartitionCandidate")
    if (
        candidate.disposition is not SemanticUnitPartitionDisposition.CANDIDATE
        or candidate.candidate_unitizer_version
        != CONTINUOUS_EXPLICIT_LIST_CANDIDATE_UNITIZER_VERSION
    ):
        raise ControlledSemanticUnitAdapterRejected(
            ControlledSemanticUnitAdapterRejectionCode.CANDIDATE_NOT_ELIGIBLE,
            "Only the controlled continuous-list candidate may be adapted.",
        )
    if tuple(
        item.owner_structure_node_id for item in current_plan.units
    ) != current_plan.scope_owner_structure_node_ids or any(
        item.unit_kind is not SemanticUnitKind.OWNER_FULL_SPAN
        or item.owner_unit_index != 0
        for item in current_plan.units
    ):
        raise ControlledSemanticUnitAdapterRejected(
            ControlledSemanticUnitAdapterRejectionCode.INPUT_MISMATCH,
            "The controlled adapter requires the current 1:1 full-span plan.",
        )

    current_units_by_owner = {
        item.owner_structure_node_id: item for item in current_plan.units
    }
    current_owner_unit = current_units_by_owner.get(candidate.owner_structure_node_id)
    if current_owner_unit is None or (
        candidate.owner_semantic_unit_id != current_owner_unit.semantic_unit_id
        or candidate.parent_owner_structure_node_id
        != current_owner_unit.parent_owner_structure_node_id
        or candidate.parent_semantic_unit_id
        != current_owner_unit.parent_semantic_unit_id
        or candidate.owner_source_spans != current_owner_unit.source_spans
    ):
        raise ControlledSemanticUnitAdapterRejected(
            ControlledSemanticUnitAdapterRejectionCode.INPUT_MISMATCH,
            "The partition candidate does not bind to the supplied full-span unit.",
        )

    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    owner = nodes_by_id.get(candidate.owner_structure_node_id)
    if (
        owner is None
        or owner.source_order != current_owner_unit.source_order
        or owner.parent_structure_node_id
        != current_owner_unit.parent_owner_structure_node_id
    ):
        raise ControlledSemanticUnitAdapterRejected(
            ControlledSemanticUnitAdapterRejectionCode.INPUT_MISMATCH,
            "The candidate owner no longer matches StructureTree.",
        )
    if tuple(item.source_ref for item in candidate.owner_source_spans) != (
        owner.effective_semantic_source_refs
    ):
        raise ControlledSemanticUnitAdapterRejected(
            ControlledSemanticUnitAdapterRejectionCode.INVALID_GROUNDING,
            "The candidate owner spans do not cover Structure ownership.",
        )

    text_index = _SourceTextIndex(source_ir)
    owner_span = candidate.owner_source_spans[0]
    try:
        owner_text = text_index.resolve(owner_span.source_ref)
    except KeyError as exc:
        raise ControlledSemanticUnitAdapterRejected(
            ControlledSemanticUnitAdapterRejectionCode.INVALID_GROUNDING,
            "The candidate source text cannot be resolved.",
        ) from exc
    if owner_span.start_offset != 0 or owner_span.end_offset != len(owner_text):
        raise ControlledSemanticUnitAdapterRejected(
            ControlledSemanticUnitAdapterRejectionCode.INVALID_GROUNDING,
            "The candidate owner interval does not match the source text view.",
        )
    if any(
        span.start_offset < owner_span.start_offset
        or span.end_offset > owner_span.end_offset
        for span in candidate.proposed_unit_spans
    ):
        raise ControlledSemanticUnitAdapterRejected(
            ControlledSemanticUnitAdapterRejectionCode.INVALID_PARTITION,
            "The proposed units exceed the owner interval.",
        )

    unit_shapes_by_owner: dict[
        str,
        tuple[
            tuple[
                SemanticUnitKind,
                tuple[SemanticGroundingSpan, ...],
            ],
            ...,
        ],
    ] = {}
    for unit in current_plan.units:
        if unit.owner_structure_node_id == candidate.owner_structure_node_id:
            unit_shapes_by_owner[unit.owner_structure_node_id] = tuple(
                (
                    SemanticUnitKind.CONTINUOUS_EXPLICIT_LIST_ITEM,
                    (span,),
                )
                for span in candidate.proposed_unit_spans
            )
        else:
            unit_shapes_by_owner[unit.owner_structure_node_id] = (
                (SemanticUnitKind.OWNER_FULL_SPAN, unit.source_spans),
            )

    unit_ids_by_owner: dict[str, tuple[str, ...]] = {}
    for owner_id in current_plan.scope_owner_structure_node_ids:
        unit_ids_by_owner[owner_id] = tuple(
            stable_semantic_unit_id(
                source_ir_id=source_ir.source_ir_id,
                structure_snapshot_id=tree.structure_snapshot_id,
                owner_structure_node_id=owner_id,
                unitizer_version=CONTROLLED_EXPLICIT_LIST_UNITIZER_VERSION,
                ordered_source_spans=source_spans,
            )
            for _kind, source_spans in unit_shapes_by_owner[owner_id]
        )

    units: list[SemanticUnit] = []
    unit_order = 0
    for owner_id in current_plan.scope_owner_structure_node_ids:
        previous_unit = current_units_by_owner[owner_id]
        parent_owner_id = previous_unit.parent_owner_structure_node_id
        parent_unit_ids = unit_ids_by_owner.get(parent_owner_id or "")
        if parent_unit_ids is not None and len(parent_unit_ids) != 1:
            raise ControlledSemanticUnitAdapterRejected(
                ControlledSemanticUnitAdapterRejectionCode.INVALID_PARENT,
                "A split owner cannot serve as an in-scope semantic parent.",
            )
        parent_unit_id = parent_unit_ids[0] if parent_unit_ids is not None else None
        for owner_unit_index, (
            (unit_kind, source_spans),
            semantic_unit_id,
        ) in enumerate(
            zip(
                unit_shapes_by_owner[owner_id],
                unit_ids_by_owner[owner_id],
                strict=True,
            )
        ):
            units.append(
                SemanticUnit(
                    semantic_unit_id=semantic_unit_id,
                    owner_structure_node_id=owner_id,
                    parent_owner_structure_node_id=parent_owner_id,
                    parent_semantic_unit_id=parent_unit_id,
                    source_order=previous_unit.source_order,
                    unit_order=unit_order,
                    owner_unit_index=owner_unit_index,
                    unit_kind=unit_kind,
                    source_spans=source_spans,
                )
            )
            unit_order += 1

    semantic_unit_ids = tuple(item.semantic_unit_id for item in units)
    return SemanticUnitPlan(
        semantic_unit_plan_id=stable_semantic_unit_plan_id(
            source_ir_id=source_ir.source_ir_id,
            structure_snapshot_id=tree.structure_snapshot_id,
            unitizer_version=CONTROLLED_EXPLICIT_LIST_UNITIZER_VERSION,
            scope_owner_structure_node_ids=(
                current_plan.scope_owner_structure_node_ids
            ),
            semantic_unit_ids=semantic_unit_ids,
        ),
        source_ir_id=source_ir.source_ir_id,
        structure_snapshot_id=tree.structure_snapshot_id,
        unitizer_version=CONTROLLED_EXPLICIT_LIST_UNITIZER_VERSION,
        scope_owner_structure_node_ids=current_plan.scope_owner_structure_node_ids,
        units=tuple(units),
    )


def plan_controlled_semantic_unit_decisions(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantic_unit_plan: SemanticUnitPlan,
) -> ControlledSemanticUnitDecisionPlan:
    """Create one source-bound provider request for every controlled unit."""

    _validate_input_types_and_bindings(source_ir, tree, semantic_unit_plan)
    if semantic_unit_plan.unitizer_version != (
        CONTROLLED_EXPLICIT_LIST_UNITIZER_VERSION
    ):
        raise ControlledSemanticUnitAdapterRejected(
            ControlledSemanticUnitAdapterRejectionCode.INPUT_MISMATCH,
            "Controlled decisions require the controlled explicit-list plan.",
        )
    owner_counts: dict[str, int] = {}
    for unit in semantic_unit_plan.units:
        owner_counts[unit.owner_structure_node_id] = (
            owner_counts.get(unit.owner_structure_node_id, 0) + 1
        )
    if not any(count > 1 for count in owner_counts.values()):
        raise ControlledSemanticUnitAdapterRejected(
            ControlledSemanticUnitAdapterRejectionCode.INVALID_PARTITION,
            "Controlled decisions require at least one multi-unit owner.",
        )

    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    text_index = _SourceTextIndex(source_ir)
    requests: list[SemanticUnitDecisionRequest] = []
    for unit in semantic_unit_plan.units:
        owner = nodes_by_id.get(unit.owner_structure_node_id)
        if (
            owner is None
            or owner.source_order != unit.source_order
            or owner.parent_structure_node_id != unit.parent_owner_structure_node_id
            or any(
                span.source_ref not in owner.effective_semantic_source_refs
                for span in unit.source_spans
            )
        ):
            raise ControlledSemanticUnitAdapterRejected(
                ControlledSemanticUnitAdapterRejectionCode.INVALID_GROUNDING,
                "A controlled unit no longer matches its Structure owner.",
            )
        evidence: list[SemanticUnitDecisionEvidence] = []
        for span in unit.source_spans:
            try:
                source_text = text_index.resolve(span.source_ref)
            except KeyError as exc:
                raise ControlledSemanticUnitAdapterRejected(
                    ControlledSemanticUnitAdapterRejectionCode.INVALID_GROUNDING,
                    "A controlled unit source text cannot be resolved.",
                ) from exc
            if (
                span.end_offset > len(source_text)
                or span.end_offset <= span.start_offset
            ):
                raise ControlledSemanticUnitAdapterRejected(
                    ControlledSemanticUnitAdapterRejectionCode.INVALID_GROUNDING,
                    "A controlled unit interval is outside its source text.",
                )
            text = source_text[span.start_offset : span.end_offset]
            evidence.append(
                SemanticUnitDecisionEvidence(
                    grounding_span=span,
                    text=text,
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )
            )
        evidence_tuple = tuple(evidence)
        request_id = stable_semantic_unit_decision_request_id(
            source_ir_id=source_ir.source_ir_id,
            source_sha256=source_ir.source_sha256,
            structure_snapshot_id=tree.structure_snapshot_id,
            semantic_unit_plan_id=semantic_unit_plan.semantic_unit_plan_id,
            unitizer_version=semantic_unit_plan.unitizer_version,
            semantic_unit=unit,
            evidence=evidence_tuple,
        )
        requests.append(
            SemanticUnitDecisionRequest(
                decision_request_id=request_id,
                source_ir_id=source_ir.source_ir_id,
                source_sha256=source_ir.source_sha256,
                structure_snapshot_id=tree.structure_snapshot_id,
                semantic_unit_plan_id=semantic_unit_plan.semantic_unit_plan_id,
                unitizer_version=semantic_unit_plan.unitizer_version,
                semantic_unit=unit,
                evidence=evidence_tuple,
            )
        )
    return ControlledSemanticUnitDecisionPlan(
        semantic_unit_plan=semantic_unit_plan,
        requests=tuple(requests),
    )


def classify_controlled_semantic_units(
    plan: ControlledSemanticUnitDecisionPlan,
    provider: ControlledSemanticUnitProviderPort,
) -> ControlledSemanticUnitDecisionExecution:
    """Execute the inactive one-request/one-decision unit contract."""

    if not isinstance(plan, ControlledSemanticUnitDecisionPlan):
        raise TypeError("plan must be a ControlledSemanticUnitDecisionPlan")
    if not isinstance(provider, ControlledSemanticUnitProviderPort):
        raise TypeError("provider must implement ControlledSemanticUnitProviderPort")

    decisions: list[SemanticUnitDecision] = []
    for request in plan.requests:
        try:
            decision = provider.classify_semantic_unit(request)
        except Exception as exc:
            raise ControlledSemanticUnitAdapterRejected(
                ControlledSemanticUnitAdapterRejectionCode.PROVIDER_FAILURE,
                "The controlled unit provider failed.",
            ) from exc
        if not isinstance(decision, SemanticUnitDecision):
            raise ControlledSemanticUnitAdapterRejected(
                ControlledSemanticUnitAdapterRejectionCode.PROVIDER_RESULT_MISMATCH,
                "The controlled provider returned an invalid decision type.",
            )
        if (
            decision.decision_request_id != request.decision_request_id
            or decision.semantic_unit_id != request.semantic_unit_id
            or decision.owner_structure_node_id != request.owner_structure_node_id
        ):
            raise ControlledSemanticUnitAdapterRejected(
                ControlledSemanticUnitAdapterRejectionCode.PROVIDER_RESULT_MISMATCH,
                "The controlled provider decision does not match its request.",
            )
        allowed_evidence_ids = tuple(
            item.grounding_span.grounding_span_id for item in request.evidence
        )
        observed_evidence_ids = decision.evidence_grounding_span_ids
        if not set(observed_evidence_ids).issubset(
            allowed_evidence_ids
        ) or observed_evidence_ids != tuple(
            item for item in allowed_evidence_ids if item in set(observed_evidence_ids)
        ):
            raise ControlledSemanticUnitAdapterRejected(
                ControlledSemanticUnitAdapterRejectionCode.PROVIDER_RESULT_MISMATCH,
                "The controlled provider selected evidence outside its unit.",
            )
        decisions.append(decision)

    return ControlledSemanticUnitDecisionExecution(
        plan=plan,
        provider_id=provider.provider_id,
        provider_version=provider.provider_version,
        model_id=provider.model_id,
        decisions=tuple(decisions),
    )


def _validate_input_types_and_bindings(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantic_unit_plan: SemanticUnitPlan,
) -> None:
    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if not isinstance(tree, StructureTreeSnapshot):
        raise TypeError("tree must be a StructureTreeSnapshot")
    if not isinstance(semantic_unit_plan, SemanticUnitPlan):
        raise TypeError("semantic_unit_plan must be a SemanticUnitPlan")
    if (
        tree.source_ir_id != source_ir.source_ir_id
        or semantic_unit_plan.source_ir_id != source_ir.source_ir_id
        or semantic_unit_plan.structure_snapshot_id != tree.structure_snapshot_id
    ):
        raise ControlledSemanticUnitAdapterRejected(
            ControlledSemanticUnitAdapterRejectionCode.INPUT_MISMATCH,
            "Controlled semantic-unit inputs are not bound to one snapshot.",
        )


class _SourceTextIndex:
    def __init__(self, source_ir: SourceIRSnapshot) -> None:
        self.source_nodes = {
            item.source_node_id: item.raw_text for item in source_ir.nodes
        }
        self.source_cells: dict[str, str] = {
            cell.cell_id: cell.raw_text
            for source_node in source_ir.nodes
            if source_node.table_payload is not None
            for cell in source_node.table_payload.cells
            if isinstance(cell, TableCellIR)
        }
        self.visual_observations: dict[str, str] = {
            observation.observation_id: observation.raw_text
            for snapshot in source_ir.visual_text_snapshots
            for observation in snapshot.observations
            if isinstance(observation, VisualTextObservation)
        }

    def resolve(self, source_ref: StructureSourceRef) -> str:
        if source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
            return self.source_nodes[source_ref.source_id]
        if source_ref.kind is StructureSourceRefKind.TABLE_CELL:
            return self.source_cells[source_ref.source_id]
        return self.visual_observations[source_ref.source_id]


__all__ = [
    "CONTROLLED_EXPLICIT_LIST_UNITIZER_VERSION",
    "ControlledSemanticUnitAdapterRejected",
    "ControlledSemanticUnitAdapterRejectionCode",
    "ControlledSemanticUnitDecisionExecution",
    "ControlledSemanticUnitDecisionPlan",
    "build_controlled_semantic_unit_plan",
    "classify_controlled_semantic_units",
    "plan_controlled_semantic_unit_decisions",
]
