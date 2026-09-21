from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from reqpilotx_server.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalStructureProposalV1,
    D2LogicalStructureStatus,
    D2LogicalStructureValidationReceiptV1,
    D2PhysicalTableDispositionKind,
    D2SourceEvidenceDispositionKind,
)
from reqpilotx_server.application.extraction.agent_logical_table_scope import (
    source_ir_canonical_sha256,
)
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot, SourceNodeKind, TableIR


LOGICAL_STRUCTURE_QUALITY_ORACLE_SCHEMA_VERSION = "logical-structure-quality-oracle/v1"
LOGICAL_STRUCTURE_QUALITY_REPORT_SCHEMA_VERSION = (
    "logical-structure-quality-evaluation-report/v1"
)
LOGICAL_STRUCTURE_QUALITY_EVALUATOR_ID = "reqpilotx-logical-structure-quality-oracle"
LOGICAL_STRUCTURE_QUALITY_EVALUATOR_VERSION = "1.0.0"

_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_FINDING_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class LogicalStructureQualityStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class LogicalStructureQualityAxisStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class LogicalStructureQualityAxis(StrEnum):
    IDENTITY = "IDENTITY"
    COVERAGE = "COVERAGE"
    BOUNDARY = "BOUNDARY"


class LogicalStructureTableRegionSelectorKind(StrEnum):
    WHOLE_TABLE = "WHOLE_TABLE"
    CELL_IDS = "CELL_IDS"
    ROW_INDICES = "ROW_INDICES"
    COLUMN_INDICES = "COLUMN_INDICES"
    CELL_COORDINATES = "CELL_COORDINATES"
    BBOX = "BBOX"


class LogicalStructureCellCoordinateV1(_StrictFrozenModel):
    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)


class LogicalStructureRegionBboxV1(_StrictFrozenModel):
    page_number: StrictInt = Field(ge=1)
    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def validate_bbox(self) -> LogicalStructureRegionBboxV1:
        values = (self.x0, self.y0, self.x1, self.y1)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("logical-structure oracle bbox values must be finite")
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("logical-structure oracle bbox must have positive area")
        return self


