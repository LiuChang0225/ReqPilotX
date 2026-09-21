from __future__ import annotations

import hashlib
import json
from collections import Counter
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from reqpilotx_server.application.agent_workflow.d2_table_region_parent_input import (
    D2TableRegionParentInputV1,
)
from reqpilotx_server.application.agent_workflow.d2_table_regions import (
    D2CompiledTableRegionV1,
    D2TableRegionArtifactV1,
    D2TableRegionDispositionKind,
    D2TableRegionRowMaterializerEligibility,
    d2_table_region_source_ir_canonical_sha256,
)
from reqpilotx_server.application.extraction.agent_table_boundaries import (
    AgentTableRowBindingV1,
)
from reqpilotx_server.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
)
from reqpilotx_server.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)
from reqpilotx_server.domain.extraction.structure_revision import canonical_model_sha256
from reqpilotx_server.domain.extraction.table_records import (
    TableFactBindingV1,
    TableRecordProjectionV1,
)


D2_REGION_SCOPE_MATERIALIZER_ELIGIBILITY_RECEIPT_SCHEMA_VERSION = (
    "d2-region-scope-materializer-eligibility-receipt/v1"
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_ROWS = 500_000
_MAX_REGIONS = 100_000


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D2RegionScopeRowDomainStatus(StrEnum):
    ROW_DOMAIN_READY = "ROW_DOMAIN_READY"
    ROW_DOMAIN_NOT_READY = "ROW_DOMAIN_NOT_READY"


class D2RegionScopeMaterializationStatus(StrEnum):
    MATERIALIZATION_READY = "MATERIALIZATION_READY"
    MATERIALIZATION_NOT_READY = "MATERIALIZATION_NOT_READY"


class D2RegionScopeEligibilityReason(StrEnum):
    SCOPE_FULL_ROW_EXACT_COVER = "SCOPE_FULL_ROW_EXACT_COVER"
    SCOPE_REGION_NOT_LOGICAL_SCOPE_PART = "SCOPE_REGION_NOT_LOGICAL_SCOPE_PART"
    SCOPE_REGION_ORDER_INVALID = "SCOPE_REGION_ORDER_INVALID"
    SCOPE_REGION_NOT_FULL_ROW_ELIGIBLE = "SCOPE_REGION_NOT_FULL_ROW_ELIGIBLE"
    SCOPE_SELECTED_ROW_OVERLAP = "SCOPE_SELECTED_ROW_OVERLAP"
    SCOPE_ROW_OVERLAPS_EXCLUDED = "SCOPE_ROW_OVERLAPS_EXCLUDED"
    SCOPE_REGION_CELL_EXACT_COVER_FAILED = "SCOPE_REGION_CELL_EXACT_COVER_FAILED"
    ROW_DOMAIN_NOT_READY = "ROW_DOMAIN_NOT_READY"
    ROW_ROLE_CONTRACT_MISSING = "ROW_ROLE_CONTRACT_MISSING"
    ROW_ROLE_CONTRACT_INCOMPLETE = "ROW_ROLE_CONTRACT_INCOMPLETE"
    HEADER_ROLE_MISSING = "HEADER_ROLE_MISSING"
    BODY_ROLE_MISSING = "BODY_ROLE_MISSING"
    BASE_FACT_NOT_BOUND = "BASE_FACT_NOT_BOUND"
    BASE_FACT_NOT_SCOPE_EXACT = "BASE_FACT_NOT_SCOPE_EXACT"
    SCOPE_LOCAL_MATERIALIZER_INPUTS_VERIFIED = (
        "SCOPE_LOCAL_MATERIALIZER_INPUTS_VERIFIED"
    )


class D2RegionScopeRowRole(StrEnum):
    HEADER = "HEADER"
    BODY = "BODY"
    NOTE = "NOTE"
    UNKNOWN = "UNKNOWN"


class D2RegionScopeRowRoleSource(StrEnum):
    EXPLICIT_INPUT = "EXPLICIT_INPUT"
    HOST_UNKNOWN = "HOST_UNKNOWN"


class D2RegionScopeBaseFactRelation(StrEnum):
    NOT_BOUND = "NOT_BOUND"
    SCOPE_EXACT = "SCOPE_EXACT"
    WHOLE_TABLE_SUPERSET = "WHOLE_TABLE_SUPERSET"


class D2RegionScopeExplicitRowRoleV1(_StrictFrozenModel):
    """Content-free, caller-supplied role for one exact scope-local row."""

    table_id: str = Field(min_length=1, max_length=160)
    row_index: StrictInt = Field(ge=0)
    cell_ids: tuple[str, ...] = Field(min_length=1)
    role: D2RegionScopeRowRole
    evidence_ref: str = Field(min_length=1, max_length=400)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    role_binding_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("cell_ids")
    @classmethod
    def validate_cell_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("explicit row-role cell IDs must be unique and non-empty")
        return value

    @field_validator("role")
    @classmethod
    def reject_unknown_role(
        cls,
        value: D2RegionScopeRowRole,
    ) -> D2RegionScopeRowRole:
        if value is D2RegionScopeRowRole.UNKNOWN:
            raise ValueError("explicit row-role input cannot claim UNKNOWN")
        return value

    @field_validator("evidence_ref")
    @classmethod
    def reject_blank_evidence_ref(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("explicit row-role evidence ref must not be blank")
        return value

    @model_validator(mode="after")
    def validate_binding(self) -> D2RegionScopeExplicitRowRoleV1:
        expected = stable_d2_region_scope_explicit_row_role_sha256(
            table_id=self.table_id,
            row_index=self.row_index,
            cell_ids=self.cell_ids,
            role=self.role,
            evidence_ref=self.evidence_ref,
            evidence_sha256=self.evidence_sha256,
        )
        if self.role_binding_sha256 != expected:
            raise ValueError("explicit row-role SHA must bind its exact row evidence")
        return self


class D2RegionScopePhysicalRowDomainV1(_StrictFrozenModel):
    physical_table_id: str = Field(min_length=1, max_length=160)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    physical_table_source_order: StrictInt = Field(ge=0)
    table_ir_sha256: str = Field(pattern=_SHA256_PATTERN)
    selected_region_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_REGIONS,
    )
    selected_region_sha256s: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_REGIONS,
    )
    selected_region_cell_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_ROWS,
    )
    row_bindings: tuple[AgentTableRowBindingV1, ...] = Field(
        min_length=1,
        max_length=_MAX_ROWS,
    )
    cell_exact_cover: StrictBool
    row_domain_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("selected_region_ids", "selected_region_cell_ids")
    @classmethod
    def require_unique_non_blank(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("row-domain identities must be unique and non-empty")
        return value

    @model_validator(mode="after")
    def validate_domain(self) -> D2RegionScopePhysicalRowDomainV1:
        if len(self.selected_region_ids) != len(self.selected_region_sha256s):
            raise ValueError("selected region IDs and SHAs must be one-to-one")
        if any(item.table_id != self.physical_table_id for item in self.row_bindings):
            raise ValueError("row bindings must belong to their physical table")
        row_indices = tuple(item.row_index for item in self.row_bindings)
        if row_indices != tuple(sorted(set(row_indices))):
            raise ValueError("row bindings must be unique and source ordered")
        observed_exact_cover = Counter(
            cell_id for binding in self.row_bindings for cell_id in binding.cell_ids
        ) == Counter(self.selected_region_cell_ids)
        if self.cell_exact_cover is not observed_exact_cover:
            raise ValueError("cell exact-cover marker differs from row bindings")
        expected = stable_d2_region_scope_physical_row_domain_sha256(
            physical_table_id=self.physical_table_id,
            table_source_node_id=self.table_source_node_id,
            physical_table_source_order=self.physical_table_source_order,
            table_ir_sha256=self.table_ir_sha256,
            selected_region_ids=self.selected_region_ids,
            selected_region_sha256s=self.selected_region_sha256s,
            selected_region_cell_ids=self.selected_region_cell_ids,
            row_bindings=self.row_bindings,
            cell_exact_cover=self.cell_exact_cover,
        )
        if self.row_domain_sha256 != expected:
            raise ValueError("row-domain SHA must bind exact region rows and cells")
        return self


class D2RegionScopeExcludedRowDomainV1(_StrictFrozenModel):
    region_id: str = Field(min_length=1, max_length=200)
    region_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: D2TableRegionDispositionKind
    physical_table_id: str = Field(min_length=1, max_length=160)
    physical_table_source_order: StrictInt = Field(ge=0)
    covered_row_indices: tuple[StrictInt, ...] = Field(min_length=1)
    cell_ids: tuple[str, ...] = Field(min_length=1)
    exclusion_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("covered_row_indices")
    @classmethod
    def validate_rows(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("excluded rows must be unique and source ordered")
        return value

    @field_validator("cell_ids")
    @classmethod
    def validate_cells(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("excluded cell IDs must be unique and non-empty")
        return value

    @model_validator(mode="after")
    def validate_exclusion(self) -> D2RegionScopeExcludedRowDomainV1:
        expected = stable_d2_region_scope_exclusion_sha256(
            region_id=self.region_id,
            region_sha256=self.region_sha256,
            disposition=self.disposition,
            physical_table_id=self.physical_table_id,
            physical_table_source_order=self.physical_table_source_order,
            covered_row_indices=self.covered_row_indices,
            cell_ids=self.cell_ids,
        )
        if self.exclusion_sha256 != expected:
            raise ValueError("excluded row-domain SHA must bind exact region cells")
        return self


class D2RegionScopeResolvedRowRoleV1(_StrictFrozenModel):
    row_binding: AgentTableRowBindingV1
    role: D2RegionScopeRowRole
    role_source: D2RegionScopeRowRoleSource
    explicit_role_binding_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    resolved_role_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_role(self) -> D2RegionScopeResolvedRowRoleV1:
        if self.role_source is D2RegionScopeRowRoleSource.HOST_UNKNOWN:
            if (
                self.role is not D2RegionScopeRowRole.UNKNOWN
                or self.explicit_role_binding_sha256 is not None
            ):
                raise ValueError("Host-unknown rows must remain UNKNOWN")
        elif (
            self.role is D2RegionScopeRowRole.UNKNOWN
            or self.explicit_role_binding_sha256 is None
        ):
            raise ValueError("explicit row roles require their exact input binding")
        expected = stable_d2_region_scope_resolved_row_role_sha256(
            row_binding=self.row_binding,
            role=self.role,
            role_source=self.role_source,
            explicit_role_binding_sha256=self.explicit_role_binding_sha256,
        )
        if self.resolved_role_sha256 != expected:
            raise ValueError("resolved row-role SHA must bind its exact row")
        return self


class D2RegionScopeBaseFactBindingV1(_StrictFrozenModel):
    relation: D2RegionScopeBaseFactRelation
    fact_binding_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    structure_snapshot_id: str | None = Field(default=None, max_length=200)
    logical_table_id: str | None = Field(default=None, max_length=200)
    projection_id: str | None = Field(default=None, max_length=200)
    projection_revision: StrictInt | None = Field(default=None, ge=0)
    bound_table_ids: tuple[str, ...] = ()
    bound_table_part_sha256s: tuple[str, ...] = ()
    selected_cell_ids_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_binding(self) -> D2RegionScopeBaseFactBindingV1:
        if len(self.bound_table_ids) != len(self.bound_table_part_sha256s):
            raise ValueError("base-fact table IDs and SHAs must be one-to-one")
        if len(self.bound_table_ids) != len(set(self.bound_table_ids)):
            raise ValueError("base-fact table IDs must be unique")
        identity_values = (
            self.fact_binding_sha256,
            self.structure_snapshot_id,
            self.logical_table_id,
            self.selected_cell_ids_sha256,
        )
        if self.relation is D2RegionScopeBaseFactRelation.NOT_BOUND:
            if any(value is not None for value in identity_values) or any(
                (
                    self.projection_id is not None,
                    self.projection_revision is not None,
                    bool(self.bound_table_ids),
                    bool(self.bound_table_part_sha256s),
                )
            ):
                raise ValueError("unbound base fact cannot carry an identity")
        elif (
            any(value is None for value in identity_values) or not self.bound_table_ids
        ):
            raise ValueError("bound base fact requires complete content-free identity")
        if (self.projection_id is None) is not (self.projection_revision is None):
            raise ValueError("projection ID and revision must be present together")
        expected = stable_d2_region_scope_base_fact_binding_sha256(
            relation=self.relation,
            fact_binding_sha256=self.fact_binding_sha256,
            structure_snapshot_id=self.structure_snapshot_id,
            logical_table_id=self.logical_table_id,
            projection_id=self.projection_id,
            projection_revision=self.projection_revision,
            bound_table_ids=self.bound_table_ids,
            bound_table_part_sha256s=self.bound_table_part_sha256s,
            selected_cell_ids_sha256=self.selected_cell_ids_sha256,
        )
        if self.binding_sha256 != expected:
            raise ValueError("base-fact binding SHA must bind its exact relation")
        return self


class D2RegionScopeMaterializerEligibilityReceiptV1(_StrictFrozenModel):
    """Content-free proof that a table region can enter a row materializer.

    Row-domain readiness and materialization readiness are deliberately
    separate. A full-row scope can be safely isolated before a verified
    scope-local header/body role contract or exact base fact exists.
    """

    schema_version: Literal["d2-region-scope-materializer-eligibility-receipt/v1"] = (
        D2_REGION_SCOPE_MATERIALIZER_ELIGIBILITY_RECEIPT_SCHEMA_VERSION
    )
    content_free: Literal[True] = True
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN)
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    d1_table_map_artifact_id: str = Field(min_length=1, max_length=240)
    d1_table_map_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    region_artifact_id: str = Field(min_length=1, max_length=200)
    region_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_input_id: str = Field(min_length=1, max_length=200)
    parent_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    logical_scope_id: str = Field(min_length=1, max_length=200)
    compiled_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    ordered_region_ids: tuple[str, ...] = Field(min_length=1, max_length=_MAX_REGIONS)
    ordered_region_sha256s: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_REGIONS,
    )
    physical_row_domains: tuple[D2RegionScopePhysicalRowDomainV1, ...] = Field(
        min_length=1,
    )
    excluded_row_domains: tuple[D2RegionScopeExcludedRowDomainV1, ...] = ()
    selected_row_overlap_coordinates: tuple[tuple[str, StrictInt], ...] = ()
    excluded_row_overlap_coordinates: tuple[tuple[str, StrictInt], ...] = ()
    resolved_row_roles: tuple[D2RegionScopeResolvedRowRoleV1, ...] = Field(
        min_length=1,
    )
    base_fact_binding: D2RegionScopeBaseFactBindingV1
    row_domain_status: D2RegionScopeRowDomainStatus
    row_domain_reason_codes: tuple[D2RegionScopeEligibilityReason, ...] = Field(
        min_length=1,
    )
    materialization_status: D2RegionScopeMaterializationStatus
    materialization_reason_codes: tuple[D2RegionScopeEligibilityReason, ...] = Field(
        min_length=1,
    )
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("row_domain_reason_codes", "materialization_reason_codes")
    @classmethod
    def validate_reasons(
        cls,
        value: tuple[D2RegionScopeEligibilityReason, ...],
    ) -> tuple[D2RegionScopeEligibilityReason, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.value)):
            raise ValueError("eligibility reasons must be unique and canonical")
        return value

    @field_validator(
        "selected_row_overlap_coordinates",
        "excluded_row_overlap_coordinates",
    )
    @classmethod
    def validate_overlap_coordinates(
        cls,
        value: tuple[tuple[str, int], ...],
    ) -> tuple[tuple[str, int], ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("overlap coordinates must be unique and canonical")
        return value

    @model_validator(mode="after")
    def validate_receipt(self) -> D2RegionScopeMaterializerEligibilityReceiptV1:
        if len(self.ordered_region_ids) != len(self.ordered_region_sha256s):
            raise ValueError("scope region IDs and SHAs must be one-to-one")
        if len(self.ordered_region_ids) != len(set(self.ordered_region_ids)):
            raise ValueError("scope region IDs must be unique")
        physical_orders = tuple(
            item.physical_table_source_order for item in self.physical_row_domains
        )
        if physical_orders != tuple(sorted(set(physical_orders))):
            raise ValueError("physical row domains must follow unique source order")
        rows = tuple(
            binding
            for domain in self.physical_row_domains
            for binding in domain.row_bindings
        )
        if tuple(item.row_binding for item in self.resolved_row_roles) != rows:
            raise ValueError("resolved row roles must exactly cover scope rows")
        ready_row_domain = (
            not self.selected_row_overlap_coordinates
            and not self.excluded_row_overlap_coordinates
            and all(item.cell_exact_cover for item in self.physical_row_domains)
            and self.row_domain_reason_codes
            == (D2RegionScopeEligibilityReason.SCOPE_FULL_ROW_EXACT_COVER,)
        )
        if (
            self.row_domain_status is D2RegionScopeRowDomainStatus.ROW_DOMAIN_READY
        ) is not ready_row_domain:
            raise ValueError("row-domain status differs from its proof")
        if (
            self.materialization_status
            is D2RegionScopeMaterializationStatus.MATERIALIZATION_READY
        ):
            if (
                self.row_domain_status
                is not D2RegionScopeRowDomainStatus.ROW_DOMAIN_READY
            ):
                raise ValueError(
                    "materialization cannot be ready before its row domain"
                )
            if any(
                item.role is D2RegionScopeRowRole.UNKNOWN
                for item in self.resolved_row_roles
            ):
                raise ValueError("materialization-ready rows require explicit roles")
            roles = {item.role for item in self.resolved_row_roles}
            if not {
                D2RegionScopeRowRole.HEADER,
                D2RegionScopeRowRole.BODY,
            }.issubset(roles):
                raise ValueError("materialization-ready scope requires header and body")
            if (
                self.base_fact_binding.relation
                is not D2RegionScopeBaseFactRelation.SCOPE_EXACT
            ):
                raise ValueError(
                    "materialization-ready scope requires an exact base fact"
                )
            if self.materialization_reason_codes != (
                D2RegionScopeEligibilityReason.SCOPE_LOCAL_MATERIALIZER_INPUTS_VERIFIED,
            ):
                raise ValueError("materialization-ready reason is inconsistent")
        expected = stable_d2_region_scope_materializer_eligibility_receipt_sha256(
            **self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("eligibility receipt SHA must bind the complete proof")
        return self


def build_d2_region_scope_explicit_row_role(
    *,
    table_id: str,
    row_index: int,
    cell_ids: tuple[str, ...],
    role: D2RegionScopeRowRole,
    evidence_ref: str,
    evidence_sha256: str,
) -> D2RegionScopeExplicitRowRoleV1:
    return D2RegionScopeExplicitRowRoleV1(
        table_id=table_id,
        row_index=row_index,
        cell_ids=cell_ids,
        role=role,
        evidence_ref=evidence_ref,
        evidence_sha256=evidence_sha256,
        role_binding_sha256=stable_d2_region_scope_explicit_row_role_sha256(
            table_id=table_id,
            row_index=row_index,
            cell_ids=cell_ids,
            role=role,
            evidence_ref=evidence_ref,
            evidence_sha256=evidence_sha256,
        ),
    )


def build_d2_region_scope_materializer_eligibility_receipt(
    source_ir: SourceIRSnapshot,
    region_artifact: D2TableRegionArtifactV1,
    parent_input: D2TableRegionParentInputV1,
    *,
    logical_scope_id: str,
    explicit_row_roles: tuple[D2RegionScopeExplicitRowRoleV1, ...] = (),
    base_projection: TableRecordProjectionV1 | None = None,
    base_table_fact: TableFactBindingV1 | None = None,
) -> D2RegionScopeMaterializerEligibilityReceiptV1:
    """Assess a region scope without invoking or mutating the materializer."""

    source = SourceIRSnapshot.model_validate(source_ir.model_dump(mode="json"))
    artifact = D2TableRegionArtifactV1.model_validate(
        region_artifact.model_dump(mode="json")
    )
    stage_input = D2TableRegionParentInputV1.model_validate(
        parent_input.model_dump(mode="json")
    )
    if source.status is SourceIRStatus.FAILED:
        raise ValueError("failed SourceIR cannot produce a scope row domain")
    source_canonical_sha256 = d2_table_region_source_ir_canonical_sha256(source)
    _validate_input_lineage(
        source=source,
        source_canonical_sha256=source_canonical_sha256,
        artifact=artifact,
        stage_input=stage_input,
    )
    table_catalog = _table_catalog(source)
    _validate_artifact_tables(artifact=artifact, table_catalog=table_catalog)
    _validate_parent_cell_ledgers(artifact=artifact, stage_input=stage_input)

    scope = next(
        (
            item
            for item in artifact.logical_scopes
            if item.logical_scope_id == logical_scope_id
        ),
        None,
    )
    if scope is None:
        raise ValueError("logical scope is not present in the table-region artifact")
    parent_scope = next(
        (
            item
            for item in stage_input.logical_scopes
            if item.logical_scope_id == logical_scope_id
        ),
        None,
    )
    if parent_scope is None:
        raise ValueError("logical scope is missing from the parent stage input")
    if (
        parent_scope.compiled_scope_sha256 != scope.scope_sha256
        or parent_scope.ordered_region_ids != scope.ordered_region_ids
    ):
        raise ValueError("parent logical-scope lineage drifted")

    region_by_id = {item.region_id: item for item in artifact.regions}
    try:
        selected_regions = tuple(
            region_by_id[region_id] for region_id in scope.ordered_region_ids
        )
    except KeyError as exc:
        raise ValueError("logical scope references an unknown compiled region") from exc
    expected_region_order = tuple(
        sorted(selected_regions, key=_region_source_order_key)
    )

    row_domain_reasons: set[D2RegionScopeEligibilityReason] = set()
    if any(
        region.disposition is not D2TableRegionDispositionKind.LOGICAL_SCOPE_PART
        or region.logical_scope_id != logical_scope_id
        for region in selected_regions
    ):
        row_domain_reasons.add(
            D2RegionScopeEligibilityReason.SCOPE_REGION_NOT_LOGICAL_SCOPE_PART
        )
    if selected_regions != expected_region_order:
        row_domain_reasons.add(
            D2RegionScopeEligibilityReason.SCOPE_REGION_ORDER_INVALID
        )
    if any(
        region.row_materializer_eligibility
        is not D2TableRegionRowMaterializerEligibility.SHAPE_ELIGIBLE_FULL_ROWS
        for region in selected_regions
    ):
        row_domain_reasons.add(
            D2RegionScopeEligibilityReason.SCOPE_REGION_NOT_FULL_ROW_ELIGIBLE
        )

    selected_rows_by_table: dict[str, list[int]] = {}
    for region in selected_regions:
        selected_rows_by_table.setdefault(region.physical_table_id, []).extend(
            region.covered_row_indices
        )
    selected_row_overlap_coordinates = tuple(
        sorted(
            (table_id, row_index)
            for table_id, rows in selected_rows_by_table.items()
            for row_index, count in Counter(rows).items()
            if count > 1
        )
    )
    if selected_row_overlap_coordinates:
        row_domain_reasons.add(
            D2RegionScopeEligibilityReason.SCOPE_SELECTED_ROW_OVERLAP
        )

    physical_row_domains = tuple(
        _build_physical_row_domain(
            table_catalog=table_catalog,
            table_id=table_id,
            selected_regions=tuple(
                region
                for region in selected_regions
                if region.physical_table_id == table_id
            ),
        )
        for table_id in _ordered_unique(
            region.physical_table_id for region in selected_regions
        )
    )
    if any(not item.cell_exact_cover for item in physical_row_domains):
        row_domain_reasons.add(
            D2RegionScopeEligibilityReason.SCOPE_REGION_CELL_EXACT_COVER_FAILED
        )

    selected_table_ids = {item.physical_table_id for item in physical_row_domains}
    selected_region_ids = set(scope.ordered_region_ids)
    excluded_row_domains = tuple(
        _build_excluded_row_domain(region)
        for region in artifact.regions
        if region.physical_table_id in selected_table_ids
        and region.region_id not in selected_region_ids
    )
    selected_coordinates = {
        (binding.table_id, binding.row_index)
        for domain in physical_row_domains
        for binding in domain.row_bindings
    }
    excluded_coordinates = {
        (item.physical_table_id, row_index)
        for item in excluded_row_domains
        for row_index in item.covered_row_indices
    }
    excluded_row_overlap_coordinates = tuple(
        sorted(selected_coordinates.intersection(excluded_coordinates))
    )
    if excluded_row_overlap_coordinates:
        row_domain_reasons.add(
            D2RegionScopeEligibilityReason.SCOPE_ROW_OVERLAPS_EXCLUDED
        )

    row_domain_ready = not row_domain_reasons
    if row_domain_ready:
        row_domain_reasons.add(
            D2RegionScopeEligibilityReason.SCOPE_FULL_ROW_EXACT_COVER
        )
    row_domain_status = (
        D2RegionScopeRowDomainStatus.ROW_DOMAIN_READY
        if row_domain_ready
        else D2RegionScopeRowDomainStatus.ROW_DOMAIN_NOT_READY
    )

    ordered_rows = tuple(
        binding for domain in physical_row_domains for binding in domain.row_bindings
    )
    resolved_row_roles = _resolve_row_roles(
        ordered_rows=ordered_rows,
        explicit_row_roles=explicit_row_roles,
    )
    base_fact_binding = _bind_base_fact(
        source=source,
        source_canonical_sha256=source_canonical_sha256,
        table_catalog=table_catalog,
        physical_row_domains=physical_row_domains,
        base_projection=base_projection,
        base_table_fact=base_table_fact,
    )

    materialization_reasons: set[D2RegionScopeEligibilityReason] = set()
    if not row_domain_ready:
        materialization_reasons.add(D2RegionScopeEligibilityReason.ROW_DOMAIN_NOT_READY)
    explicit_count = sum(
        item.role_source is D2RegionScopeRowRoleSource.EXPLICIT_INPUT
        for item in resolved_row_roles
    )
    if explicit_count == 0:
        materialization_reasons.add(
            D2RegionScopeEligibilityReason.ROW_ROLE_CONTRACT_MISSING
        )
    elif explicit_count != len(resolved_row_roles):
        materialization_reasons.add(
            D2RegionScopeEligibilityReason.ROW_ROLE_CONTRACT_INCOMPLETE
        )
    roles = {item.role for item in resolved_row_roles}
    if D2RegionScopeRowRole.HEADER not in roles:
        materialization_reasons.add(D2RegionScopeEligibilityReason.HEADER_ROLE_MISSING)
    if D2RegionScopeRowRole.BODY not in roles:
        materialization_reasons.add(D2RegionScopeEligibilityReason.BODY_ROLE_MISSING)
    if base_fact_binding.relation is D2RegionScopeBaseFactRelation.NOT_BOUND:
        materialization_reasons.add(D2RegionScopeEligibilityReason.BASE_FACT_NOT_BOUND)
    elif (
        base_fact_binding.relation is D2RegionScopeBaseFactRelation.WHOLE_TABLE_SUPERSET
    ):
        materialization_reasons.add(
            D2RegionScopeEligibilityReason.BASE_FACT_NOT_SCOPE_EXACT
        )
    materialization_ready = not materialization_reasons
    if materialization_ready:
        materialization_reasons.add(
            D2RegionScopeEligibilityReason.SCOPE_LOCAL_MATERIALIZER_INPUTS_VERIFIED
        )
    materialization_status = (
        D2RegionScopeMaterializationStatus.MATERIALIZATION_READY
        if materialization_ready
        else D2RegionScopeMaterializationStatus.MATERIALIZATION_NOT_READY
    )

    values = {
        "schema_version": (
            D2_REGION_SCOPE_MATERIALIZER_ELIGIBILITY_RECEIPT_SCHEMA_VERSION
        ),
        "content_free": True,
        "source_ir_id": source.source_ir_id,
        "source_sha256": source.source_sha256,
        "source_ir_canonical_sha256": source_canonical_sha256,
        "kit_bundle_sha256": artifact.kit_bundle_sha256,
        "d1_table_map_artifact_id": artifact.d1_table_map_artifact_id,
        "d1_table_map_artifact_sha256": artifact.d1_table_map_artifact_sha256,
        "region_artifact_id": artifact.artifact_id,
        "region_artifact_sha256": artifact.artifact_sha256,
        "parent_input_id": stage_input.stage_input_id,
        "parent_input_sha256": stage_input.stage_input_sha256,
        "logical_scope_id": logical_scope_id,
        "compiled_scope_sha256": scope.scope_sha256,
        "parent_scope_sha256": parent_scope.parent_scope_sha256,
        "ordered_region_ids": scope.ordered_region_ids,
        "ordered_region_sha256s": tuple(
            item.region_sha256 for item in selected_regions
        ),
        "physical_row_domains": physical_row_domains,
        "excluded_row_domains": excluded_row_domains,
        "selected_row_overlap_coordinates": selected_row_overlap_coordinates,
        "excluded_row_overlap_coordinates": excluded_row_overlap_coordinates,
        "resolved_row_roles": resolved_row_roles,
        "base_fact_binding": base_fact_binding,
        "row_domain_status": row_domain_status,
        "row_domain_reason_codes": tuple(
            sorted(row_domain_reasons, key=lambda item: item.value)
        ),
        "materialization_status": materialization_status,
        "materialization_reason_codes": tuple(
            sorted(materialization_reasons, key=lambda item: item.value)
        ),
    }
    return D2RegionScopeMaterializerEligibilityReceiptV1(
        **values,
        receipt_sha256=(
            stable_d2_region_scope_materializer_eligibility_receipt_sha256(
                **_json_values(values)
            )
        ),
    )


def _validate_input_lineage(
    *,
    source: SourceIRSnapshot,
    source_canonical_sha256: str,
    artifact: D2TableRegionArtifactV1,
    stage_input: D2TableRegionParentInputV1,
) -> None:
    if (
        artifact.source_ir_id != source.source_ir_id
        or artifact.source_sha256 != source.source_sha256
        or artifact.source_ir_canonical_sha256 != source_canonical_sha256
    ):
        raise ValueError("table-region artifact SourceIR identity drifted")
    if (
        stage_input.source_ir_id != source.source_ir_id
        or stage_input.source_sha256 != source.source_sha256
        or stage_input.source_ir_canonical_sha256 != source_canonical_sha256
    ):
        raise ValueError("table-region parent input SourceIR identity drifted")
    if (
        stage_input.kit_bundle_sha256 != artifact.kit_bundle_sha256
        or stage_input.d1_table_map_artifact_id != artifact.d1_table_map_artifact_id
        or stage_input.d1_table_map_artifact_sha256
        != artifact.d1_table_map_artifact_sha256
        or stage_input.region_artifact_id != artifact.artifact_id
        or stage_input.region_artifact_sha256 != artifact.artifact_sha256
        or stage_input.region_artifact_status != artifact.status
    ):
        raise ValueError("table-region parent input predecessor lineage drifted")
    if stage_input.ordered_region_ids != tuple(
        item.region_id for item in artifact.regions
    ) or stage_input.ordered_region_sha256s != tuple(
        item.region_sha256 for item in artifact.regions
    ):
        raise ValueError("table-region parent input region ledger drifted")
    if tuple(item.cell_coverage_sha256 for item in stage_input.physical_partitions) != (
        tuple(
            item.cell_coverage_sha256 for item in artifact.physical_table_cell_coverage
        )
    ):
        raise ValueError("table-region parent input physical partition drifted")


def _validate_artifact_tables(
    *,
    artifact: D2TableRegionArtifactV1,
    table_catalog: dict[str, tuple[SourceNode, TableIR, str, tuple[TableCellIR, ...]]],
) -> None:
    if tuple(table_catalog) != tuple(
        item.physical_table_id for item in artifact.physical_table_cell_coverage
    ):
        raise ValueError("table-region artifact physical-table scope drifted")
    for coverage in artifact.physical_table_cell_coverage:
        node, table, table_sha256, cells = table_catalog[coverage.physical_table_id]
        if (
            coverage.table_source_node_id != node.source_node_id
            or coverage.physical_table_source_order != node.source_order
            or coverage.table_ir_sha256 != table_sha256
            or coverage.canonical_cell_ids != tuple(item.cell_id for item in cells)
            or table.table_id != coverage.physical_table_id
        ):
            raise ValueError("table-region artifact physical TableIR revision drifted")


def _validate_parent_cell_ledgers(
    *,
    artifact: D2TableRegionArtifactV1,
    stage_input: D2TableRegionParentInputV1,
) -> None:
    parent_cells_by_region: dict[str, list[tuple[int, str]]] = {}
    for segment in stage_input.source_segments:
        for cell in segment.verbatim_cells:
            parent_cells_by_region.setdefault(segment.region_id, []).append(
                (cell.canonical_cell_ordinal, cell.cell_id)
            )
    for evidence in (
        *stage_input.nested_region_evidence,
        *stage_input.non_business_region_evidence,
        *stage_input.unresolved_scopes,
    ):
        for cell in evidence.verbatim_cells:
            parent_cells_by_region.setdefault(evidence.region_id, []).append(
                (cell.canonical_cell_ordinal, cell.cell_id)
            )
    for region in artifact.regions:
        observed = tuple(
            cell_id
            for _, cell_id in sorted(parent_cells_by_region.get(region.region_id, ()))
        )
        expected = tuple(item.cell_id for item in region.cell_refs)
        if observed != expected:
            raise ValueError("parent input cells drifted from compiled table regions")


def _build_physical_row_domain(
    *,
    table_catalog: dict[str, tuple[SourceNode, TableIR, str, tuple[TableCellIR, ...]]],
    table_id: str,
    selected_regions: tuple[D2CompiledTableRegionV1, ...],
) -> D2RegionScopePhysicalRowDomainV1:
    node, table, table_sha256, cells = table_catalog[table_id]
    selected_rows = tuple(
        sorted(
            {
                row_index
                for region in selected_regions
                for row_index in region.covered_row_indices
            }
        )
    )
    row_bindings = tuple(
        AgentTableRowBindingV1(
            table_id=table_id,
            row_index=row_index,
            cell_ids=tuple(
                cell.cell_id
                for cell in sorted(
                    (
                        cell
                        for cell in cells
                        if cell.row <= row_index < cell.row + cell.row_span
                    ),
                    key=lambda item: (item.column, item.row, item.cell_id),
                )
            ),
        )
        for row_index in selected_rows
    )
    selected_region_cell_ids = tuple(
        ref.cell_id for region in selected_regions for ref in region.cell_refs
    )
    exact_cover = Counter(
        cell_id for binding in row_bindings for cell_id in binding.cell_ids
    ) == Counter(selected_region_cell_ids)
    values = {
        "physical_table_id": table_id,
        "table_source_node_id": node.source_node_id,
        "physical_table_source_order": node.source_order,
        "table_ir_sha256": table_sha256,
        "selected_region_ids": tuple(item.region_id for item in selected_regions),
        "selected_region_sha256s": tuple(
            item.region_sha256 for item in selected_regions
        ),
        "selected_region_cell_ids": selected_region_cell_ids,
        "row_bindings": row_bindings,
        "cell_exact_cover": exact_cover,
    }
    return D2RegionScopePhysicalRowDomainV1(
        **values,
        row_domain_sha256=stable_d2_region_scope_physical_row_domain_sha256(**values),
    )


def _build_excluded_row_domain(
    region: D2CompiledTableRegionV1,
) -> D2RegionScopeExcludedRowDomainV1:
    values = {
        "region_id": region.region_id,
        "region_sha256": region.region_sha256,
        "disposition": region.disposition,
        "physical_table_id": region.physical_table_id,
        "physical_table_source_order": region.physical_table_source_order,
        "covered_row_indices": region.covered_row_indices,
        "cell_ids": tuple(item.cell_id for item in region.cell_refs),
    }
    return D2RegionScopeExcludedRowDomainV1(
        **values,
        exclusion_sha256=stable_d2_region_scope_exclusion_sha256(**values),
    )


def _resolve_row_roles(
    *,
    ordered_rows: tuple[AgentTableRowBindingV1, ...],
    explicit_row_roles: tuple[D2RegionScopeExplicitRowRoleV1, ...],
) -> tuple[D2RegionScopeResolvedRowRoleV1, ...]:
    explicit_by_coordinate: dict[tuple[str, int], D2RegionScopeExplicitRowRoleV1] = {}
    for raw_role in explicit_row_roles:
        role = D2RegionScopeExplicitRowRoleV1.model_validate(
            raw_role.model_dump(mode="json")
        )
        coordinate = (role.table_id, role.row_index)
        if coordinate in explicit_by_coordinate:
            raise ValueError("explicit row-role input repeats a scope row")
        explicit_by_coordinate[coordinate] = role
    row_by_coordinate = {(item.table_id, item.row_index): item for item in ordered_rows}
    unknown_coordinates = set(explicit_by_coordinate).difference(row_by_coordinate)
    if unknown_coordinates:
        raise ValueError("explicit row-role input references a row outside the scope")
    result: list[D2RegionScopeResolvedRowRoleV1] = []
    for row in ordered_rows:
        explicit = explicit_by_coordinate.get((row.table_id, row.row_index))
        if explicit is not None and explicit.cell_ids != row.cell_ids:
            raise ValueError("explicit row-role cell IDs differ from the Host row")
        role = explicit.role if explicit is not None else D2RegionScopeRowRole.UNKNOWN
        role_source = (
            D2RegionScopeRowRoleSource.EXPLICIT_INPUT
            if explicit is not None
            else D2RegionScopeRowRoleSource.HOST_UNKNOWN
        )
        explicit_sha = explicit.role_binding_sha256 if explicit is not None else None
        values = {
            "row_binding": row,
            "role": role,
            "role_source": role_source,
            "explicit_role_binding_sha256": explicit_sha,
        }
        result.append(
            D2RegionScopeResolvedRowRoleV1(
                **values,
                resolved_role_sha256=(
                    stable_d2_region_scope_resolved_row_role_sha256(**values)
                ),
            )
        )
    return tuple(result)


def _bind_base_fact(
    *,
    source: SourceIRSnapshot,
    source_canonical_sha256: str,
    table_catalog: dict[str, tuple[SourceNode, TableIR, str, tuple[TableCellIR, ...]]],
    physical_row_domains: tuple[D2RegionScopePhysicalRowDomainV1, ...],
    base_projection: TableRecordProjectionV1 | None,
    base_table_fact: TableFactBindingV1 | None,
) -> D2RegionScopeBaseFactBindingV1:
    projection = (
        TableRecordProjectionV1.model_validate(base_projection.model_dump(mode="json"))
        if base_projection is not None
        else None
    )
    direct_fact = (
        TableFactBindingV1.model_validate(base_table_fact.model_dump(mode="json"))
        if base_table_fact is not None
        else None
    )
    if projection is not None and direct_fact is not None:
        if projection.table_fact != direct_fact:
            raise ValueError("base projection and direct table fact disagree")
    fact = projection.table_fact if projection is not None else direct_fact
    if fact is None:
        values = {
            "relation": D2RegionScopeBaseFactRelation.NOT_BOUND,
            "fact_binding_sha256": None,
            "structure_snapshot_id": None,
            "logical_table_id": None,
            "projection_id": None,
            "projection_revision": None,
            "bound_table_ids": (),
            "bound_table_part_sha256s": (),
            "selected_cell_ids_sha256": None,
        }
        return D2RegionScopeBaseFactBindingV1(
            **values,
            binding_sha256=stable_d2_region_scope_base_fact_binding_sha256(**values),
        )
    if (
        fact.source_ir_id != source.source_ir_id
        or fact.source_sha256 != source.source_sha256
        or fact.source_ir_canonical_sha256 != source_canonical_sha256
    ):
        raise ValueError("base table fact SourceIR revision drifted")
    for part in fact.table_parts:
        current = table_catalog.get(part.table_id)
        if current is None:
            raise ValueError("base table fact references an unknown physical table")
        node, table, _, cells = current
        expected_refs = {
            StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id=node.source_node_id,
            ),
            *{
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell.cell_id,
                )
                for cell in cells
            },
        }
        if (
            part.source_order != node.source_order
            or part.row_count != table.row_count
            or part.column_count != table.column_count
            or part.cell_ids != tuple(cell.cell_id for cell in cells)
            or part.table_content_sha256 != canonical_model_sha256(table)
            or set(part.source_refs) != expected_refs
        ):
            raise ValueError("base table fact physical TableIR revision drifted")
    selected_cells_by_table = {
        item.physical_table_id: item.selected_region_cell_ids
        for item in physical_row_domains
    }
    fact_parts_by_id = {item.table_id: item for item in fact.table_parts}
    if not set(selected_cells_by_table).issubset(fact_parts_by_id):
        raise ValueError("base table fact does not contain every scope table")
    if any(
        not set(cell_ids).issubset(fact_parts_by_id[table_id].cell_ids)
        for table_id, cell_ids in selected_cells_by_table.items()
    ):
        raise ValueError("base table fact does not contain every scope cell")
    exact = fact.table_ids == tuple(selected_cells_by_table) and all(
        fact_parts_by_id[table_id].cell_ids == cell_ids
        for table_id, cell_ids in selected_cells_by_table.items()
    )
    relation = (
        D2RegionScopeBaseFactRelation.SCOPE_EXACT
        if exact
        else D2RegionScopeBaseFactRelation.WHOLE_TABLE_SUPERSET
    )
    values = {
        "relation": relation,
        "fact_binding_sha256": fact.fact_binding_sha256,
        "structure_snapshot_id": fact.structure_snapshot_id,
        "logical_table_id": fact.logical_table_id,
        "projection_id": projection.projection_id if projection is not None else None,
        "projection_revision": (
            projection.projection_revision if projection is not None else None
        ),
        "bound_table_ids": fact.table_ids,
        "bound_table_part_sha256s": tuple(
            _canonical_sha256(item.model_dump(mode="json")) for item in fact.table_parts
        ),
        "selected_cell_ids_sha256": _canonical_sha256(
            {
                table_id: list(cell_ids)
                for table_id, cell_ids in selected_cells_by_table.items()
            }
        ),
    }
    return D2RegionScopeBaseFactBindingV1(
        **values,
        binding_sha256=stable_d2_region_scope_base_fact_binding_sha256(**values),
    )


