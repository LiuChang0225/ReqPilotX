from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
from typing import Literal

from reqpilotx_server.private_eval import private_eval_path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from reqpilotx_server.application.agent_workflow.d2_table_regions import (
    D2CompiledTableRegionV1,
    D2TableRegionArtifactV1,
    D2TableRegionDispositionKind,
)


PDS_PRIVATE_REGION_BOUNDARY_ORACLE_SCHEMA_VERSION = (
    "pds-private-region-boundary-oracle/v1"
)
PDS_PRIVATE_REGION_BOUNDARY_ORACLE_RESULT_SCHEMA_VERSION = (
    "pds-private-region-boundary-oracle-result/v1"
)
PDS_PRIVATE_REGION_BOUNDARY_ORACLE_AUTHORITY = "AI_REVIEWED_PRIVATE_REFERENCE_NOT_GOLD"
PDS_PRIVATE_REGION_BOUNDARY_SOURCE_SHA256 = (
    "81a178bb7affc33a74bf0c9ff00f6c605a6fae1530d13c482606a086031fdb97"
)
PDS_PRIVATE_REGION_BOUNDARY_REFERENCE_BUNDLE_SHA256 = (
    "00ce3dcf4ef62437b2ca06d637e293495248a8718e398867b6793f019934f950"
)
PDS_PRIVATE_REGION_BOUNDARY_ORACLE_PATH = private_eval_path(
    "private_oracles/pds_region_boundary_oracle.v1.json"
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class PdsPrivateNestedOwnerV1(_StrictFrozenModel):
    table_ordinal: StrictInt = Field(ge=1, le=30)
    region_ordinal: StrictInt = Field(ge=1)


class PdsPrivateRegionExpectationV1(_StrictFrozenModel):
    cell_span: tuple[StrictInt, StrictInt] | Literal["WHOLE_TABLE"]
    disposition: D2TableRegionDispositionKind
    scope_label: str | None = Field(default=None, pattern=r"^L(?:[1-9]|1[0-5])$")
    nested_owner: PdsPrivateNestedOwnerV1 | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> PdsPrivateRegionExpectationV1:
        if isinstance(self.cell_span, tuple):
            if self.cell_span[1] <= self.cell_span[0]:
                raise ValueError("private oracle cell spans must be non-empty")
        scope = self.disposition is D2TableRegionDispositionKind.LOGICAL_SCOPE_PART
        nested = self.disposition is D2TableRegionDispositionKind.NESTED_VALUE_REGION
        if (self.scope_label is not None) != scope:
            raise ValueError("private oracle scope labels must match disposition")
        if (self.nested_owner is not None) != nested:
            raise ValueError("private oracle nested owners must match disposition")
        if self.disposition is D2TableRegionDispositionKind.UNRESOLVED:
            raise ValueError("private reviewed oracle cannot expect unresolved regions")
        return self


class PdsPrivateTableExpectationV1(_StrictFrozenModel):
    table_ordinal: StrictInt = Field(ge=1, le=30)
    regions: tuple[PdsPrivateRegionExpectationV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_regions(self) -> PdsPrivateTableExpectationV1:
        whole = tuple(item for item in self.regions if item.cell_span == "WHOLE_TABLE")
        if whole and (len(whole) != 1 or len(self.regions) != 1):
            raise ValueError("WHOLE_TABLE must be the only expected region")
        numeric = tuple(
            item.cell_span for item in self.regions if isinstance(item.cell_span, tuple)
        )
        if numeric and numeric != tuple(sorted(numeric)):
            raise ValueError("private oracle regions must follow cell order")
        if any(
            previous[1] != current[0] for previous, current in zip(numeric, numeric[1:])
        ):
            raise ValueError("private oracle numeric regions must be contiguous")
        return self


class PdsPrivateRegionBoundaryOracleV1(_StrictFrozenModel):
    schema_version: Literal["pds-private-region-boundary-oracle/v1"] = (
        PDS_PRIVATE_REGION_BOUNDARY_ORACLE_SCHEMA_VERSION
    )
    oracle_id: Literal["pds-active-damper-region-boundary-v1"]
    authority: Literal["AI_REVIEWED_PRIVATE_REFERENCE_NOT_GOLD"]
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    reference_workspace_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_physical_table_count: Literal[30]
    front_boundary_table_ordinals: tuple[StrictInt, ...]
    tail_table_ordinals: tuple[StrictInt, ...]
    tables: tuple[PdsPrivateTableExpectationV1, ...] = Field(
        min_length=30, max_length=30
    )

    @field_validator("front_boundary_table_ordinals", "tail_table_ordinals")
    @classmethod
    def validate_ordinals(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(
            not 1 <= item <= 30 for item in value
        ):
            raise ValueError("private oracle table ordinals must be canonical")
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> PdsPrivateRegionBoundaryOracleV1:
        if self.source_sha256 != PDS_PRIVATE_REGION_BOUNDARY_SOURCE_SHA256:
            raise ValueError("private oracle source identity drifted")
        if (
            self.reference_workspace_bundle_sha256
            != PDS_PRIVATE_REGION_BOUNDARY_REFERENCE_BUNDLE_SHA256
        ):
            raise ValueError("private oracle reference workspace drifted")
        if tuple(item.table_ordinal for item in self.tables) != tuple(range(1, 31)):
            raise ValueError("private oracle must cover T1 through T30 exactly")
        if set(self.front_boundary_table_ordinals).intersection(
            self.tail_table_ordinals
        ):
            raise ValueError("private oracle front and tail table sets overlap")
        if set((*self.front_boundary_table_ordinals, *self.tail_table_ordinals)) != set(
            range(1, 31)
        ):
            raise ValueError("private oracle front and tail sets must cover all tables")
        scope_labels = {
            item.scope_label
            for table in self.tables
            for item in table.regions
            if item.scope_label is not None
        }
        if scope_labels != {f"L{ordinal}" for ordinal in range(1, 16)}:
            raise ValueError("private oracle must define L1 through L15")
        for table in self.tables:
            for region in table.regions:
                owner = region.nested_owner
                if owner is None:
                    continue
                owner_table = self.tables[owner.table_ordinal - 1]
                if owner.region_ordinal > len(owner_table.regions):
                    raise ValueError("private oracle nested owner is out of bounds")
        return self


def load_pds_private_region_boundary_oracle(
    path: Path = PDS_PRIVATE_REGION_BOUNDARY_ORACLE_PATH,
) -> tuple[PdsPrivateRegionBoundaryOracleV1, str]:
    payload = path.read_bytes()
    manifest = PdsPrivateRegionBoundaryOracleV1.model_validate_json(payload)
    return manifest, hashlib.sha256(payload).hexdigest()


def evaluate_pds_private_region_boundary_oracle(
    artifact: D2TableRegionArtifactV1,
    *,
    oracle_path: Path = PDS_PRIVATE_REGION_BOUNDARY_ORACLE_PATH,
) -> dict[str, object] | None:
    compiled = D2TableRegionArtifactV1.model_validate(artifact.model_dump(mode="json"))
    manifest, manifest_sha256 = load_pds_private_region_boundary_oracle(oracle_path)
    if compiled.source_sha256 != manifest.source_sha256:
        return None

    coverage_by_table = tuple(compiled.physical_table_cell_coverage)
    observed_by_table = {
        ordinal: tuple(
            region
            for region in compiled.regions
            if ordinal <= len(coverage_by_table)
            and region.physical_table_id
            == coverage_by_table[ordinal - 1].physical_table_id
        )
        for ordinal in range(1, len(coverage_by_table) + 1)
    }
    coverage_gap_cells = 0
    coverage_overlap_cells = 0
    expected_source_drift_tables: list[int] = []
    boundary_gap_tables: list[int] = []
    expected_regions: dict[
        tuple[int, int], tuple[int, int, D2TableRegionDispositionKind]
    ] = {}
    matched_regions: dict[tuple[int, int], D2CompiledTableRegionV1] = {}

    for table_ordinal, coverage in enumerate(coverage_by_table, start=1):
        cell_count = len(coverage.canonical_cell_ids)
        counts: Counter[int] = Counter()
        for region in observed_by_table[table_ordinal]:
            for span in region.cell_spans:
                counts.update(
                    range(span.start_cell_ordinal, span.end_cell_ordinal_exclusive)
                )
        coverage_gap_cells += sum(counts[ordinal] == 0 for ordinal in range(cell_count))
        coverage_overlap_cells += sum(
            counts[ordinal] > 1 for ordinal in range(cell_count)
        )

        if table_ordinal > len(manifest.tables):
            continue
        table_expected = manifest.tables[table_ordinal - 1]
        resolved_expected = tuple(
            _resolve_expected_region(item, cell_count=cell_count)
            for item in table_expected.regions
        )
        if (
            not resolved_expected
            or resolved_expected[0][0] != 0
            or resolved_expected[-1][1] != cell_count
            or any(
                previous[1] != current[0]
                for previous, current in zip(
                    resolved_expected,
                    resolved_expected[1:],
                )
            )
        ):
            expected_source_drift_tables.append(table_ordinal)
        for region_ordinal, (start, end, disposition) in enumerate(
            resolved_expected,
            start=1,
        ):
            expected_regions[(table_ordinal, region_ordinal)] = (
                start,
                end,
                disposition,
            )
            match = _find_exact_region(
                observed_by_table[table_ordinal],
                start=start,
                end=end,
                disposition=disposition,
            )
            if match is not None:
                matched_regions[(table_ordinal, region_ordinal)] = match
        observed_signatures = tuple(
            _region_signature(item) for item in observed_by_table[table_ordinal]
        )
        if observed_signatures != resolved_expected:
            boundary_gap_tables.append(table_ordinal)

    missing_physical_tables = max(
        0,
        manifest.expected_physical_table_count - len(coverage_by_table),
    )
    extra_physical_tables = max(
        0,
        len(coverage_by_table) - manifest.expected_physical_table_count,
    )
    if missing_physical_tables:
        boundary_gap_tables.extend(
            range(
                len(coverage_by_table) + 1, manifest.expected_physical_table_count + 1
            )
        )

    scope_mismatch_labels = _scope_group_mismatches(
        manifest=manifest,
        observed_by_table=observed_by_table,
        matched_regions=matched_regions,
    )
    nested_owner_mismatch_count = _nested_owner_mismatch_count(
        manifest=manifest,
        matched_regions=matched_regions,
    )
    boundary_gap_ordinals = tuple(sorted(set(boundary_gap_tables)))
    front_gap_ordinals = tuple(
        item
        for item in boundary_gap_ordinals
        if item in set(manifest.front_boundary_table_ordinals)
    )
    tail_gap_ordinals = tuple(
        item
        for item in boundary_gap_ordinals
        if item in set(manifest.tail_table_ordinals)
    )
    tail_scope_labels = {f"L{ordinal}" for ordinal in range(10, 16)}
    tail_scope_mismatches = tuple(
        item for item in scope_mismatch_labels if item in tail_scope_labels
    )
    coverage_pass = not (
        coverage_gap_cells
        or coverage_overlap_cells
        or missing_physical_tables
        or extra_physical_tables
        or expected_source_drift_tables
    )
    boundary_pass = not (
        boundary_gap_ordinals
        or scope_mismatch_labels
        or nested_owner_mismatch_count
        or missing_physical_tables
        or extra_physical_tables
    )
    tail_grouping_pass = not (
        tail_gap_ordinals or tail_scope_mismatches or nested_owner_mismatch_count
    )
    reason_codes: list[str] = []
    if missing_physical_tables or extra_physical_tables:
        reason_codes.append("PHYSICAL_TABLE_COUNT_MISMATCH")
    if coverage_gap_cells:
        reason_codes.append("CELL_COVERAGE_GAP")
    if coverage_overlap_cells:
        reason_codes.append("CELL_COVERAGE_OVERLAP")
    if expected_source_drift_tables:
        reason_codes.append("ORACLE_SOURCE_CELL_COUNT_DRIFT")
    if boundary_gap_ordinals:
        reason_codes.append("REGION_BOUNDARY_MISMATCH")
    if front_gap_ordinals:
        reason_codes.append("FRONT_REGION_BOUNDARY_GAP")
    if scope_mismatch_labels:
        reason_codes.append("SCOPE_GROUPING_MISMATCH")
    if nested_owner_mismatch_count:
        reason_codes.append("NESTED_OWNER_MISMATCH")
    if not tail_grouping_pass:
        reason_codes.append("TAIL_GROUPING_MISMATCH")

    return {
        "schema_version": PDS_PRIVATE_REGION_BOUNDARY_ORACLE_RESULT_SCHEMA_VERSION,
        "oracle_id": manifest.oracle_id,
        "authority": manifest.authority,
        "reference_workspace_bundle_sha256": (
            manifest.reference_workspace_bundle_sha256
        ),
        "oracle_manifest_sha256": manifest_sha256,
        "verdict": "PASS" if coverage_pass and boundary_pass else "FAIL",
        "coverage_verdict": "PASS" if coverage_pass else "FAIL",
        "boundary_verdict": "PASS" if boundary_pass else "FAIL",
        "tail_grouping_verdict": "PASS" if tail_grouping_pass else "FAIL",
        "expected_physical_table_count": manifest.expected_physical_table_count,
        "observed_physical_table_count": len(coverage_by_table),
        "expected_region_count": sum(len(item.regions) for item in manifest.tables),
        "observed_region_count": len(compiled.regions),
        "coverage_gap_cell_count": coverage_gap_cells,
        "coverage_overlap_cell_count": coverage_overlap_cells,
        "region_boundary_gap_table_count": len(boundary_gap_ordinals),
        "front_region_boundary_gap_table_count": len(front_gap_ordinals),
        "tail_region_boundary_gap_table_count": len(tail_gap_ordinals),
        "scope_group_mismatch_count": len(scope_mismatch_labels),
        "tail_scope_group_mismatch_count": len(tail_scope_mismatches),
        "nested_owner_mismatch_count": nested_owner_mismatch_count,
        "boundary_gap_table_ordinals": boundary_gap_ordinals,
        "front_boundary_gap_table_ordinals": front_gap_ordinals,
        "tail_boundary_gap_table_ordinals": tail_gap_ordinals,
        "reason_codes": tuple(sorted(set(reason_codes))),
    }


def _resolve_expected_region(
    item: PdsPrivateRegionExpectationV1,
    *,
    cell_count: int,
) -> tuple[int, int, D2TableRegionDispositionKind]:
    if item.cell_span == "WHOLE_TABLE":
        return 0, cell_count, item.disposition
    return item.cell_span[0], item.cell_span[1], item.disposition


def _region_signature(
    region: D2CompiledTableRegionV1,
) -> tuple[int, int, D2TableRegionDispositionKind] | tuple[object, ...]:
    if len(region.cell_spans) != 1:
        return (
            tuple(
                (item.start_cell_ordinal, item.end_cell_ordinal_exclusive)
                for item in region.cell_spans
            ),
            region.disposition,
        )
    span = region.cell_spans[0]
    return span.start_cell_ordinal, span.end_cell_ordinal_exclusive, region.disposition


def _find_exact_region(
    regions: tuple[D2CompiledTableRegionV1, ...],
    *,
    start: int,
    end: int,
    disposition: D2TableRegionDispositionKind,
) -> D2CompiledTableRegionV1 | None:
    matches = tuple(
        item for item in regions if _region_signature(item) == (start, end, disposition)
    )
    return matches[0] if len(matches) == 1 else None


def _scope_group_mismatches(
    *,
    manifest: PdsPrivateRegionBoundaryOracleV1,
    observed_by_table: dict[int, tuple[D2CompiledTableRegionV1, ...]],
    matched_regions: dict[tuple[int, int], D2CompiledTableRegionV1],
) -> tuple[str, ...]:
    expected_by_label: dict[str, set[tuple[int, int, int]]] = {}
    for table in manifest.tables:
        for region_ordinal, region in enumerate(table.regions, start=1):
            if region.scope_label is None:
                continue
            expected = matched_regions.get((table.table_ordinal, region_ordinal))
            if expected is None:
                expected_by_label.setdefault(region.scope_label, set()).add(
                    (table.table_ordinal, -1, -1)
                )
                continue
            span = expected.cell_spans[0]
            expected_by_label.setdefault(region.scope_label, set()).add(
                (
                    table.table_ordinal,
                    span.start_cell_ordinal,
                    span.end_cell_ordinal_exclusive,
                )
            )

    mismatches: list[str] = []
    claimed_scope_ids: set[str] = set()
    for label, expected_members in expected_by_label.items():
        members = tuple(
            matched_regions.get((table.table_ordinal, region_ordinal))
            for table in manifest.tables
            for region_ordinal, region in enumerate(table.regions, start=1)
            if region.scope_label == label
        )
        scope_ids = {item.logical_scope_id for item in members if item is not None}
        if (
            None in scope_ids
            or len(scope_ids) != 1
            or any(item is None for item in members)
        ):
            mismatches.append(label)
            continue
        scope_id = next(iter(scope_ids))
        assert scope_id is not None
        if scope_id in claimed_scope_ids:
            mismatches.append(label)
            continue
        claimed_scope_ids.add(scope_id)
        actual_members = {
            (
                table_ordinal,
                region.cell_spans[0].start_cell_ordinal,
                region.cell_spans[0].end_cell_ordinal_exclusive,
            )
            for table_ordinal, regions in observed_by_table.items()
            for region in regions
            if region.logical_scope_id == scope_id and len(region.cell_spans) == 1
        }
        if actual_members != expected_members:
            mismatches.append(label)
    return tuple(sorted(set(mismatches), key=lambda value: int(value[1:])))


def _nested_owner_mismatch_count(
    *,
    manifest: PdsPrivateRegionBoundaryOracleV1,
    matched_regions: dict[tuple[int, int], D2CompiledTableRegionV1],
) -> int:
    mismatches = 0
    for table in manifest.tables:
        for region_ordinal, expected in enumerate(table.regions, start=1):
            owner = expected.nested_owner
            if owner is None:
                continue
            observed = matched_regions.get((table.table_ordinal, region_ordinal))
            observed_owner = matched_regions.get(
                (owner.table_ordinal, owner.region_ordinal)
            )
            if (
                observed is None
                or observed_owner is None
                or observed.nested_owner_region_id != observed_owner.region_id
            ):
                mismatches += 1
    return mismatches


__all__ = [
    "PDS_PRIVATE_REGION_BOUNDARY_ORACLE_AUTHORITY",
    "PDS_PRIVATE_REGION_BOUNDARY_ORACLE_PATH",
    "PDS_PRIVATE_REGION_BOUNDARY_ORACLE_RESULT_SCHEMA_VERSION",
    "PDS_PRIVATE_REGION_BOUNDARY_ORACLE_SCHEMA_VERSION",
    "PDS_PRIVATE_REGION_BOUNDARY_REFERENCE_BUNDLE_SHA256",
    "PDS_PRIVATE_REGION_BOUNDARY_SOURCE_SHA256",
    "PdsPrivateRegionBoundaryOracleV1",
    "evaluate_pds_private_region_boundary_oracle",
    "load_pds_private_region_boundary_oracle",
]