class LogicalStructureTableRegionV1(_StrictFrozenModel):
    """One exact region of a physical table.

    Region selectors are resolved against immutable SourceIR table cells. A row or
    column selector uses cell anchor coordinates; a bbox selector selects cells
    whose native bbox has positive overlap with the declared page region.
    """

    region_id: str = Field(min_length=1, max_length=160)
    table_id: str = Field(min_length=1, max_length=160)
    selector_kind: LogicalStructureTableRegionSelectorKind
    cell_ids: tuple[str, ...] = ()
    row_indices: tuple[StrictInt, ...] = ()
    column_indices: tuple[StrictInt, ...] = ()
    cell_coordinates: tuple[LogicalStructureCellCoordinateV1, ...] = ()
    bbox: LogicalStructureRegionBboxV1 | None = None

    @field_validator("region_id", "table_id")
    @classmethod
    def validate_refs(cls, value: str) -> str:
        if _REF_PATTERN.fullmatch(value) is None:
            raise ValueError("logical-structure oracle refs must be portable IDs")
        return value

    @field_validator("cell_ids")
    @classmethod
    def validate_cell_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("oracle region cell IDs must be non-empty and sorted")
        return value

    @field_validator("row_indices", "column_indices")
    @classmethod
    def validate_indices(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("oracle region indexes must be unique and sorted")
        return value

    @field_validator("cell_coordinates")
    @classmethod
    def validate_coordinates(
        cls,
        value: tuple[LogicalStructureCellCoordinateV1, ...],
    ) -> tuple[LogicalStructureCellCoordinateV1, ...]:
        keys = tuple((item.row, item.column) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("oracle cell coordinates must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_selector(self) -> LogicalStructureTableRegionV1:
        populated = {
            LogicalStructureTableRegionSelectorKind.CELL_IDS: bool(self.cell_ids),
            LogicalStructureTableRegionSelectorKind.ROW_INDICES: bool(self.row_indices),
            LogicalStructureTableRegionSelectorKind.COLUMN_INDICES: bool(
                self.column_indices
            ),
            LogicalStructureTableRegionSelectorKind.CELL_COORDINATES: bool(
                self.cell_coordinates
            ),
            LogicalStructureTableRegionSelectorKind.BBOX: self.bbox is not None,
        }
        supplied_count = sum(populated.values())
        if self.selector_kind is LogicalStructureTableRegionSelectorKind.WHOLE_TABLE:
            if supplied_count:
                raise ValueError("WHOLE_TABLE cannot carry a narrower selector")
            return self
        if supplied_count != 1 or not populated[self.selector_kind]:
            raise ValueError(
                "oracle table region must carry exactly its declared selector"
            )
        return self


class LogicalStructureRegionPairV1(_StrictFrozenModel):
    first_region_id: str = Field(min_length=1, max_length=160)
    second_region_id: str = Field(min_length=1, max_length=160)

    @field_validator("first_region_id", "second_region_id")
    @classmethod
    def validate_refs(cls, value: str) -> str:
        if _REF_PATTERN.fullmatch(value) is None:
            raise ValueError("logical-structure oracle refs must be portable IDs")
        return value

    @model_validator(mode="after")
    def reject_self_pair(self) -> LogicalStructureRegionPairV1:
        if self.first_region_id == self.second_region_id:
            raise ValueError("a boundary pair requires two different regions")
        return self


class LogicalStructureExpectedPageOwnerV1(_StrictFrozenModel):
    owner_ref: str = Field(min_length=1, max_length=160)
    object_kind: D2LogicalObjectKind
    page_numbers: tuple[StrictInt, ...] = Field(min_length=1)

    @field_validator("owner_ref")
    @classmethod
    def validate_owner_ref(cls, value: str) -> str:
        if _REF_PATTERN.fullmatch(value) is None:
            raise ValueError("logical-structure oracle refs must be portable IDs")
        return value

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(page < 1 for page in value):
            raise ValueError("oracle page ownership must be positive and sorted")
        return value

    @model_validator(mode="after")
    def validate_owner_kind(self) -> LogicalStructureExpectedPageOwnerV1:
        if self.object_kind not in {
            D2LogicalObjectKind.DOCUMENT_UNIT,
            D2LogicalObjectKind.ATTACHMENT,
        }:
            raise ValueError(
                "page ownership may be declared only for document units/attachments"
            )
        return self


class LogicalStructureExpectedTableGroupV1(_StrictFrozenModel):
    logical_group_ref: str = Field(min_length=1, max_length=160)
    regions: tuple[LogicalStructureTableRegionV1, ...] = Field(min_length=1)
    continuation_pairs: tuple[LogicalStructureRegionPairV1, ...] = ()

    @field_validator("logical_group_ref")
    @classmethod
    def validate_group_ref(cls, value: str) -> str:
        if _REF_PATTERN.fullmatch(value) is None:
            raise ValueError("logical-structure oracle refs must be portable IDs")
        return value

    @model_validator(mode="after")
    def validate_group(self) -> LogicalStructureExpectedTableGroupV1:
        region_ids = tuple(item.region_id for item in self.regions)
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("one expected logical table cannot repeat a region")
        adjacent_pairs = set(zip(region_ids, region_ids[1:], strict=False))
        observed_pairs = tuple(
            (item.first_region_id, item.second_region_id)
            for item in self.continuation_pairs
        )
        if len(observed_pairs) != len(set(observed_pairs)) or any(
            pair not in adjacent_pairs for pair in observed_pairs
        ):
            raise ValueError(
                "continuation pairs must be unique adjacent expected regions"
            )
        return self


class LogicalStructureExpectedNestedRegionV1(_StrictFrozenModel):
    region: LogicalStructureTableRegionV1
    owner_region_id: str = Field(min_length=1, max_length=160)

    @field_validator("owner_region_id")
    @classmethod
    def validate_owner_ref(cls, value: str) -> str:
        if _REF_PATTERN.fullmatch(value) is None:
            raise ValueError("logical-structure oracle refs must be portable IDs")
        return value


class LogicalStructureBoundaryExpectationV1(_StrictFrozenModel):
    expectation_id: str = Field(min_length=1, max_length=160)
    page_owners: tuple[LogicalStructureExpectedPageOwnerV1, ...] = Field(min_length=1)
    logical_table_groups: tuple[LogicalStructureExpectedTableGroupV1, ...] = ()
    nested_regions: tuple[LogicalStructureExpectedNestedRegionV1, ...] = ()
    false_regions: tuple[LogicalStructureTableRegionV1, ...] = ()
    must_separate_pairs: tuple[LogicalStructureRegionPairV1, ...] = ()

    @field_validator("expectation_id")
    @classmethod
    def validate_expectation_id(cls, value: str) -> str:
        if _REF_PATTERN.fullmatch(value) is None:
            raise ValueError("logical-structure oracle refs must be portable IDs")
        return value

    @model_validator(mode="after")
    def validate_expectation(self) -> LogicalStructureBoundaryExpectationV1:
        owner_refs = tuple(item.owner_ref for item in self.page_owners)
        if len(owner_refs) != len(set(owner_refs)):
            raise ValueError("oracle page-owner refs must be unique")
        group_refs = tuple(item.logical_group_ref for item in self.logical_table_groups)
        if len(group_refs) != len(set(group_refs)):
            raise ValueError("oracle logical-table group refs must be unique")

        group_regions = tuple(
            region for group in self.logical_table_groups for region in group.regions
        )
        disposition_regions = (
            *group_regions,
            *(item.region for item in self.nested_regions),
            *self.false_regions,
        )
        region_ids = tuple(item.region_id for item in disposition_regions)
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("oracle table-region IDs must be globally unique")
        group_region_ids = {item.region_id for item in group_regions}
        if any(
            item.owner_region_id not in group_region_ids for item in self.nested_regions
        ):
            raise ValueError("nested regions must reference a logical-group region")

        pair_keys: set[frozenset[str]] = set()
        for pair in self.must_separate_pairs:
            if {
                pair.first_region_id,
                pair.second_region_id,
            } - group_region_ids:
                raise ValueError(
                    "must-separate pairs must reference logical-group regions"
                )
            key = frozenset((pair.first_region_id, pair.second_region_id))
            if key in pair_keys:
                raise ValueError("must-separate boundary pairs must be unique")
            pair_keys.add(key)
        return self


class LogicalStructureQualityOracleV1(_StrictFrozenModel):
    """Source-bound boundary oracle; it contains IDs/geometry, never source text."""

    schema_version: Literal["logical-structure-quality-oracle/v1"] = (
        LOGICAL_STRUCTURE_QUALITY_ORACLE_SCHEMA_VERSION
    )
    oracle_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_page_count: StrictInt = Field(ge=1)
    expected_physical_table_ids: tuple[str, ...] = ()
    primary_expectation: LogicalStructureBoundaryExpectationV1
    allowed_alternatives: tuple[LogicalStructureBoundaryExpectationV1, ...] = ()
    oracle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("oracle_id")
    @classmethod
    def validate_oracle_id(cls, value: str) -> str:
        if _REF_PATTERN.fullmatch(value) is None:
            raise ValueError("logical-structure oracle refs must be portable IDs")
        return value

    @field_validator("expected_physical_table_ids")
    @classmethod
    def validate_table_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("expected physical table IDs must be unique/non-empty")
        return value

    @model_validator(mode="after")
    def validate_oracle(self) -> LogicalStructureQualityOracleV1:
        expectations = (self.primary_expectation, *self.allowed_alternatives)
        expectation_ids = tuple(item.expectation_id for item in expectations)
        if len(expectation_ids) != len(set(expectation_ids)):
            raise ValueError("oracle expectation IDs must be unique")
        allowed_ids = tuple(item.expectation_id for item in self.allowed_alternatives)
        if allowed_ids != tuple(sorted(allowed_ids)):
            raise ValueError("allowed alternatives must be sorted by expectation ID")

        expected_pages = set(range(1, self.expected_page_count + 1))
        expected_tables = set(self.expected_physical_table_ids)
        for expectation in expectations:
            page_counts = Counter(
                page for owner in expectation.page_owners for page in owner.page_numbers
            )
            if set(page_counts) != expected_pages or any(
                count != 1 for count in page_counts.values()
            ):
                raise ValueError(
                    "every oracle expectation must own each document page once"
                )
            region_tables = {
                region.table_id for region in _expectation_regions(expectation)
            }
            if region_tables != expected_tables:
                raise ValueError(
                    "every oracle expectation must disposition every physical table"
                )

        expected_sha = stable_logical_structure_quality_oracle_sha256(self)
        if self.oracle_sha256 != expected_sha:
            raise ValueError("logical-structure oracle SHA does not bind the oracle")
        return self


class LogicalStructureQualityFindingV1(_StrictFrozenModel):
    axis: LogicalStructureQualityAxis
    code: str = Field(min_length=1, max_length=120)
    subject_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if _FINDING_PATTERN.fullmatch(value) is None:
            raise ValueError("quality finding codes must be lowercase snake_case")
        return value

    @field_validator("subject_refs")
    @classmethod
    def validate_subjects(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("quality finding subjects must be non-empty and sorted")
        return value


class LogicalStructureAlternativeEvaluationV1(_StrictFrozenModel):
    expectation_id: str = Field(min_length=1, max_length=160)
    status: LogicalStructureQualityStatus
    expected_boundary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding_codes: tuple[str, ...] = ()

    @field_validator("finding_codes")
    @classmethod
    def validate_finding_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _FINDING_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("alternative finding codes must be sorted reason codes")
        return value

    @model_validator(mode="after")
    def validate_status(self) -> LogicalStructureAlternativeEvaluationV1:
        if (self.status is LogicalStructureQualityStatus.PASS) == bool(
            self.finding_codes
        ):
            raise ValueError("alternative PASS/FAIL must agree with finding codes")
        return self


class LogicalStructureQualityEvaluationReportV1(_StrictFrozenModel):
    schema_version: Literal["logical-structure-quality-evaluation-report/v1"] = (
        LOGICAL_STRUCTURE_QUALITY_REPORT_SCHEMA_VERSION
    )
    evaluator_id: Literal["reqpilotx-logical-structure-quality-oracle"] = (
        LOGICAL_STRUCTURE_QUALITY_EVALUATOR_ID
    )
    evaluator_version: Literal["1.0.0"] = LOGICAL_STRUCTURE_QUALITY_EVALUATOR_VERSION
    oracle_id: str = Field(min_length=1, max_length=160)
    oracle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_status: LogicalStructureQualityAxisStatus
    coverage_status: LogicalStructureQualityAxisStatus
    boundary_status: LogicalStructureQualityAxisStatus
    overall_status: LogicalStructureQualityStatus
    matched_expectation_id: str | None = Field(default=None, max_length=160)
    coverage_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_boundary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alternative_evaluations: tuple[LogicalStructureAlternativeEvaluationV1, ...]
    findings: tuple[LogicalStructureQualityFindingV1, ...]
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> LogicalStructureQualityEvaluationReportV1:
        finding_keys = tuple(
            (item.axis.value, item.code, item.subject_refs) for item in self.findings
        )
        if finding_keys != tuple(sorted(set(finding_keys))):
            raise ValueError("logical-structure quality findings must be canonical")
        alternative_ids = tuple(
            item.expectation_id for item in self.alternative_evaluations
        )
        if len(alternative_ids) != len(set(alternative_ids)):
            raise ValueError("quality report alternatives must be unique")

        if self.identity_status is LogicalStructureQualityAxisStatus.FAIL:
            if (
                self.coverage_status
                is not LogicalStructureQualityAxisStatus.NOT_EVALUATED
                or self.boundary_status
                is not LogicalStructureQualityAxisStatus.NOT_EVALUATED
                or self.alternative_evaluations
                or self.matched_expectation_id is not None
            ):
                raise ValueError("identity failure must stop quality evaluation")
        elif self.identity_status is not LogicalStructureQualityAxisStatus.PASS:
            raise ValueError("identity status must be PASS or FAIL")
        elif (
            self.coverage_status is LogicalStructureQualityAxisStatus.NOT_EVALUATED
            or self.boundary_status is LogicalStructureQualityAxisStatus.NOT_EVALUATED
        ):
            raise ValueError("bound inputs require both quality axes to be evaluated")

        expected_overall = (
            LogicalStructureQualityStatus.PASS
            if self.identity_status is LogicalStructureQualityAxisStatus.PASS
            and self.coverage_status is LogicalStructureQualityAxisStatus.PASS
            and self.boundary_status is LogicalStructureQualityAxisStatus.PASS
            else LogicalStructureQualityStatus.FAIL
        )
        if self.overall_status is not expected_overall:
            raise ValueError("overall quality status must bind all three axes")
        if (self.boundary_status is LogicalStructureQualityAxisStatus.PASS) != (
            self.matched_expectation_id is not None
        ):
            raise ValueError("boundary PASS requires one matched expectation")

        expected_sha = stable_logical_structure_quality_report_sha256(self)
        if self.report_sha256 != expected_sha:
            raise ValueError("quality report SHA does not bind the complete report")
        return self


def stable_logical_structure_quality_oracle_sha256(
    oracle: LogicalStructureQualityOracleV1,
) -> str:
    return _canonical_sha256(oracle.model_dump(mode="json", exclude={"oracle_sha256"}))


def stable_logical_structure_quality_report_sha256(
    report: LogicalStructureQualityEvaluationReportV1,
) -> str:
    return _canonical_sha256(report.model_dump(mode="json", exclude={"report_sha256"}))


def evaluate_logical_structure_quality(
    source_ir: SourceIRSnapshot,
    proposal: D2LogicalStructureProposalV1,
    receipt: D2LogicalStructureValidationReceiptV1,
    *,
    oracle: LogicalStructureQualityOracleV1,
    expected_oracle_sha256: str,
) -> LogicalStructureQualityEvaluationReportV1:
    """Evaluate evidence coverage and logical boundary quality independently.

    A Host exact-cover receipt can make ``coverage_status=PASS``. It never makes
    ``boundary_status=PASS`` unless one complete source-bound expectation matches.
    """

    actual_source_ir_sha = source_ir_canonical_sha256(source_ir)
    findings: list[LogicalStructureQualityFindingV1] = []
    identity_codes: set[str] = set()

    if expected_oracle_sha256 != oracle.oracle_sha256:
        identity_codes.add("oracle_sha256_drift")
    if (
        source_ir.source_sha256 != oracle.source_sha256
        or proposal.source_sha256 != oracle.source_sha256
        or receipt.source_sha256 != oracle.source_sha256
    ):
        identity_codes.add("source_identity_drift")
    if (
        actual_source_ir_sha != oracle.source_ir_canonical_sha256
        or proposal.source_ir_canonical_sha256 != oracle.source_ir_canonical_sha256
        or receipt.source_ir_canonical_sha256 != oracle.source_ir_canonical_sha256
    ):
        identity_codes.add("source_ir_identity_drift")
    if (
        proposal.kit_identity.bundle_sha256 != oracle.kit_bundle_sha256
        or receipt.kit_identity.bundle_sha256 != oracle.kit_bundle_sha256
    ):
        identity_codes.add("kit_identity_drift")
    if proposal.predecessor_d1_artifact.page_count != oracle.expected_page_count:
        identity_codes.add("page_scope_identity_drift")
    if not _proposal_receipt_lineage_matches(proposal, receipt):
        identity_codes.add("proposal_receipt_lineage_drift")

    table_catalog = _physical_table_catalog(source_ir)
    if tuple(table_catalog) != oracle.expected_physical_table_ids:
        identity_codes.add("oracle_physical_table_catalog_drift")
    if not _proposal_table_scope_matches_source(proposal, table_catalog):
        identity_codes.add("proposal_table_region_source_drift")

    resolved_expectations: dict[str, dict[str, frozenset[str]]] = {}
    for expectation in (oracle.primary_expectation, *oracle.allowed_alternatives):
        try:
            resolved = _resolve_expectation_regions(expectation, table_catalog)
        except ValueError:
            identity_codes.add("oracle_region_unresolvable")
            continue
        partition_codes = _region_partition_codes(
            resolved,
            table_catalog=table_catalog,
        )
        identity_codes.update(partition_codes)
        resolved_expectations[expectation.expectation_id] = resolved

    for code in sorted(identity_codes):
        findings.append(
            _finding(
                LogicalStructureQualityAxis.IDENTITY,
                code,
                (oracle.oracle_id,),
            )
        )

    coverage_evidence_sha256 = _coverage_evidence_sha256(
        proposal,
        receipt,
        oracle=oracle,
    )
    try:
        observed_boundary = _observed_boundary_payload(proposal, table_catalog)
    except (KeyError, ValueError):
        identity_codes.add("proposal_table_region_source_drift")
        observed_boundary = _empty_observed_boundary_payload()
    observed_boundary_sha256 = _canonical_sha256(observed_boundary)

    if "proposal_table_region_source_drift" in identity_codes and not any(
        item.code == "proposal_table_region_source_drift" for item in findings
    ):
        findings.append(
            _finding(
                LogicalStructureQualityAxis.IDENTITY,
                "proposal_table_region_source_drift",
                (proposal.proposal_sha256,),
            )
        )

    if identity_codes:
        return _build_report(
            oracle=oracle,
            proposal=proposal,
            receipt=receipt,
            actual_source_ir_sha=actual_source_ir_sha,
            identity_status=LogicalStructureQualityAxisStatus.FAIL,
            coverage_status=LogicalStructureQualityAxisStatus.NOT_EVALUATED,
            boundary_status=LogicalStructureQualityAxisStatus.NOT_EVALUATED,
            matched_expectation_id=None,
            coverage_evidence_sha256=coverage_evidence_sha256,
            observed_boundary_sha256=observed_boundary_sha256,
            alternative_evaluations=(),
            findings=tuple(findings),
        )

    coverage_codes = _coverage_finding_codes(proposal, receipt, oracle=oracle)
    for code in coverage_codes:
        findings.append(
            _finding(
                LogicalStructureQualityAxis.COVERAGE,
                code,
                (proposal.proposal_sha256,),
            )
        )
    coverage_status = (
        LogicalStructureQualityAxisStatus.FAIL
        if coverage_codes
        else LogicalStructureQualityAxisStatus.PASS
    )

    alternative_evaluations: list[LogicalStructureAlternativeEvaluationV1] = []
    matched_expectation_id: str | None = None
    boundary_findings_by_expectation: dict[
        str, tuple[LogicalStructureQualityFindingV1, ...]
    ] = {}
    for expectation in (oracle.primary_expectation, *oracle.allowed_alternatives):
        resolved = resolved_expectations[expectation.expectation_id]
        expectation_findings = _evaluate_boundary_expectation(
            expectation,
            resolved_regions=resolved,
            observed_boundary=observed_boundary,
        )
        boundary_findings_by_expectation[expectation.expectation_id] = (
            expectation_findings
        )
        finding_codes = tuple(sorted({item.code for item in expectation_findings}))
        status = (
            LogicalStructureQualityStatus.FAIL
            if finding_codes
            else LogicalStructureQualityStatus.PASS
        )
        alternative_evaluations.append(
            LogicalStructureAlternativeEvaluationV1(
                expectation_id=expectation.expectation_id,
                status=status,
                expected_boundary_sha256=_expected_boundary_sha256(
                    expectation,
                    resolved,
                ),
                finding_codes=finding_codes,
            )
        )
        if (
            status is LogicalStructureQualityStatus.PASS
            and matched_expectation_id is None
        ):
            matched_expectation_id = expectation.expectation_id

    if matched_expectation_id is None:
        boundary_status = LogicalStructureQualityAxisStatus.FAIL
        for expectation in (oracle.primary_expectation, *oracle.allowed_alternatives):
            findings.extend(
                boundary_findings_by_expectation[expectation.expectation_id]
            )
    else:
        boundary_status = LogicalStructureQualityAxisStatus.PASS

    return _build_report(
        oracle=oracle,
        proposal=proposal,
        receipt=receipt,
        actual_source_ir_sha=actual_source_ir_sha,
        identity_status=LogicalStructureQualityAxisStatus.PASS,
        coverage_status=coverage_status,
        boundary_status=boundary_status,
        matched_expectation_id=matched_expectation_id,
        coverage_evidence_sha256=coverage_evidence_sha256,
        observed_boundary_sha256=observed_boundary_sha256,
        alternative_evaluations=tuple(alternative_evaluations),
        findings=tuple(findings),
    )


def _build_report(
    *,
    oracle: LogicalStructureQualityOracleV1,
    proposal: D2LogicalStructureProposalV1,
    receipt: D2LogicalStructureValidationReceiptV1,
    actual_source_ir_sha: str,
    identity_status: LogicalStructureQualityAxisStatus,
    coverage_status: LogicalStructureQualityAxisStatus,
    boundary_status: LogicalStructureQualityAxisStatus,
    matched_expectation_id: str | None,
    coverage_evidence_sha256: str,
    observed_boundary_sha256: str,
    alternative_evaluations: tuple[LogicalStructureAlternativeEvaluationV1, ...],
    findings: tuple[LogicalStructureQualityFindingV1, ...],
) -> LogicalStructureQualityEvaluationReportV1:
    canonical_findings = tuple(
        sorted(
            set(findings),
            key=lambda item: (item.axis.value, item.code, item.subject_refs),
        )
    )
    overall_status = (
        LogicalStructureQualityStatus.PASS
        if identity_status is LogicalStructureQualityAxisStatus.PASS
        and coverage_status is LogicalStructureQualityAxisStatus.PASS
        and boundary_status is LogicalStructureQualityAxisStatus.PASS
        else LogicalStructureQualityStatus.FAIL
    )
    values = dict(
        oracle_id=oracle.oracle_id,
        oracle_sha256=oracle.oracle_sha256,
        proposal_sha256=proposal.proposal_sha256,
        receipt_sha256=receipt.receipt_sha256,
        source_sha256=proposal.source_sha256,
        source_ir_canonical_sha256=actual_source_ir_sha,
        kit_bundle_sha256=proposal.kit_identity.bundle_sha256,
        identity_status=identity_status,
        coverage_status=coverage_status,
        boundary_status=boundary_status,
        overall_status=overall_status,
        matched_expectation_id=matched_expectation_id,
        coverage_evidence_sha256=coverage_evidence_sha256,
        observed_boundary_sha256=observed_boundary_sha256,
        alternative_evaluations=alternative_evaluations,
        findings=canonical_findings,
    )
    draft = LogicalStructureQualityEvaluationReportV1.model_construct(
        **values,
        report_sha256="0" * 64,
    )
    return LogicalStructureQualityEvaluationReportV1(
        **values,
        report_sha256=stable_logical_structure_quality_report_sha256(draft),
    )


def _proposal_receipt_lineage_matches(
    proposal: D2LogicalStructureProposalV1,
    receipt: D2LogicalStructureValidationReceiptV1,
) -> bool:
    unresolved_source_ids = tuple(
        item.disposition_id
        for item in proposal.source_evidence_dispositions
        if item.disposition is D2SourceEvidenceDispositionKind.UNRESOLVED
    )
    unresolved_table_ids = tuple(
        item.table_id
        for item in proposal.physical_table_dispositions
        if item.disposition is D2PhysicalTableDispositionKind.UNRESOLVED
    )
    return (
        receipt.run_id == proposal.run_id
        and receipt.source_sha256 == proposal.source_sha256
        and receipt.source_ir_id == proposal.source_ir_id
        and receipt.source_ir_canonical_sha256 == proposal.source_ir_canonical_sha256
        and receipt.kit_identity == proposal.kit_identity
        and receipt.predecessor_d1_artifact_id
        == proposal.predecessor_d1_artifact.artifact_id
        and receipt.predecessor_d1_artifact_sha256
        == proposal.predecessor_d1_artifact.artifact_sha256
        and receipt.d1_table_map_artifact_id
        == proposal.d1_table_map_artifact.artifact_id
        and receipt.d1_table_map_artifact_sha256
        == proposal.d1_table_map_artifact.artifact_sha256
        and receipt.status is proposal.status
        and receipt.logical_object_ids
        == tuple(item.logical_object_id for item in proposal.logical_objects)
        and receipt.source_segment_ids
        == tuple(
            segment.segment_id
            for item in proposal.logical_objects
            for segment in item.source_segments
        )
        and receipt.logical_table_scope_ids
        == tuple(item.logical_object_id for item in proposal.logical_table_scopes)
        and receipt.source_evidence_disposition_ids
        == tuple(item.disposition_id for item in proposal.source_evidence_dispositions)
        and receipt.unresolved_source_evidence_disposition_ids == unresolved_source_ids
        and receipt.physical_table_ids
        == tuple(item.table_id for item in proposal.physical_table_dispositions)
        and receipt.unresolved_table_ids == unresolved_table_ids
        and receipt.proposal_sha256 == proposal.proposal_sha256
        and receipt.artifact_sha256 == proposal.proposal_sha256
    )


def _coverage_finding_codes(
    proposal: D2LogicalStructureProposalV1,
    receipt: D2LogicalStructureValidationReceiptV1,
    *,
    oracle: LogicalStructureQualityOracleV1,
) -> tuple[str, ...]:
    codes: set[str] = set()
    if (
        proposal.status is not D2LogicalStructureStatus.COMPLETE
        or receipt.status is not D2LogicalStructureStatus.COMPLETE
    ):
        codes.add("structure_status_partial")
    if receipt.unresolved_source_evidence_disposition_ids:
        codes.add("unresolved_source_evidence")
    if receipt.unresolved_table_ids:
        codes.add("unresolved_physical_table")
    disposition_ids = tuple(
        item.table_id for item in proposal.physical_table_dispositions
    )
    if (
        disposition_ids != oracle.expected_physical_table_ids
        or receipt.physical_table_ids != oracle.expected_physical_table_ids
    ):
        codes.add("physical_table_disposition_coverage_mismatch")
    if not _proposal_receipt_lineage_matches(proposal, receipt):
        codes.add("receipt_proposal_coverage_drift")
    return tuple(sorted(codes))


def _coverage_evidence_sha256(
    proposal: D2LogicalStructureProposalV1,
    receipt: D2LogicalStructureValidationReceiptV1,
    *,
    oracle: LogicalStructureQualityOracleV1,
) -> str:
    return _canonical_sha256(
        {
            "proposal_sha256": proposal.proposal_sha256,
            "receipt_sha256": receipt.receipt_sha256,
            "receipt_evidence_sha256": receipt.evidence_sha256,
            "source_segment_ids": list(receipt.source_segment_ids),
            "source_evidence_disposition_ids": list(
                receipt.source_evidence_disposition_ids
            ),
            "physical_table_ids": list(receipt.physical_table_ids),
            "unresolved_source_evidence_disposition_ids": list(
                receipt.unresolved_source_evidence_disposition_ids
            ),
            "unresolved_table_ids": list(receipt.unresolved_table_ids),
            "oracle_expected_physical_table_ids": list(
                oracle.expected_physical_table_ids
            ),
        }
    )


def _proposal_table_scope_matches_source(
    proposal: D2LogicalStructureProposalV1,
    table_catalog: dict[str, TableIR],
) -> bool:
    disposition_by_table = {
        item.table_id: item for item in proposal.physical_table_dispositions
    }
    if set(disposition_by_table) != set(table_catalog):
        return False
    seen_scope_tables: list[str] = []
    for scope in proposal.logical_table_scopes:
        for part in scope.physical_parts:
            table = table_catalog.get(part.table_id)
            if table is None or set(part.cell_ids) != {
                cell.cell_id for cell in table.cells
            }:
                return False
            disposition = disposition_by_table[part.table_id]
            if (
                disposition.disposition
                is not D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART
                or disposition.logical_table_scope_id != scope.logical_object_id
            ):
                return False
            seen_scope_tables.append(part.table_id)
    return len(seen_scope_tables) == len(set(seen_scope_tables)) and set(
        seen_scope_tables
    ) == {
        table_id
        for table_id, item in disposition_by_table.items()
        if item.disposition is D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART
    }


def _physical_table_catalog(source_ir: SourceIRSnapshot) -> dict[str, TableIR]:
    catalog: dict[str, TableIR] = {}
    for node in source_ir.nodes:
        if node.kind is not SourceNodeKind.TABLE or node.table_payload is None:
            continue
        table = node.table_payload
        if table.table_id in catalog:
            raise ValueError("SourceIR contains an ambiguous physical TableIR ID")
        catalog[table.table_id] = table
    return catalog


def _expectation_regions(
    expectation: LogicalStructureBoundaryExpectationV1,
) -> tuple[LogicalStructureTableRegionV1, ...]:
    return (
        *(
            region
            for group in expectation.logical_table_groups
            for region in group.regions
        ),
        *(item.region for item in expectation.nested_regions),
        *expectation.false_regions,
    )


def _resolve_expectation_regions(
    expectation: LogicalStructureBoundaryExpectationV1,
    table_catalog: dict[str, TableIR],
) -> dict[str, frozenset[str]]:
    return {
        region.region_id: _resolve_region(region, table_catalog)
        for region in _expectation_regions(expectation)
    }


def _resolve_region(
    region: LogicalStructureTableRegionV1,
    table_catalog: dict[str, TableIR],
) -> frozenset[str]:
    table = table_catalog.get(region.table_id)
    if table is None:
        raise ValueError("oracle table region references an unknown physical table")
    cell_by_id = {cell.cell_id: cell for cell in table.cells}
    if region.selector_kind is LogicalStructureTableRegionSelectorKind.WHOLE_TABLE:
        selected = set(cell_by_id)
    elif region.selector_kind is LogicalStructureTableRegionSelectorKind.CELL_IDS:
        if set(region.cell_ids) - set(cell_by_id):
            raise ValueError("oracle table region references an unknown cell")
        selected = set(region.cell_ids)
    elif region.selector_kind is LogicalStructureTableRegionSelectorKind.ROW_INDICES:
        selected = {
            cell.cell_id for cell in table.cells if cell.row in set(region.row_indices)
        }
    elif region.selector_kind is LogicalStructureTableRegionSelectorKind.COLUMN_INDICES:
        selected = {
            cell.cell_id
            for cell in table.cells
            if cell.column in set(region.column_indices)
        }
    elif (
        region.selector_kind is LogicalStructureTableRegionSelectorKind.CELL_COORDINATES
    ):
        positions = {(item.row, item.column) for item in region.cell_coordinates}
        selected = {
            cell.cell_id for cell in table.cells if (cell.row, cell.column) in positions
        }
        if len(selected) != len(positions):
            raise ValueError("oracle table region references an unknown coordinate")
    else:
        assert region.bbox is not None
        selected = {
            cell.cell_id
            for cell in table.cells
            if cell.native_locator.page_number == region.bbox.page_number
            and cell.native_locator.bbox is not None
            and _bboxes_overlap(cell.native_locator.bbox, region.bbox)
        }
    if not selected:
        raise ValueError("oracle table region resolved to no SourceIR cells")
    return frozenset(selected)


def _bboxes_overlap(
    cell_bbox: tuple[float, float, float, float],
    region_bbox: LogicalStructureRegionBboxV1,
) -> bool:
    x0, y0, x1, y1 = cell_bbox
    return max(x0, region_bbox.x0) < min(x1, region_bbox.x1) and max(
        y0, region_bbox.y0
    ) < min(y1, region_bbox.y1)


def _region_partition_codes(
    resolved_regions: dict[str, frozenset[str]],
    *,
    table_catalog: dict[str, TableIR],
) -> set[str]:
    expected_cells = {
        cell.cell_id for table in table_catalog.values() for cell in table.cells
    }
    counts = Counter(
        cell_id for cell_ids in resolved_regions.values() for cell_id in cell_ids
    )
    codes: set[str] = set()
    if set(counts) != expected_cells:
        codes.add("oracle_region_partition_gap")
    if any(count != 1 for count in counts.values()):
        codes.add("oracle_region_partition_overlap")
    return codes


def _region_signature(
    table_id: str,
    cell_ids: frozenset[str] | tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    return (table_id, tuple(sorted(cell_ids)))


def _observed_boundary_payload(
    proposal: D2LogicalStructureProposalV1,
    table_catalog: dict[str, TableIR],
) -> dict[str, object]:
    page_owners = sorted(
        (
            item.object_kind.value,
            list(item.page_numbers),
        )
        for item in proposal.logical_objects
        if item.object_kind
        in {D2LogicalObjectKind.DOCUMENT_UNIT, D2LogicalObjectKind.ATTACHMENT}
    )
    groups = sorted(
        (
            tuple(
                _region_signature(
                    part.table_id,
                    frozenset(part.cell_ids),
                )
                for part in scope.physical_parts
            )
            for scope in proposal.logical_table_scopes
        ),
        key=repr,
    )
    full_region_by_table = {
        table_id: _region_signature(
            table_id,
            frozenset(cell.cell_id for cell in table.cells),
        )
        for table_id, table in table_catalog.items()
    }
    nested = sorted(
        (
            full_region_by_table[item.table_id],
            full_region_by_table[item.nested_owner_table_id or ""],
        )
        for item in proposal.physical_table_dispositions
        if item.disposition is D2PhysicalTableDispositionKind.NESTED_VALUE_TABLE
    )
    false_regions = sorted(
        full_region_by_table[item.table_id]
        for item in proposal.physical_table_dispositions
        if item.disposition is D2PhysicalTableDispositionKind.NON_BUSINESS_FALSE_TABLE
    )
    return {
        "page_owners": page_owners,
        "logical_table_groups": groups,
        "continuation_pairs": sorted(
            pair for group in groups for pair in zip(group, group[1:], strict=False)
        ),
        "nested_regions": nested,
        "false_regions": false_regions,
    }


def _empty_observed_boundary_payload() -> dict[str, object]:
    return {
        "page_owners": [],
        "logical_table_groups": [],
        "continuation_pairs": [],
        "nested_regions": [],
        "false_regions": [],
    }


def _expected_boundary_payload(
    expectation: LogicalStructureBoundaryExpectationV1,
    resolved_regions: dict[str, frozenset[str]],
) -> dict[str, object]:
    region_by_id = {
        region.region_id: region for region in _expectation_regions(expectation)
    }
    page_owners = sorted(
        (item.object_kind.value, list(item.page_numbers))
        for item in expectation.page_owners
    )
    groups = sorted(
        (
            tuple(
                _region_signature(region.table_id, resolved_regions[region.region_id])
                for region in group.regions
            )
            for group in expectation.logical_table_groups
        ),
        key=repr,
    )
    continuation_pairs = sorted(
        (
            _region_signature(
                region_by_id[pair.first_region_id].table_id,
                resolved_regions[pair.first_region_id],
            ),
            _region_signature(
                region_by_id[pair.second_region_id].table_id,
                resolved_regions[pair.second_region_id],
            ),
        )
        for group in expectation.logical_table_groups
        for pair in group.continuation_pairs
    )
    nested = sorted(
        (
            _region_signature(
                item.region.table_id,
                resolved_regions[item.region.region_id],
            ),
            _region_signature(
                region_by_id[item.owner_region_id].table_id,
                resolved_regions[item.owner_region_id],
            ),
        )
        for item in expectation.nested_regions
    )
    false_regions = sorted(
        _region_signature(region.table_id, resolved_regions[region.region_id])
        for region in expectation.false_regions
    )
    return {
        "page_owners": page_owners,
        "logical_table_groups": groups,
        "continuation_pairs": continuation_pairs,
        "nested_regions": nested,
        "false_regions": false_regions,
    }


def _expected_boundary_sha256(
    expectation: LogicalStructureBoundaryExpectationV1,
    resolved_regions: dict[str, frozenset[str]],
) -> str:
    return _canonical_sha256(
        _expected_boundary_payload(expectation, resolved_regions)
        | {
            "expectation_id": expectation.expectation_id,
            "must_separate_pairs": [
                [item.first_region_id, item.second_region_id]
                for item in expectation.must_separate_pairs
            ],
        }
    )


def _evaluate_boundary_expectation(
    expectation: LogicalStructureBoundaryExpectationV1,
    *,
    resolved_regions: dict[str, frozenset[str]],
    observed_boundary: dict[str, object],
) -> tuple[LogicalStructureQualityFindingV1, ...]:
    expected = _expected_boundary_payload(expectation, resolved_regions)
    findings: list[LogicalStructureQualityFindingV1] = []

    comparisons = (
        ("page_owners", "page_ownership_mismatch"),
        ("logical_table_groups", "logical_table_grouping_mismatch"),
        ("continuation_pairs", "continuation_mismatch"),
        ("nested_regions", "nested_region_disposition_mismatch"),
        ("false_regions", "false_region_disposition_mismatch"),
    )
    for key, code in comparisons:
        if observed_boundary[key] != expected[key]:
            findings.append(
                _finding(
                    LogicalStructureQualityAxis.BOUNDARY,
                    code,
                    (expectation.expectation_id,),
                )
            )

    expected_region_signatures = {
        _region_signature(region.table_id, resolved_regions[region.region_id])
        for region in _expectation_regions(expectation)
    }
    observed_region_signatures = (
        {
            signature
            for group in observed_boundary["logical_table_groups"]
            for signature in group
        }
        | {
            signature
            for nested, owner in observed_boundary["nested_regions"]
            for signature in (nested, owner)
        }
        | set(observed_boundary["false_regions"])
    )
    if not expected_region_signatures.issubset(observed_region_signatures):
        findings.append(
            _finding(
                LogicalStructureQualityAxis.BOUNDARY,
                "region_granularity_mismatch",
                (expectation.expectation_id,),
            )
        )

    region_by_id = {
        region.region_id: region for region in _expectation_regions(expectation)
    }
    observed_groups = tuple(observed_boundary["logical_table_groups"])
    for pair in expectation.must_separate_pairs:
        first = region_by_id[pair.first_region_id]
        second = region_by_id[pair.second_region_id]
        first_atoms = {
            (first.table_id, cell_id) for cell_id in resolved_regions[first.region_id]
        }
        second_atoms = {
            (second.table_id, cell_id) for cell_id in resolved_regions[second.region_id]
        }
        if any(
            first_atoms.issubset(_group_atoms(group))
            and second_atoms.issubset(_group_atoms(group))
            for group in observed_groups
        ):
            findings.append(
                _finding(
                    LogicalStructureQualityAxis.BOUNDARY,
                    "must_separate_boundary_merged",
                    tuple(sorted((pair.first_region_id, pair.second_region_id))),
                )
            )

    return tuple(
        sorted(
            set(findings),
            key=lambda item: (item.axis.value, item.code, item.subject_refs),
        )
    )


def _group_atoms(
    group: tuple[tuple[str, tuple[str, ...]], ...],
) -> set[tuple[str, str]]:
    return {(table_id, cell_id) for table_id, cell_ids in group for cell_id in cell_ids}


def _finding(
    axis: LogicalStructureQualityAxis,
    code: str,
    subject_refs: tuple[str, ...],
) -> LogicalStructureQualityFindingV1:
    return LogicalStructureQualityFindingV1(
        axis=axis,
        code=code,
        subject_refs=tuple(sorted(set(subject_refs))),
    )


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
    "LOGICAL_STRUCTURE_QUALITY_EVALUATOR_ID",
    "LOGICAL_STRUCTURE_QUALITY_EVALUATOR_VERSION",
    "LOGICAL_STRUCTURE_QUALITY_ORACLE_SCHEMA_VERSION",
    "LOGICAL_STRUCTURE_QUALITY_REPORT_SCHEMA_VERSION",
    "LogicalStructureAlternativeEvaluationV1",
    "LogicalStructureBoundaryExpectationV1",
    "LogicalStructureCellCoordinateV1",
    "LogicalStructureExpectedNestedRegionV1",
    "LogicalStructureExpectedPageOwnerV1",
    "LogicalStructureExpectedTableGroupV1",
    "LogicalStructureQualityAxis",
    "LogicalStructureQualityAxisStatus",
    "LogicalStructureQualityEvaluationReportV1",
    "LogicalStructureQualityFindingV1",
    "LogicalStructureQualityOracleV1",
    "LogicalStructureQualityStatus",
    "LogicalStructureRegionBboxV1",
    "LogicalStructureRegionPairV1",
    "LogicalStructureTableRegionSelectorKind",
    "LogicalStructureTableRegionV1",
    "evaluate_logical_structure_quality",
    "stable_logical_structure_quality_oracle_sha256",
    "stable_logical_structure_quality_report_sha256",
]
