from __future__ import annotations

import hashlib
import json
from collections import Counter
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.domain.extraction.semantics import SemanticClass
from app.ports.semantic_provider import (
    SemanticClassificationRequest,
    SemanticDecision,
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class NativeSemanticConsensusStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class NativeSemanticTargetConsensusV1(_StrictFrozenModel):
    target_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    consensus_count: StrictInt = Field(ge=0)
    run_count: StrictInt = Field(ge=1)
    consensus_decision_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    candidate_decision_sha256s: tuple[str, ...]

    @model_validator(mode="after")
    def validate_counts(self) -> NativeSemanticTargetConsensusV1:
        if self.consensus_count > self.run_count:
            raise ValueError("semantic consensus count cannot exceed run_count")
        if self.candidate_decision_sha256s != tuple(
            sorted(set(self.candidate_decision_sha256s))
        ):
            raise ValueError("semantic candidate hashes must be unique and sorted")
        return self


class NativeSemanticConsensusReportV1(_StrictFrozenModel):
    schema_version: str = "native-semantic-consensus/v1"
    status: NativeSemanticConsensusStatus
    run_count: StrictInt = Field(ge=1)
    consensus_threshold: StrictInt = Field(ge=1)
    resolved_target_count: StrictInt = Field(ge=0)
    unresolved_target_count: StrictInt = Field(ge=0)
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    targets: tuple[NativeSemanticTargetConsensusV1, ...]
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @model_validator(mode="after")
    def validate_report(self) -> NativeSemanticConsensusReportV1:
        if self.consensus_threshold != self.run_count // 2 + 1:
            raise ValueError("semantic consensus requires a strict majority")
        if self.resolved_target_count + self.unresolved_target_count != len(
            self.targets
        ):
            raise ValueError("semantic consensus target counts do not close")
        expected = (
            NativeSemanticConsensusStatus.COMPLETE
            if self.unresolved_target_count == 0
            else NativeSemanticConsensusStatus.PARTIAL
        )
        if self.status is not expected:
            raise ValueError(
                "semantic consensus status does not match unresolved targets"
            )
        if self.is_human_gold or self.is_serving_activation_gate:
            raise ValueError("semantic consensus cannot be Gold or a serving gate")
        return self


def validate_native_semantic_decisions(
    request: SemanticClassificationRequest,
    decisions: tuple[SemanticDecision, ...],
) -> None:
    targets = tuple(item.target_structure_node_id for item in decisions)
    if targets != request.scope_structure_node_ids:
        raise ValueError(
            "semantic decisions must exactly cover request targets in order"
        )
    nodes_by_target = {item.target_structure_node_id: item for item in request.nodes}
    for decision in decisions:
        node = nodes_by_target[decision.target_structure_node_id]
        allowed_refs = {item.source_ref for item in node.evidence}
        if not set(decision.evidence_source_refs).issubset(allowed_refs):
            raise ValueError("semantic decision referenced evidence outside its target")
        if (
            decision.semantic_class is SemanticClass.REQUIREMENT
            and not decision.evidence_source_refs
        ):
            raise ValueError("requirement decisions require source-bound evidence")
        if (
            decision.semantic_class is not SemanticClass.UNRESOLVED
            and node.evidence
            and not decision.evidence_source_refs
        ):
            raise ValueError(
                "terminal semantic decisions require source-bound evidence"
            )


def build_native_semantic_consensus(
    request: SemanticClassificationRequest,
    runs: tuple[tuple[SemanticDecision, ...], ...],
) -> tuple[tuple[SemanticDecision, ...], NativeSemanticConsensusReportV1]:
    if not runs:
        raise ValueError("semantic decision aggregation requires at least one run")
    for decisions in runs:
        validate_native_semantic_decisions(request, decisions)
    threshold = len(runs) // 2 + 1
    consensus_decisions: list[SemanticDecision] = []
    target_reports: list[NativeSemanticTargetConsensusV1] = []
    unresolved_count = 0
    for target_index, target_id in enumerate(request.scope_structure_node_ids):
        decisions = [run[target_index] for run in runs]
        fingerprints = [_decision_fingerprint(item) for item in decisions]
        counts = Counter(fingerprints)
        fingerprint, count = counts.most_common(1)[0]
        if count >= threshold:
            selected = decisions[fingerprints.index(fingerprint)]
            consensus_sha = fingerprint
        else:
            unresolved_count += 1
            node = request.nodes[target_index]
            selected = SemanticDecision(
                target_structure_node_id=target_id,
                semantic_class=SemanticClass.UNRESOLVED,
                evidence_source_refs=node.target_source_refs,
                reason_codes=("semantic_consensus_unresolved",),
            )
            consensus_sha = None
        consensus_decisions.append(selected)
        target_reports.append(
            NativeSemanticTargetConsensusV1(
                target_structure_node_id=target_id,
                consensus_count=(count if consensus_sha is not None else 0),
                run_count=len(runs),
                consensus_decision_sha256=consensus_sha,
                candidate_decision_sha256s=tuple(sorted(set(fingerprints))),
            )
        )
    decision_sha = _canonical_hash(
        [item.model_dump(mode="json") for item in consensus_decisions]
    )
    report = NativeSemanticConsensusReportV1(
        status=(
            NativeSemanticConsensusStatus.COMPLETE
            if unresolved_count == 0
            else NativeSemanticConsensusStatus.PARTIAL
        ),
        run_count=len(runs),
        consensus_threshold=threshold,
        resolved_target_count=len(consensus_decisions) - unresolved_count,
        unresolved_target_count=unresolved_count,
        decision_sha256=decision_sha,
        targets=tuple(target_reports),
    )
    return tuple(consensus_decisions), report


def _decision_fingerprint(decision: SemanticDecision) -> str:
    return _canonical_hash(
        {
            "semantic_class": decision.semantic_class.value,
            "requirement_subtype": (
                decision.requirement_subtype.value
                if decision.requirement_subtype
                else None
            ),
            "is_continuation_fragment": decision.is_continuation_fragment,
            "evidence_source_refs": [
                item.model_dump(mode="json") for item in decision.evidence_source_refs
            ],
        }
    )


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "NativeSemanticConsensusReportV1",
    "NativeSemanticConsensusStatus",
    "NativeSemanticTargetConsensusV1",
    "build_native_semantic_consensus",
    "validate_native_semantic_decisions",
]
