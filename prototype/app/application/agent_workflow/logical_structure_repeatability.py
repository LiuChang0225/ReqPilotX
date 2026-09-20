from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalStructureProposalV1,
    D2LogicalStructureStatus,
    D2LogicalStructureValidationReceiptV1,
    D2PhysicalTableDispositionKind,
    D2SourceEvidenceDispositionKind,
    D2SourceHandleKind,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1


LOGICAL_STRUCTURE_REPEATABILITY_FINGERPRINT_SCHEMA_VERSION = (
    "logical-structure-repeatability-fingerprint/v1"
)
LOGICAL_STRUCTURE_REPEATABILITY_EVALUATION_SCHEMA_VERSION = (
    "logical-structure-repeatability-evaluation/v1"
)
LOGICAL_STRUCTURE_REPEATABILITY_ALGORITHM_VERSION = (
    "source-ordered-content-free-profile/v1"
)
_MULTI_REGION_LIMITATION = (
    "PHYSICAL_TABLE_MULTI_REGION_UNREPRESENTABLE_IN_LOGICAL_STRUCTURE_PROPOSAL_V1"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class LogicalStructureRepeatabilityVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class LogicalStructureRepeatabilityDriftKind(StrEnum):
    IDENTITY_DRIFT = "IDENTITY_DRIFT"
    COVERAGE_DRIFT = "COVERAGE_DRIFT"
    OBJECT_BOUNDARY_DRIFT = "OBJECT_BOUNDARY_DRIFT"
    TABLE_GROUPING_DRIFT = "TABLE_GROUPING_DRIFT"
    STATUS_DRIFT = "STATUS_DRIFT"


_DRIFT_ORDER = tuple(LogicalStructureRepeatabilityDriftKind)


class LogicalStructureTablePartitionMode(StrEnum):
    """Current capability plus a reserved future cell-partition mode."""

    WHOLE_TABLE_ONLY = "WHOLE_TABLE_ONLY"
    CELL_PARTITION = "CELL_PARTITION"


class LogicalStructureTableRegionIdentityMode(StrEnum):
    WHOLE_PHYSICAL_TABLE = "WHOLE_PHYSICAL_TABLE"
    CELL_PARTITION_SHA256 = "CELL_PARTITION_SHA256"


class LogicalStructurePageSpanV1(_StrictFrozenModel):
    start_page: StrictInt = Field(ge=1)
    end_page: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def validate_span(self) -> LogicalStructurePageSpanV1:
        if self.end_page < self.start_page:
            raise ValueError("page span end must not precede its start")
        return self


class LogicalStructureCoverageRangeV1(_StrictFrozenModel):
    """Content-free union range over one source handle."""

    source_handle_ordinal: StrictInt = Field(ge=0)
    source_order: StrictInt = Field(ge=0)
    handle_kind: D2SourceHandleKind
    page_spans: tuple[LogicalStructurePageSpanV1, ...] = Field(min_length=1)
    start_offset: StrictInt | None = Field(default=None, ge=0)
    end_offset: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> LogicalStructureCoverageRangeV1:
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("coverage offsets must be supplied together")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("coverage range must be non-empty")
        return self


class LogicalStructureCoverageProfileV1(_StrictFrozenModel):
    source_ranges: tuple[LogicalStructureCoverageRangeV1, ...] = ()
    covered_physical_table_ordinals: tuple[StrictInt, ...] = ()

    @model_validator(mode="after")
    def validate_profile(self) -> LogicalStructureCoverageProfileV1:
        if self.covered_physical_table_ordinals != tuple(
            sorted(set(self.covered_physical_table_ordinals))
        ):
            raise ValueError("covered physical-table ordinals must be canonical")
        range_order = tuple(
            (
                item.source_handle_ordinal,
                -1 if item.start_offset is None else item.start_offset,
                -1 if item.end_offset is None else item.end_offset,
            )
            for item in self.source_ranges
        )
        if range_order != tuple(sorted(range_order)):
            raise ValueError("coverage ranges must use canonical source order")
        return self


class LogicalStructureObjectProfileV1(_StrictFrozenModel):
    logical_object_ordinal: StrictInt = Field(ge=0)
    object_kind: D2LogicalObjectKind
    parent_object_ordinal: StrictInt | None = Field(default=None, ge=0)
    source_order: StrictInt = Field(ge=0)
    page_spans: tuple[LogicalStructurePageSpanV1, ...] = Field(min_length=1)
    source_segment_count: StrictInt = Field(ge=0)
    logical_table_scope_ordinals: tuple[StrictInt, ...] = ()
    boundary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_profile(self) -> LogicalStructureObjectProfileV1:
        if self.parent_object_ordinal == self.logical_object_ordinal:
            raise ValueError("an object profile cannot own itself")
        if self.logical_table_scope_ordinals != tuple(
            sorted(set(self.logical_table_scope_ordinals))
        ):
            raise ValueError("logical-table scope ordinals must be canonical")
        return self


class LogicalStructurePhysicalTableRegionV1(_StrictFrozenModel):
    """Region-ready identity; proposal v1 can emit only a whole-table region."""

    region_id: str = Field(pattern=r"^logical-structure-table-region-[0-9a-f]{32}$")
    physical_table_ordinal: StrictInt = Field(ge=0)
    region_ordinal_within_table: StrictInt = Field(ge=0)
    identity_mode: LogicalStructureTableRegionIdentityMode
    cell_partition_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_region(self) -> LogicalStructurePhysicalTableRegionV1:
        if (
            self.identity_mode
            is LogicalStructureTableRegionIdentityMode.WHOLE_PHYSICAL_TABLE
        ):
            if self.region_ordinal_within_table != 0 or self.cell_partition_sha256:
                raise ValueError("whole-table regions cannot carry a cell partition")
        elif self.cell_partition_sha256 is None:
            raise ValueError("cell-partition regions require a partition SHA")
        expected_id = stable_logical_structure_table_region_id(
            physical_table_ordinal=self.physical_table_ordinal,
            region_ordinal_within_table=self.region_ordinal_within_table,
            identity_mode=self.identity_mode,
            cell_partition_sha256=self.cell_partition_sha256,
        )
        if self.region_id != expected_id:
            raise ValueError("table-region ID does not bind its partition identity")
        return self


class LogicalStructureTableScopeProfileV1(_StrictFrozenModel):
    logical_scope_ordinal: StrictInt = Field(ge=0)
    logical_object_ordinal: StrictInt = Field(ge=0)
    partition_mode: LogicalStructureTablePartitionMode
    physical_table_ordinals: tuple[StrictInt, ...] = Field(min_length=1)
    regions: tuple[LogicalStructurePhysicalTableRegionV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scope(self) -> LogicalStructureTableScopeProfileV1:
        if self.physical_table_ordinals != tuple(
            sorted(set(self.physical_table_ordinals))
        ):
            raise ValueError("physical-table scope ordinals must be canonical")
        region_ordinals = tuple(item.physical_table_ordinal for item in self.regions)
        if self.partition_mode is LogicalStructureTablePartitionMode.WHOLE_TABLE_ONLY:
            if region_ordinals != self.physical_table_ordinals or any(
                item.identity_mode
                is not LogicalStructureTableRegionIdentityMode.WHOLE_PHYSICAL_TABLE
                for item in self.regions
            ):
                raise ValueError(
                    "whole-table scopes require one whole region per table"
                )
        return self


class LogicalStructurePhysicalTableDispositionProfileV1(_StrictFrozenModel):
    physical_table_ordinal: StrictInt = Field(ge=0)
    disposition: D2PhysicalTableDispositionKind
    logical_scope_ordinal: StrictInt | None = Field(default=None, ge=0)
    nested_owner_physical_table_ordinal: StrictInt | None = Field(
        default=None,
        ge=0,
    )


class LogicalStructureDispositionCountV1(_StrictFrozenModel):
    disposition: str = Field(min_length=1, max_length=120)
    count: StrictInt = Field(ge=0)


class LogicalStructureSourceDispositionProfileV1(_StrictFrozenModel):
    source_node_ordinal: StrictInt = Field(ge=0)
    source_order: StrictInt = Field(ge=0)
    start_offset: StrictInt | None = Field(default=None, ge=0)
    end_offset: StrictInt | None = Field(default=None, ge=1)
    disposition: D2SourceEvidenceDispositionKind

    @model_validator(mode="after")
    def validate_range(self) -> LogicalStructureSourceDispositionProfileV1:
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("source disposition offsets must be supplied together")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("source disposition range must be non-empty")
        return self


class LogicalStructureStatusProfileV1(_StrictFrozenModel):
    proposal_status: D2LogicalStructureStatus
    unresolved_code_count: StrictInt = Field(ge=0)
    unresolved_source_evidence_count: StrictInt = Field(ge=0)
    unresolved_physical_table_count: StrictInt = Field(ge=0)
    source_evidence_disposition_counts: tuple[LogicalStructureDispositionCountV1, ...]
    source_evidence_dispositions: tuple[LogicalStructureSourceDispositionProfileV1, ...]
    physical_table_dispositions: tuple[
        LogicalStructurePhysicalTableDispositionProfileV1, ...
    ]

    @model_validator(mode="after")
    def validate_profile(self) -> LogicalStructureStatusProfileV1:
        disposition_values = tuple(
            item.disposition for item in self.source_evidence_disposition_counts
        )
        if disposition_values != tuple(sorted(set(disposition_values))):
            raise ValueError("source disposition counts must be canonical")
        source_order = tuple(
            (
                item.source_node_ordinal,
                -1 if item.start_offset is None else item.start_offset,
                item.disposition.value,
            )
            for item in self.source_evidence_dispositions
        )
        if source_order != tuple(sorted(source_order)):
            raise ValueError("source dispositions must use canonical source order")
        physical_ordinals = tuple(
            item.physical_table_ordinal for item in self.physical_table_dispositions
        )
        if physical_ordinals != tuple(range(len(physical_ordinals))):
            raise ValueError("physical-table dispositions require a complete ledger")
        return self


class LogicalStructureRepeatabilityFingerprintV1(_StrictFrozenModel):
    schema_version: Literal["logical-structure-repeatability-fingerprint/v1"] = (
        LOGICAL_STRUCTURE_REPEATABILITY_FINGERPRINT_SCHEMA_VERSION
    )
    algorithm_version: Literal["source-ordered-content-free-profile/v1"] = (
        LOGICAL_STRUCTURE_REPEATABILITY_ALGORITHM_VERSION
    )
    fingerprint_id: str = Field(
        pattern=r"^logical-structure-repeatability-fingerprint-[0-9a-f]{32}$"
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    table_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_id: str = Field(min_length=1, max_length=160)
    validator_version: str = Field(min_length=1, max_length=80)
    comparison_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_profile: LogicalStructureCoverageProfileV1
    coverage_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_profile: tuple[LogicalStructureObjectProfileV1, ...] = Field(min_length=1)
    object_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    table_partition_capability: LogicalStructureTablePartitionMode
    logical_table_scopes: tuple[LogicalStructureTableScopeProfileV1, ...] = ()
    table_grouping_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status_profile: LogicalStructureStatusProfileV1
    status_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_expressiveness_codes: tuple[str, ...]
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_fingerprint(self) -> LogicalStructureRepeatabilityFingerprintV1:
        object_ordinals = tuple(
            item.logical_object_ordinal for item in self.object_profile
        )
        if object_ordinals != tuple(range(len(object_ordinals))):
            raise ValueError(
                "logical-object profile requires a complete ordinal ledger"
            )
        scope_ordinals = tuple(
            item.logical_scope_ordinal for item in self.logical_table_scopes
        )
        if scope_ordinals != tuple(range(len(scope_ordinals))):
            raise ValueError("logical-table profile requires a complete ordinal ledger")
        if self.schema_expressiveness_codes != tuple(
            sorted(set(self.schema_expressiveness_codes))
        ):
            raise ValueError("schema expressiveness codes must be canonical")
        expected_comparison_identity = (
            stable_logical_structure_comparison_identity_sha256(
                source_sha256=self.source_sha256,
                source_ir_canonical_sha256=self.source_ir_canonical_sha256,
                kit_identity=self.kit_identity,
                validator_id=self.validator_id,
                validator_version=self.validator_version,
            )
        )
        if self.comparison_identity_sha256 != expected_comparison_identity:
            raise ValueError("comparison identity SHA drifted")
        if self.coverage_profile_sha256 != _model_sha256(self.coverage_profile):
            raise ValueError("coverage profile SHA drifted")
        if self.object_profile_sha256 != _models_sha256(self.object_profile):
            raise ValueError("object profile SHA drifted")
        if self.table_grouping_profile_sha256 != _canonical_sha256(
            {
                "table_partition_capability": self.table_partition_capability.value,
                "logical_table_scopes": [
                    item.model_dump(mode="json") for item in self.logical_table_scopes
                ],
            }
        ):
            raise ValueError("table grouping profile SHA drifted")
        if self.status_profile_sha256 != _model_sha256(self.status_profile):
            raise ValueError("status profile SHA drifted")
        expected_profile_sha = _canonical_sha256(
            {
                "comparison_identity_sha256": self.comparison_identity_sha256,
                "coverage_profile_sha256": self.coverage_profile_sha256,
                "object_profile_sha256": self.object_profile_sha256,
                "table_grouping_profile_sha256": self.table_grouping_profile_sha256,
                "status_profile_sha256": self.status_profile_sha256,
                "schema_expressiveness_codes": list(self.schema_expressiveness_codes),
            }
        )
        if self.profile_sha256 != expected_profile_sha:
            raise ValueError("repeatability profile SHA drifted")
        expected_id = stable_logical_structure_repeatability_fingerprint_id(
            source_sha256=self.source_sha256,
            validation_receipt_sha256=self.validation_receipt_sha256,
            profile_sha256=self.profile_sha256,
        )
        if self.fingerprint_id != expected_id:
            raise ValueError("repeatability fingerprint ID drifted")
        expected_fingerprint_sha = _canonical_sha256(
            self.model_dump(mode="json", exclude={"fingerprint_sha256"})
        )
        if self.fingerprint_sha256 != expected_fingerprint_sha:
            raise ValueError("repeatability fingerprint SHA drifted")
        return self


class LogicalStructureRepeatabilityEvaluationV1(_StrictFrozenModel):
    schema_version: Literal["logical-structure-repeatability-evaluation/v1"] = (
        LOGICAL_STRUCTURE_REPEATABILITY_EVALUATION_SCHEMA_VERSION
    )
    algorithm_version: Literal["source-ordered-content-free-profile/v1"] = (
        LOGICAL_STRUCTURE_REPEATABILITY_ALGORITHM_VERSION
    )
    evaluation_id: str = Field(
        pattern=r"^logical-structure-repeatability-evaluation-[0-9a-f]{32}$"
    )
    run_count: StrictInt = Field(ge=2)
    fingerprint_ids: tuple[str, ...] = Field(min_length=2)
    fingerprint_sha256s: tuple[str, ...] = Field(min_length=2)
    verdict: LogicalStructureRepeatabilityVerdict
    drift_kinds: tuple[LogicalStructureRepeatabilityDriftKind, ...]
    distinct_comparison_identity_count: StrictInt = Field(ge=1)
    distinct_coverage_profile_count: StrictInt = Field(ge=1)
    distinct_object_profile_count: StrictInt = Field(ge=1)
    distinct_table_grouping_profile_count: StrictInt = Field(ge=1)
    distinct_status_profile_count: StrictInt = Field(ge=1)
    schema_expressiveness_codes: tuple[str, ...]
    evaluation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evaluation(self) -> LogicalStructureRepeatabilityEvaluationV1:
        if self.run_count != len(self.fingerprint_ids) or self.run_count != len(
            self.fingerprint_sha256s
        ):
            raise ValueError("repeatability evaluation run ledger is incomplete")
        pairs = tuple(zip(self.fingerprint_sha256s, self.fingerprint_ids, strict=True))
        if pairs != tuple(sorted(pairs)):
            raise ValueError("repeatability fingerprints must use canonical order")
        canonical_drifts = tuple(
            drift for drift in _DRIFT_ORDER if drift in set(self.drift_kinds)
        )
        if self.drift_kinds != canonical_drifts:
            raise ValueError("repeatability drift kinds must use canonical order")
        if self.schema_expressiveness_codes != tuple(
            sorted(set(self.schema_expressiveness_codes))
        ):
            raise ValueError("schema expressiveness codes must be canonical")
        if (self.verdict is LogicalStructureRepeatabilityVerdict.PASS) != (
            not self.drift_kinds
        ):
            raise ValueError("repeatability verdict is inconsistent with drift")
        expected_id = stable_logical_structure_repeatability_evaluation_id(
            fingerprint_sha256s=self.fingerprint_sha256s,
        )
        if self.evaluation_id != expected_id:
            raise ValueError("repeatability evaluation ID drifted")
        expected_sha = _canonical_sha256(
            self.model_dump(mode="json", exclude={"evaluation_sha256"})
        )
        if self.evaluation_sha256 != expected_sha:
            raise ValueError("repeatability evaluation SHA drifted")
        return self


def build_logical_structure_repeatability_fingerprint(
    proposal: D2LogicalStructureProposalV1,
    receipt: D2LogicalStructureValidationReceiptV1,
) -> LogicalStructureRepeatabilityFingerprintV1:
    """Build a body-free repeatability fingerprint from one validated pair."""

    proposal, receipt = _validated_pair(proposal, receipt)
    physical_table_ids = receipt.physical_table_ids
    table_ordinal_by_id = {
        table_id: ordinal for ordinal, table_id in enumerate(physical_table_ids)
    }
    logical_objects = tuple(
        sorted(
            proposal.logical_objects,
            key=_logical_object_source_key,
        )
    )
    object_ordinal_by_id = {
        item.logical_object_id: ordinal for ordinal, item in enumerate(logical_objects)
    }
    logical_scopes = tuple(
        sorted(
            proposal.logical_table_scopes,
            key=lambda scope: tuple(
                table_ordinal_by_id[table_id] for table_id in scope.physical_table_ids
            ),
        )
    )
    scope_ordinal_by_id = {
        item.logical_object_id: ordinal for ordinal, item in enumerate(logical_scopes)
    }
    handle_ordinal_by_key = _source_handle_ordinal_ledger(proposal)

    table_scope_profiles = tuple(
        _table_scope_profile(
            scope,
            logical_scope_ordinal=ordinal,
            table_ordinal_by_id=table_ordinal_by_id,
            object_ordinal_by_id=object_ordinal_by_id,
        )
        for ordinal, scope in enumerate(logical_scopes)
    )
    object_profiles = tuple(
        _object_profile(
            item,
            logical_object_ordinal=ordinal,
            object_ordinal_by_id=object_ordinal_by_id,
            scope_ordinal_by_id=scope_ordinal_by_id,
            handle_ordinal_by_key=handle_ordinal_by_key,
        )
        for ordinal, item in enumerate(logical_objects)
    )
    physical_disposition_by_id = {
        item.table_id: item for item in proposal.physical_table_dispositions
    }
    physical_disposition_profiles = tuple(
        LogicalStructurePhysicalTableDispositionProfileV1(
            physical_table_ordinal=ordinal,
            disposition=physical_disposition_by_id[table_id].disposition,
            logical_scope_ordinal=(
                scope_ordinal_by_id[
                    physical_disposition_by_id[table_id].logical_table_scope_id or ""
                ]
                if physical_disposition_by_id[table_id].logical_table_scope_id
                else None
            ),
            nested_owner_physical_table_ordinal=(
                table_ordinal_by_id[
                    physical_disposition_by_id[table_id].nested_owner_table_id or ""
                ]
                if physical_disposition_by_id[table_id].nested_owner_table_id
                else None
            ),
        )
        for ordinal, table_id in enumerate(physical_table_ids)
    )
    coverage_profile = _coverage_profile(
        proposal,
        handle_ordinal_by_key=handle_ordinal_by_key,
        physical_disposition_profiles=physical_disposition_profiles,
    )
    source_disposition_profile = _normalized_source_disposition_profile(proposal)
    source_disposition_counts = Counter(
        item.disposition.value for item in source_disposition_profile
    )
    status_profile = LogicalStructureStatusProfileV1(
        proposal_status=proposal.status,
        unresolved_code_count=len(proposal.unresolved_codes),
        unresolved_source_evidence_count=len(
            receipt.unresolved_source_evidence_disposition_ids
        ),
        unresolved_physical_table_count=len(receipt.unresolved_table_ids),
        source_evidence_disposition_counts=tuple(
            LogicalStructureDispositionCountV1(
                disposition=disposition,
                count=count,
            )
            for disposition, count in sorted(source_disposition_counts.items())
        ),
        source_evidence_dispositions=source_disposition_profile,
        physical_table_dispositions=physical_disposition_profiles,
    )
    comparison_identity_sha256 = stable_logical_structure_comparison_identity_sha256(
        source_sha256=proposal.source_sha256,
        source_ir_canonical_sha256=proposal.source_ir_canonical_sha256,
        kit_identity=proposal.kit_identity,
        validator_id=receipt.validator_id,
        validator_version=receipt.validator_version,
    )
    coverage_profile_sha256 = _model_sha256(coverage_profile)
    object_profile_sha256 = _models_sha256(object_profiles)
    table_grouping_profile_sha256 = _canonical_sha256(
        {
            "table_partition_capability": (
                LogicalStructureTablePartitionMode.WHOLE_TABLE_ONLY.value
            ),
            "logical_table_scopes": [
                item.model_dump(mode="json") for item in table_scope_profiles
            ],
        }
    )
    status_profile_sha256 = _model_sha256(status_profile)
    expressiveness_codes = (_MULTI_REGION_LIMITATION,)
    profile_sha256 = _canonical_sha256(
        {
            "comparison_identity_sha256": comparison_identity_sha256,
            "coverage_profile_sha256": coverage_profile_sha256,
            "object_profile_sha256": object_profile_sha256,
            "table_grouping_profile_sha256": table_grouping_profile_sha256,
            "status_profile_sha256": status_profile_sha256,
            "schema_expressiveness_codes": list(expressiveness_codes),
        }
    )
    fingerprint_id = stable_logical_structure_repeatability_fingerprint_id(
        source_sha256=proposal.source_sha256,
        validation_receipt_sha256=receipt.receipt_sha256,
        profile_sha256=profile_sha256,
    )
    values = {
        "fingerprint_id": fingerprint_id,
        "run_id": proposal.run_id,
        "source_sha256": proposal.source_sha256,
        "source_ir_id": proposal.source_ir_id,
        "source_ir_canonical_sha256": proposal.source_ir_canonical_sha256,
        "kit_identity": proposal.kit_identity,
        "artifact_id": receipt.artifact_id,
        "artifact_sha256": receipt.artifact_sha256,
        "proposal_sha256": proposal.proposal_sha256,
        "validation_receipt_sha256": receipt.receipt_sha256,
        "validation_evidence_sha256": receipt.evidence_sha256,
        "predecessor_artifact_sha256": receipt.predecessor_d1_artifact_sha256,
        "table_map_artifact_sha256": receipt.d1_table_map_artifact_sha256,
        "validator_id": receipt.validator_id,
        "validator_version": receipt.validator_version,
        "comparison_identity_sha256": comparison_identity_sha256,
        "coverage_profile": coverage_profile,
        "coverage_profile_sha256": coverage_profile_sha256,
        "object_profile": object_profiles,
        "object_profile_sha256": object_profile_sha256,
        "table_partition_capability": (
            LogicalStructureTablePartitionMode.WHOLE_TABLE_ONLY
        ),
        "logical_table_scopes": table_scope_profiles,
        "table_grouping_profile_sha256": table_grouping_profile_sha256,
        "status_profile": status_profile,
        "status_profile_sha256": status_profile_sha256,
        "schema_expressiveness_codes": expressiveness_codes,
        "profile_sha256": profile_sha256,
    }
    draft = LogicalStructureRepeatabilityFingerprintV1.model_construct(
        **values,
        fingerprint_sha256="0" * 64,
    )
    return LogicalStructureRepeatabilityFingerprintV1(
        **values,
        fingerprint_sha256=_canonical_sha256(
            draft.model_dump(mode="json", exclude={"fingerprint_sha256"})
        ),
    )


def evaluate_logical_structure_repeatability(
    fingerprints: tuple[LogicalStructureRepeatabilityFingerprintV1, ...],
) -> LogicalStructureRepeatabilityEvaluationV1:
    """Compare two or more fingerprints without treating receipt IDs as drift."""

    if len(fingerprints) < 2:
        raise ValueError("repeatability evaluation requires at least two runs")
    validated = tuple(
        LogicalStructureRepeatabilityFingerprintV1.model_validate(
            item.model_dump(mode="json")
        )
        for item in fingerprints
    )
    ordered = tuple(
        sorted(
            validated, key=lambda item: (item.fingerprint_sha256, item.fingerprint_id)
        )
    )
    dimension_values = {
        LogicalStructureRepeatabilityDriftKind.IDENTITY_DRIFT: {
            item.comparison_identity_sha256 for item in ordered
        },
        LogicalStructureRepeatabilityDriftKind.COVERAGE_DRIFT: {
            item.coverage_profile_sha256 for item in ordered
        },
        LogicalStructureRepeatabilityDriftKind.OBJECT_BOUNDARY_DRIFT: {
            item.object_profile_sha256 for item in ordered
        },
        LogicalStructureRepeatabilityDriftKind.TABLE_GROUPING_DRIFT: {
            item.table_grouping_profile_sha256 for item in ordered
        },
        LogicalStructureRepeatabilityDriftKind.STATUS_DRIFT: {
            item.status_profile_sha256 for item in ordered
        },
    }
    drift_kinds = tuple(
        drift for drift in _DRIFT_ORDER if len(dimension_values[drift]) > 1
    )
    fingerprint_ids = tuple(item.fingerprint_id for item in ordered)
    fingerprint_sha256s = tuple(item.fingerprint_sha256 for item in ordered)
    expressiveness_codes = tuple(
        sorted({code for item in ordered for code in item.schema_expressiveness_codes})
    )
    values = {
        "evaluation_id": stable_logical_structure_repeatability_evaluation_id(
            fingerprint_sha256s=fingerprint_sha256s,
        ),
        "run_count": len(ordered),
        "fingerprint_ids": fingerprint_ids,
        "fingerprint_sha256s": fingerprint_sha256s,
        "verdict": (
            LogicalStructureRepeatabilityVerdict.FAIL
            if drift_kinds
            else LogicalStructureRepeatabilityVerdict.PASS
        ),
        "drift_kinds": drift_kinds,
        "distinct_comparison_identity_count": len(
            dimension_values[LogicalStructureRepeatabilityDriftKind.IDENTITY_DRIFT]
        ),
        "distinct_coverage_profile_count": len(
            dimension_values[LogicalStructureRepeatabilityDriftKind.COVERAGE_DRIFT]
        ),
        "distinct_object_profile_count": len(
            dimension_values[
                LogicalStructureRepeatabilityDriftKind.OBJECT_BOUNDARY_DRIFT
            ]
        ),
        "distinct_table_grouping_profile_count": len(
            dimension_values[
                LogicalStructureRepeatabilityDriftKind.TABLE_GROUPING_DRIFT
            ]
        ),
        "distinct_status_profile_count": len(
            dimension_values[LogicalStructureRepeatabilityDriftKind.STATUS_DRIFT]
        ),
        "schema_expressiveness_codes": expressiveness_codes,
    }
    draft = LogicalStructureRepeatabilityEvaluationV1.model_construct(
        **values,
        evaluation_sha256="0" * 64,
    )
    return LogicalStructureRepeatabilityEvaluationV1(
        **values,
        evaluation_sha256=_canonical_sha256(
            draft.model_dump(mode="json", exclude={"evaluation_sha256"})
        ),
    )


def logical_structure_repeatability_safe_summary(
    fingerprint: LogicalStructureRepeatabilityFingerprintV1,
) -> dict[str, object]:
    """Return counts and hashes only; never return source handles or body text."""

    fingerprint = LogicalStructureRepeatabilityFingerprintV1.model_validate(
        fingerprint.model_dump(mode="json")
    )
    return {
        "schema_version": fingerprint.schema_version,
        "fingerprint_sha256": fingerprint.fingerprint_sha256,
        "comparison_identity_sha256": fingerprint.comparison_identity_sha256,
        "profile_sha256": fingerprint.profile_sha256,
        "coverage_profile_sha256": fingerprint.coverage_profile_sha256,
        "object_profile_sha256": fingerprint.object_profile_sha256,
        "table_grouping_profile_sha256": (fingerprint.table_grouping_profile_sha256),
        "status_profile_sha256": fingerprint.status_profile_sha256,
        "status": fingerprint.status_profile.proposal_status.value,
        "logical_object_count": len(fingerprint.object_profile),
        "logical_object_kind_counts": dict(
            sorted(
                Counter(
                    item.object_kind.value for item in fingerprint.object_profile
                ).items()
            )
        ),
        "source_segment_count": sum(
            item.source_segment_count for item in fingerprint.object_profile
        ),
        "logical_table_scope_count": len(fingerprint.logical_table_scopes),
        "physical_table_count": len(
            fingerprint.status_profile.physical_table_dispositions
        ),
        "physical_table_disposition_counts": dict(
            sorted(
                Counter(
                    item.disposition.value
                    for item in fingerprint.status_profile.physical_table_dispositions
                ).items()
            )
        ),
        "unresolved_code_count": (fingerprint.status_profile.unresolved_code_count),
        "unresolved_source_evidence_count": (
            fingerprint.status_profile.unresolved_source_evidence_count
        ),
        "unresolved_physical_table_count": (
            fingerprint.status_profile.unresolved_physical_table_count
        ),
        "table_partition_capability": (fingerprint.table_partition_capability.value),
        "schema_expressiveness_codes": fingerprint.schema_expressiveness_codes,
    }


def stable_logical_structure_comparison_identity_sha256(
    *,
    source_sha256: str,
    source_ir_canonical_sha256: str,
    kit_identity: AgentKitIdentityV1,
    validator_id: str,
    validator_version: str,
) -> str:
    return _canonical_sha256(
        {
            "source_sha256": source_sha256,
            "source_ir_canonical_sha256": source_ir_canonical_sha256,
            "kit_identity": kit_identity.model_dump(mode="json"),
            "validator_id": validator_id,
            "validator_version": validator_version,
        }
    )


def stable_logical_structure_repeatability_fingerprint_id(
    *,
    source_sha256: str,
    validation_receipt_sha256: str,
    profile_sha256: str,
) -> str:
    digest = _canonical_sha256(
        {
            "source_sha256": source_sha256,
            "validation_receipt_sha256": validation_receipt_sha256,
            "profile_sha256": profile_sha256,
        }
    )
    return f"logical-structure-repeatability-fingerprint-{digest[:32]}"


def stable_logical_structure_repeatability_evaluation_id(
    *,
    fingerprint_sha256s: tuple[str, ...],
) -> str:
    fingerprint_sha256s = tuple(sorted(fingerprint_sha256s))
    digest = _canonical_sha256(
        {
            "algorithm_version": LOGICAL_STRUCTURE_REPEATABILITY_ALGORITHM_VERSION,
            "fingerprint_sha256s": list(fingerprint_sha256s),
        }
    )
    return f"logical-structure-repeatability-evaluation-{digest[:32]}"


def stable_logical_structure_table_region_id(
    *,
    physical_table_ordinal: int,
    region_ordinal_within_table: int,
    identity_mode: LogicalStructureTableRegionIdentityMode,
    cell_partition_sha256: str | None,
) -> str:
    digest = _canonical_sha256(
        {
            "physical_table_ordinal": physical_table_ordinal,
            "region_ordinal_within_table": region_ordinal_within_table,
            "identity_mode": identity_mode.value,
            "cell_partition_sha256": cell_partition_sha256,
        }
    )
    return f"logical-structure-table-region-{digest[:32]}"


def _validated_pair(
    proposal: D2LogicalStructureProposalV1,
    receipt: D2LogicalStructureValidationReceiptV1,
) -> tuple[D2LogicalStructureProposalV1, D2LogicalStructureValidationReceiptV1]:
    proposal = D2LogicalStructureProposalV1.model_validate(
        proposal.model_dump(mode="json")
    )
    receipt = D2LogicalStructureValidationReceiptV1.model_validate(
        receipt.model_dump(mode="json")
    )
    expected_segment_ids = tuple(
        segment.segment_id
        for item in proposal.logical_objects
        for segment in item.source_segments
    )
    expected_unresolved_source_ids = tuple(
        item.disposition_id
        for item in proposal.source_evidence_dispositions
        if item.disposition is D2SourceEvidenceDispositionKind.UNRESOLVED
    )
    expected_unresolved_table_ids = tuple(
        item.table_id
        for item in proposal.physical_table_dispositions
        if item.disposition is D2PhysicalTableDispositionKind.UNRESOLVED
    )
    mismatched = (
        receipt.run_id != proposal.run_id
        or receipt.source_sha256 != proposal.source_sha256
        or receipt.source_ir_id != proposal.source_ir_id
        or receipt.source_ir_canonical_sha256 != proposal.source_ir_canonical_sha256
        or receipt.kit_identity != proposal.kit_identity
        or receipt.status is not proposal.status
        or receipt.proposal_sha256 != proposal.proposal_sha256
        or receipt.artifact_sha256 != proposal.proposal_sha256
        or receipt.predecessor_d1_artifact_id
        != proposal.predecessor_d1_artifact.artifact_id
        or receipt.predecessor_d1_artifact_sha256
        != proposal.predecessor_d1_artifact.artifact_sha256
        or receipt.d1_table_map_artifact_id
        != proposal.d1_table_map_artifact.artifact_id
        or receipt.d1_table_map_artifact_sha256
        != proposal.d1_table_map_artifact.artifact_sha256
        or receipt.logical_object_ids
        != tuple(item.logical_object_id for item in proposal.logical_objects)
        or receipt.source_segment_ids != expected_segment_ids
        or receipt.logical_table_scope_ids
        != tuple(item.logical_object_id for item in proposal.logical_table_scopes)
        or receipt.source_evidence_disposition_ids
        != tuple(item.disposition_id for item in proposal.source_evidence_dispositions)
        or receipt.unresolved_source_evidence_disposition_ids
        != expected_unresolved_source_ids
        or receipt.physical_table_ids
        != tuple(item.table_id for item in proposal.physical_table_dispositions)
        or receipt.unresolved_table_ids != expected_unresolved_table_ids
    )
    if mismatched:
        raise ValueError(
            "logical-structure repeatability requires one exact validated proposal/receipt pair"
        )
    return proposal, receipt


def _source_handle_ordinal_ledger(
    proposal: D2LogicalStructureProposalV1,
) -> dict[tuple[int, str, str, str], int]:
    keys = {
        (
            segment.source_order,
            segment.handle_kind.value,
            segment.owner_source_node_id,
            segment.source_handle,
        )
        for item in proposal.logical_objects
        for segment in item.source_segments
    }
    return {key: ordinal for ordinal, key in enumerate(sorted(keys))}


def _logical_object_source_key(item) -> tuple[object, ...]:  # type: ignore[no-untyped-def]
    if item.source_segments:
        first_segment = min(
            item.source_segments,
            key=lambda segment: (
                segment.source_order,
                segment.source_handle,
                -1 if segment.start_offset is None else segment.start_offset,
                segment.segment_id,
            ),
        )
        return (
            item.source_order,
            first_segment.source_order,
            first_segment.source_handle,
            -1 if first_segment.start_offset is None else first_segment.start_offset,
            item.logical_object_id,
        )
    return (
        item.source_order,
        item.source_order,
        "",
        -1,
        item.logical_object_id,
    )


def _segment_content_free_value(
    segment,  # type: ignore[no-untyped-def]
    *,
    handle_ordinal_by_key: dict[tuple[int, str, str, str], int],
) -> dict[str, object]:
    key = (
        segment.source_order,
        segment.handle_kind.value,
        segment.owner_source_node_id,
        segment.source_handle,
    )
    return {
        "source_handle_ordinal": handle_ordinal_by_key[key],
        "source_order": segment.source_order,
        "handle_kind": segment.handle_kind.value,
        "page_spans": [
            item.model_dump(mode="json") for item in _page_spans(segment.page_numbers)
        ],
        "start_offset": segment.start_offset,
        "end_offset": segment.end_offset,
    }


def _object_profile(
    item,  # type: ignore[no-untyped-def]
    *,
    logical_object_ordinal: int,
    object_ordinal_by_id: dict[str, int],
    scope_ordinal_by_id: dict[str, int],
    handle_ordinal_by_key: dict[tuple[int, str, str, str], int],
) -> LogicalStructureObjectProfileV1:
    segment_values = [
        _segment_content_free_value(
            segment,
            handle_ordinal_by_key=handle_ordinal_by_key,
        )
        for segment in item.source_segments
    ]
    scope_ordinals = tuple(
        scope_ordinal_by_id[scope_id] for scope_id in item.logical_table_scope_ids
    )
    return LogicalStructureObjectProfileV1(
        logical_object_ordinal=logical_object_ordinal,
        object_kind=item.object_kind,
        parent_object_ordinal=(
            object_ordinal_by_id[item.parent_logical_object_id]
            if item.parent_logical_object_id is not None
            else None
        ),
        source_order=item.source_order,
        page_spans=_page_spans(item.page_numbers),
        source_segment_count=len(item.source_segments),
        logical_table_scope_ordinals=scope_ordinals,
        boundary_sha256=_canonical_sha256(
            {
                "source_segments": segment_values,
                "logical_table_scope_ordinals": list(scope_ordinals),
            }
        ),
    )


def _table_scope_profile(
    scope,  # type: ignore[no-untyped-def]
    *,
    logical_scope_ordinal: int,
    table_ordinal_by_id: dict[str, int],
    object_ordinal_by_id: dict[str, int],
) -> LogicalStructureTableScopeProfileV1:
    physical_ordinals = tuple(
        table_ordinal_by_id[table_id] for table_id in scope.physical_table_ids
    )
    regions = tuple(
        LogicalStructurePhysicalTableRegionV1(
            region_id=stable_logical_structure_table_region_id(
                physical_table_ordinal=ordinal,
                region_ordinal_within_table=0,
                identity_mode=(
                    LogicalStructureTableRegionIdentityMode.WHOLE_PHYSICAL_TABLE
                ),
                cell_partition_sha256=None,
            ),
            physical_table_ordinal=ordinal,
            region_ordinal_within_table=0,
            identity_mode=(
                LogicalStructureTableRegionIdentityMode.WHOLE_PHYSICAL_TABLE
            ),
            cell_partition_sha256=None,
        )
        for ordinal in physical_ordinals
    )
    return LogicalStructureTableScopeProfileV1(
        logical_scope_ordinal=logical_scope_ordinal,
        logical_object_ordinal=object_ordinal_by_id[scope.logical_object_id],
        partition_mode=LogicalStructureTablePartitionMode.WHOLE_TABLE_ONLY,
        physical_table_ordinals=physical_ordinals,
        regions=regions,
    )


def _coverage_profile(
    proposal: D2LogicalStructureProposalV1,
    *,
    handle_ordinal_by_key: dict[tuple[int, str, str, str], int],
    physical_disposition_profiles: tuple[
        LogicalStructurePhysicalTableDispositionProfileV1, ...
    ],
) -> LogicalStructureCoverageProfileV1:
    grouped: dict[
        tuple[int, int, D2SourceHandleKind, tuple[LogicalStructurePageSpanV1, ...]],
        list[tuple[int | None, int | None]],
    ] = defaultdict(list)
    for item in proposal.logical_objects:
        for segment in item.source_segments:
            key = (
                handle_ordinal_by_key[
                    (
                        segment.source_order,
                        segment.handle_kind.value,
                        segment.owner_source_node_id,
                        segment.source_handle,
                    )
                ],
                segment.source_order,
                segment.handle_kind,
                _page_spans(segment.page_numbers),
            )
            grouped[key].append((segment.start_offset, segment.end_offset))

    ranges: list[LogicalStructureCoverageRangeV1] = []
    for key in sorted(
        grouped,
        key=lambda item: (item[0], item[1], item[2].value),
    ):
        handle_ordinal, source_order, handle_kind, page_spans = key
        values = grouped[key]
        if any(start is None or end is None for start, end in values):
            if any(start is not None or end is not None for start, end in values):
                raise ValueError("one source handle cannot mix text and asset coverage")
            merged = ((None, None),)
        else:
            numeric = sorted((int(start), int(end)) for start, end in values)
            merged_values: list[tuple[int, int]] = []
            for start, end in numeric:
                if merged_values and start <= merged_values[-1][1]:
                    previous_start, previous_end = merged_values[-1]
                    merged_values[-1] = (previous_start, max(previous_end, end))
                else:
                    merged_values.append((start, end))
            merged = tuple(merged_values)
        ranges.extend(
            LogicalStructureCoverageRangeV1(
                source_handle_ordinal=handle_ordinal,
                source_order=source_order,
                handle_kind=handle_kind,
                page_spans=page_spans,
                start_offset=start,
                end_offset=end,
            )
            for start, end in merged
        )
    covered_physical_ordinals = tuple(
        item.physical_table_ordinal
        for item in physical_disposition_profiles
        if item.disposition
        in {
            D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART,
            D2PhysicalTableDispositionKind.NESTED_VALUE_TABLE,
        }
    )
    return LogicalStructureCoverageProfileV1(
        source_ranges=tuple(ranges),
        covered_physical_table_ordinals=covered_physical_ordinals,
    )


def _normalized_source_disposition_profile(
    proposal: D2LogicalStructureProposalV1,
) -> tuple[LogicalStructureSourceDispositionProfileV1, ...]:
    """Normalize semantic source ranges without counting object-boundary splits."""

    grouped: dict[
        tuple[str, int, D2SourceEvidenceDispositionKind],
        list[tuple[int | None, int | None]],
    ] = defaultdict(list)
    for item in proposal.source_evidence_dispositions:
        grouped[(item.source_node_id, item.source_order, item.disposition)].append(
            (item.start_offset, item.end_offset)
        )
    node_keys = tuple(
        sorted(
            {
                (source_order, source_node_id)
                for source_node_id, source_order, _ in grouped
            }
        )
    )
    node_ordinal_by_key = {key: ordinal for ordinal, key in enumerate(node_keys)}
    profiles: list[LogicalStructureSourceDispositionProfileV1] = []
    for (source_node_id, source_order, disposition), values in grouped.items():
        node_ordinal = node_ordinal_by_key[(source_order, source_node_id)]
        if any(start is None or end is None for start, end in values):
            if any(start is not None or end is not None for start, end in values):
                raise ValueError(
                    "one source node cannot mix ranged and asset disposition"
                )
            profiles.append(
                LogicalStructureSourceDispositionProfileV1(
                    source_node_ordinal=node_ordinal,
                    source_order=source_order,
                    disposition=disposition,
                )
            )
            continue
        numeric = sorted((int(start), int(end)) for start, end in values)
        merged: list[tuple[int, int]] = []
        for start, end in numeric:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                previous_start, previous_end = merged[-1]
                merged[-1] = (previous_start, max(previous_end, end))
        profiles.extend(
            LogicalStructureSourceDispositionProfileV1(
                source_node_ordinal=node_ordinal,
                source_order=source_order,
                start_offset=start,
                end_offset=end,
                disposition=disposition,
            )
            for start, end in merged
        )
    return tuple(
        sorted(
            profiles,
            key=lambda item: (
                item.source_node_ordinal,
                -1 if item.start_offset is None else item.start_offset,
                item.disposition.value,
            ),
        )
    )


def _page_spans(
    page_numbers: tuple[int, ...],
) -> tuple[LogicalStructurePageSpanV1, ...]:
    spans: list[LogicalStructurePageSpanV1] = []
    for page in page_numbers:
        if spans and page == spans[-1].end_page + 1:
            spans[-1] = LogicalStructurePageSpanV1(
                start_page=spans[-1].start_page,
                end_page=page,
            )
        else:
            spans.append(LogicalStructurePageSpanV1(start_page=page, end_page=page))
    return tuple(spans)


def _model_sha256(value: BaseModel) -> str:
    return _canonical_sha256(value.model_dump(mode="json"))


def _models_sha256(values: tuple[BaseModel, ...]) -> str:
    return _canonical_sha256([item.model_dump(mode="json") for item in values])


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "LOGICAL_STRUCTURE_REPEATABILITY_ALGORITHM_VERSION",
    "LOGICAL_STRUCTURE_REPEATABILITY_EVALUATION_SCHEMA_VERSION",
    "LOGICAL_STRUCTURE_REPEATABILITY_FINGERPRINT_SCHEMA_VERSION",
    "LogicalStructureCoverageProfileV1",
    "LogicalStructureCoverageRangeV1",
    "LogicalStructureDispositionCountV1",
    "LogicalStructureObjectProfileV1",
    "LogicalStructurePageSpanV1",
    "LogicalStructurePhysicalTableDispositionProfileV1",
    "LogicalStructurePhysicalTableRegionV1",
    "LogicalStructureRepeatabilityDriftKind",
    "LogicalStructureRepeatabilityEvaluationV1",
    "LogicalStructureRepeatabilityFingerprintV1",
    "LogicalStructureRepeatabilityVerdict",
    "LogicalStructureStatusProfileV1",
    "LogicalStructureSourceDispositionProfileV1",
    "LogicalStructureTablePartitionMode",
    "LogicalStructureTableRegionIdentityMode",
    "LogicalStructureTableScopeProfileV1",
    "build_logical_structure_repeatability_fingerprint",
    "evaluate_logical_structure_repeatability",
    "logical_structure_repeatability_safe_summary",
    "stable_logical_structure_comparison_identity_sha256",
    "stable_logical_structure_repeatability_evaluation_id",
    "stable_logical_structure_repeatability_fingerprint_id",
    "stable_logical_structure_table_region_id",
]
