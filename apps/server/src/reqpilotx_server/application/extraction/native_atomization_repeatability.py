from __future__ import annotations

import hashlib
import json
from collections import Counter
from enum import Enum
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from reqpilotx_server.application.extraction.native_atomization_reference import (
    NativeAtomizationContextDisposition,
    NativeAtomizationContextRangeV1,
    NativeAtomizationReferenceV1,
    NativeAtomizationReferenceStatus,
    validate_native_atomization_reference,
)
from reqpilotx_server.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderRequest,
    RequirementAtomizationRange,
)


NATIVE_ATOMIZATION_REPEATABILITY_SCHEMA_VERSION = "native-atomization-repeatability/v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class NativeAtomizationRepeatabilityStatus(str, Enum):
    STABLE = "STABLE"
    UNSTABLE = "UNSTABLE"


class NativeAtomizationRunFingerprintV1(_StrictFrozenModel):
    run_label: str = Field(min_length=1, max_length=120)
    obligation_count: StrictInt = Field(ge=1)
    obligation_source_sha256s: tuple[str, ...]
    decision_skeleton_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_count(self) -> NativeAtomizationRunFingerprintV1:
        if len(self.obligation_source_sha256s) != self.obligation_count:
            raise ValueError("obligation fingerprints must match obligation_count")
        return self