def _table_catalog(
    source: SourceIRSnapshot,
) -> dict[str, tuple[SourceNode, TableIR, str, tuple[TableCellIR, ...]]]:
    result: dict[str, tuple[SourceNode, TableIR, str, tuple[TableCellIR, ...]]] = {}
    for node in source.nodes:
        if node.kind is not SourceNodeKind.TABLE:
            continue
        table = node.table_payload
        if table is None:
            raise ValueError("SourceIR table node lost its TableIR payload")
        if table.table_id in result:
            raise ValueError("SourceIR contains an ambiguous physical table ID")
        cells = tuple(
            sorted(table.cells, key=lambda item: (item.row, item.column, item.cell_id))
        )
        result[table.table_id] = (
            node,
            table,
            _canonical_sha256(table.model_dump(mode="json")),
            cells,
        )
    return result


def _region_source_order_key(region: D2CompiledTableRegionV1) -> tuple[object, ...]:
    return (
        region.physical_table_source_order,
        region.cell_spans[0].start_cell_ordinal,
        tuple(
            (span.start_cell_ordinal, span.end_cell_ordinal_exclusive)
            for span in region.cell_spans
        ),
        region.region_id,
    )


def _ordered_unique(values: object) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:  # type: ignore[union-attr]
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def stable_d2_region_scope_explicit_row_role_sha256(**values: object) -> str:
    return _canonical_sha256(
        {
            "schema_version": "d2-region-scope-explicit-row-role/v1",
            **_json_values(values),
        }
    )