class NativeAtomizationRepeatabilityReportV1(_StrictFrozenModel):
    schema_version: str = NATIVE_ATOMIZATION_REPEATABILITY_SCHEMA_VERSION
    status: NativeAtomizationRepeatabilityStatus
    normalized_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_count: StrictInt = Field(ge=2)
    exact_decision_skeleton_match: bool
    stable_obligation_count: StrictInt = Field(ge=0)
    union_obligation_count: StrictInt = Field(ge=1)
    consensus_threshold: StrictInt = Field(ge=2)
    consensus_obligation_count: StrictInt = Field(ge=0)
    consensus_obligation_source_sha256s: tuple[str, ...]
    non_consensus_obligation_source_sha256s: tuple[str, ...]
    unstable_obligation_source_sha256s: tuple[str, ...]
    runs: tuple[NativeAtomizationRunFingerprintV1, ...]
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @model_validator(mode="after")
    def validate_report(self) -> NativeAtomizationRepeatabilityReportV1:
        if len(self.runs) != self.run_count:
            raise ValueError("repeatability runs must match run_count")
        labels = tuple(item.run_label for item in self.runs)
        if len(labels) != len(set(labels)):
            raise ValueError("repeatability run labels must be unique")
        expected_exact = len({item.decision_skeleton_sha256 for item in self.runs}) == 1
        if self.exact_decision_skeleton_match is not expected_exact:
            raise ValueError("decision skeleton match does not match run fingerprints")
        expected_status = (
            NativeAtomizationRepeatabilityStatus.STABLE
            if expected_exact
            else NativeAtomizationRepeatabilityStatus.UNSTABLE
        )
        if self.status is not expected_status:
            raise ValueError("repeatability status does not match skeleton stability")
        if self.unstable_obligation_source_sha256s != tuple(
            sorted(set(self.unstable_obligation_source_sha256s))
        ):
            raise ValueError("unstable obligation hashes must be unique and sorted")
        if self.consensus_threshold != self.run_count // 2 + 1:
            raise ValueError("consensus threshold must be a strict run majority")
        for values in (
            self.consensus_obligation_source_sha256s,
            self.non_consensus_obligation_source_sha256s,
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError("consensus hashes must be unique and sorted")
        if (
            len(self.consensus_obligation_source_sha256s)
            != self.consensus_obligation_count
        ):
            raise ValueError("consensus hashes must match consensus_obligation_count")
        if self.is_human_gold or self.is_serving_activation_gate:
            raise ValueError("repeatability reports cannot be Gold or serving gates")
        return self


def evaluate_native_atomization_repeatability(
    runs: tuple[
        tuple[
            str,
            RequirementAtomizationProviderRequest,
            NativeAtomizationReferenceV1,
        ],
        ...,
    ],
) -> NativeAtomizationRepeatabilityReportV1:
    if len(runs) < 2:
        raise ValueError("native atomization repeatability requires at least two runs")
    source_hashes: set[str] = set()
    fingerprints: list[NativeAtomizationRunFingerprintV1] = []
    obligation_sets: list[set[str]] = []
    for run_label, request, reference in runs:
        validate_native_atomization_reference(request, reference)
        normalized_source = _normalize_source(
            " ".join(segment.source_text for segment in request.source_segments)
        )
        source_hashes.add(hashlib.sha256(normalized_source.encode("utf-8")).hexdigest())
        obligation_hashes = tuple(
            _obligation_source_hash(request, obligation.source_ranges)
            for obligation in reference.obligations
        )
        fingerprints.append(
            NativeAtomizationRunFingerprintV1(
                run_label=run_label,
                obligation_count=len(obligation_hashes),
                obligation_source_sha256s=obligation_hashes,
                decision_skeleton_sha256=_canonical_hash(obligation_hashes),
            )
        )
        obligation_sets.append(set(obligation_hashes))
    if len(source_hashes) != 1:
        raise ValueError("repeatability runs must bind the same normalized source")
    stable_hashes = set.intersection(*obligation_sets)
    union_hashes = set.union(*obligation_sets)
    threshold = len(fingerprints) // 2 + 1
    counts = Counter(
        source_hash
        for obligation_set in obligation_sets
        for source_hash in obligation_set
    )
    consensus_hashes = tuple(
        sorted(
            source_hash for source_hash, count in counts.items() if count >= threshold
        )
    )
    non_consensus_hashes = tuple(sorted(union_hashes - set(consensus_hashes)))
    unstable_hashes = tuple(sorted(union_hashes - stable_hashes))
    exact = len({item.decision_skeleton_sha256 for item in fingerprints}) == 1
    return NativeAtomizationRepeatabilityReportV1(
        status=(
            NativeAtomizationRepeatabilityStatus.STABLE
            if exact
            else NativeAtomizationRepeatabilityStatus.UNSTABLE
        ),
        normalized_source_sha256=next(iter(source_hashes)),
        run_count=len(fingerprints),
        exact_decision_skeleton_match=exact,
        stable_obligation_count=len(stable_hashes),
        union_obligation_count=len(union_hashes),
        consensus_threshold=threshold,
        consensus_obligation_count=len(consensus_hashes),
        consensus_obligation_source_sha256s=consensus_hashes,
        non_consensus_obligation_source_sha256s=non_consensus_hashes,
        unstable_obligation_source_sha256s=unstable_hashes,
        runs=tuple(fingerprints),
    )


def build_native_atomization_consensus(
    canonical_request: RequirementAtomizationProviderRequest,
    canonical_reference: NativeAtomizationReferenceV1,
    report: NativeAtomizationRepeatabilityReportV1,
) -> NativeAtomizationReferenceV1:
    validate_native_atomization_reference(canonical_request, canonical_reference)
    normalized_source = _normalize_source(
        " ".join(segment.source_text for segment in canonical_request.source_segments)
    )
    if hashlib.sha256(normalized_source.encode("utf-8")).hexdigest() != (
        report.normalized_source_sha256
    ):
        raise ValueError("consensus reference does not match repeatability source")
    consensus_hashes = set(report.consensus_obligation_source_sha256s)
    consensus_obligations = []
    unresolved_ranges: list[NativeAtomizationContextRangeV1] = []
    for obligation in canonical_reference.obligations:
        source_hash = _obligation_source_hash(
            canonical_request,
            obligation.source_ranges,
        )
        if source_hash in consensus_hashes:
            consensus_obligations.append(obligation)
            continue
        unresolved_ranges.extend(
            NativeAtomizationContextRangeV1(
                source_range=source_range,
                disposition=NativeAtomizationContextDisposition.UNRESOLVED,
                reason_codes=("non_consensus_atomization_range",),
            )
            for source_range in obligation.source_ranges
        )
    if not consensus_obligations:
        raise ValueError("atomization consensus produced no safe obligations")
    diagnostics = ["repeatability_consensus_applied"]
    if unresolved_ranges:
        diagnostics.append("non_consensus_ranges_unresolved")
    consensus = NativeAtomizationReferenceV1(
        lineage_sha256=canonical_reference.lineage_sha256,
        review_node_id=canonical_reference.review_node_id,
        status=(
            NativeAtomizationReferenceStatus.PARTIAL
            if unresolved_ranges
            else canonical_reference.status
        ),
        obligations=tuple(consensus_obligations),
        context_ranges=tuple((*canonical_reference.context_ranges, *unresolved_ranges)),
        diagnostic_codes=tuple(sorted(diagnostics)),
    )
    validate_native_atomization_reference(canonical_request, consensus)
    return consensus


def select_native_atomization_consensus_canonical(
    request: RequirementAtomizationProviderRequest,
    references: tuple[NativeAtomizationReferenceV1, ...],
    report: NativeAtomizationRepeatabilityReportV1,
) -> NativeAtomizationReferenceV1:
    if len(references) != report.run_count:
        raise ValueError("canonical selection requires every repeatability run")
    consensus_hashes = set(report.consensus_obligation_source_sha256s)
    if not consensus_hashes:
        raise ValueError("canonical selection requires consensus obligations")
    scored: list[tuple[int, int, int, NativeAtomizationReferenceV1]] = []
    for index, reference in enumerate(references):
        validate_native_atomization_reference(request, reference)
        source_hashes = {
            _obligation_source_hash(request, obligation.source_ranges)
            for obligation in reference.obligations
        }
        scored.append(
            (
                len(source_hashes.intersection(consensus_hashes)),
                -sum(
                    item.disposition is NativeAtomizationContextDisposition.UNRESOLVED
                    for item in reference.context_ranges
                ),
                -index,
                reference,
            )
        )
    selected = max(scored, key=lambda item: item[:3])[3]
    if not any(
        _obligation_source_hash(request, obligation.source_ranges) in consensus_hashes
        for obligation in selected.obligations
    ):
        raise ValueError("canonical selection found no safe obligation")
    return selected


def _obligation_source_hash(
    request: RequirementAtomizationProviderRequest,
    ranges: tuple[RequirementAtomizationRange, ...],
) -> str:
    parts: list[str] = []
    for source_range in ranges:
        segment = request.source_segments[source_range.segment_index]
        parts.append(
            segment.source_text[source_range.start_offset : source_range.end_offset]
        )
    normalized = _normalize_source(" ".join(parts))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize_source(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "NativeAtomizationRepeatabilityReportV1",
    "NativeAtomizationRepeatabilityStatus",
    "NativeAtomizationRunFingerprintV1",
    "build_native_atomization_consensus",
    "evaluate_native_atomization_repeatability",
    "select_native_atomization_consensus_canonical",
]