def stable_d2_region_scope_physical_row_domain_sha256(**values: object) -> str:
    return _canonical_sha256(
        {
            "schema_version": "d2-region-scope-physical-row-domain/v1",
            **_json_values(values),
        }
    )


def stable_d2_region_scope_exclusion_sha256(**values: object) -> str:
    return _canonical_sha256(
        {
            "schema_version": "d2-region-scope-excluded-row-domain/v1",
            **_json_values(values),
        }
    )


def stable_d2_region_scope_resolved_row_role_sha256(**values: object) -> str:
    return _canonical_sha256(
        {
            "schema_version": "d2-region-scope-resolved-row-role/v1",
            **_json_values(values),
        }
    )


def stable_d2_region_scope_base_fact_binding_sha256(**values: object) -> str:
    return _canonical_sha256(
        {
            "schema_version": "d2-region-scope-base-fact-binding/v1",
            **_json_values(values),
        }
    )


def stable_d2_region_scope_materializer_eligibility_receipt_sha256(
    **values: object,
) -> str:
    return _canonical_sha256(
        {
            "schema_version": (
                D2_REGION_SCOPE_MATERIALIZER_ELIGIBILITY_RECEIPT_SCHEMA_VERSION
            ),
            **_json_values(values),
        }
    )


def _json_values(values: dict[str, object]) -> dict[str, object]:
    return {key: _json_value(value) for key, value in values.items()}


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "D2_REGION_SCOPE_MATERIALIZER_ELIGIBILITY_RECEIPT_SCHEMA_VERSION",
    "D2RegionScopeBaseFactBindingV1",
    "D2RegionScopeBaseFactRelation",
    "D2RegionScopeEligibilityReason",
    "D2RegionScopeExcludedRowDomainV1",
    "D2RegionScopeExplicitRowRoleV1",
    "D2RegionScopeMaterializationStatus",
    "D2RegionScopeMaterializerEligibilityReceiptV1",
    "D2RegionScopePhysicalRowDomainV1",
    "D2RegionScopeResolvedRowRoleV1",
    "D2RegionScopeRowDomainStatus",
    "D2RegionScopeRowRole",
    "D2RegionScopeRowRoleSource",
    "build_d2_region_scope_explicit_row_role",
    "build_d2_region_scope_materializer_eligibility_receipt",
    "stable_d2_region_scope_base_fact_binding_sha256",
    "stable_d2_region_scope_exclusion_sha256",
    "stable_d2_region_scope_explicit_row_role_sha256",
    "stable_d2_region_scope_materializer_eligibility_receipt_sha256",
    "stable_d2_region_scope_physical_row_domain_sha256",
    "stable_d2_region_scope_resolved_row_role_sha256",
]
