from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import unittest
from datetime import datetime, timezone

from app.application.agent_workflow.d2_region_scope_materializer_eligibility import (
    D2RegionScopeBaseFactRelation,
    D2RegionScopeEligibilityReason,
    D2RegionScopeMaterializationStatus,
    D2RegionScopeMaterializerEligibilityReceiptV1,
    D2RegionScopeRowDomainStatus,
    D2RegionScopeRowRole,
    build_d2_region_scope_explicit_row_role,
    build_d2_region_scope_materializer_eligibility_receipt,
)
from app.application.agent_workflow.d2_table_region_parent_input import (
    build_d2_table_region_parent_input,
)
from app.application.agent_workflow.d2_table_regions import (
    D2LogicalObjectPartTableRegionCandidateV1,
    D2LogicalScopePartTableRegionCandidateV1,
    D2NestedValueTableRegionCandidateV1,
    D2NonBusinessFalseTableRegionCandidateV1,
    D2PhysicalTableCellOrdinalSpanV1,
    D2TableLogicalScopeCandidateV1,
    D2TableRegionCandidateArtifactV1,
    D2TableRegionDispositionKind,
    D2UnresolvedTableRegionCandidateV1,
    compile_d2_table_regions,
    d2_table_region_source_ir_canonical_sha256,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
    stable_cell_id,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
    stable_table_id,
)
from app.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.domain.extraction.structure_revision import canonical_model_sha256
from app.domain.extraction.table_records import (
    TableFactPartV1,
    TableRecordProjectionMode,
    build_table_fact_binding,
    build_table_record_projection,
    normalize_table_record_source_refs,
)


SOURCE_SHA = "a" * 64
KIT_SHA = "b" * 64
D1_TABLE_MAP_ID = "d1-table-map-" + "1" * 32
D1_TABLE_MAP_SHA = "c" * 64


class D2RegionScopeMaterializerEligibilityTests(unittest.TestCase):
    def test_same_physical_table_gets_two_disjoint_scope_local_row_domains(
        self,
    ) -> None:
        source = _source_fixture((1,), rows=4, columns=2)
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(
                _scope_region("scope-a-region", table_id, 0, 4, "scope-a"),
                _scope_region("scope-b-region", table_id, 4, 8, "scope-b"),
            ),
            scopes=(
                _scope("scope-a", ("scope-a-region",)),
                _scope("scope-b", ("scope-b-region",)),
            ),
        )
        parent_input = _parent_input(source, artifact)

        first = _receipt(source, artifact, parent_input, scope_index=0)
        second = _receipt(source, artifact, parent_input, scope_index=1)

        self.assertEqual(
            first.row_domain_status, D2RegionScopeRowDomainStatus.ROW_DOMAIN_READY
        )
        self.assertEqual(
            second.row_domain_status, D2RegionScopeRowDomainStatus.ROW_DOMAIN_READY
        )
        self.assertEqual(
            tuple(
                item.row_index for item in first.physical_row_domains[0].row_bindings
            ),
            (0, 1),
        )
        self.assertEqual(
            tuple(
                item.row_index for item in second.physical_row_domains[0].row_bindings
            ),
            (2, 3),
        )
        self.assertFalse(
            {
                (item.table_id, item.row_index)
                for item in first.physical_row_domains[0].row_bindings
            }.intersection(
                {
                    (item.table_id, item.row_index)
                    for item in second.physical_row_domains[0].row_bindings
                }
            )
        )
        self.assertEqual(
            first.materialization_status,
            D2RegionScopeMaterializationStatus.MATERIALIZATION_NOT_READY,
        )
        self.assertIn(
            D2RegionScopeEligibilityReason.ROW_ROLE_CONTRACT_MISSING,
            first.materialization_reason_codes,
        )
        self.assertEqual(first, _receipt(source, artifact, parent_input, scope_index=0))
        payload = json.dumps(first.model_dump(mode="json"), ensure_ascii=False)
        self.assertNotIn("raw_text", payload)
        self.assertNotIn("source_text", payload)

    def test_one_scope_keeps_row_domains_ordered_across_physical_tables(self) -> None:
        source = _source_fixture((1, 2), rows=2, columns=2)
        first_table, second_table = _table_ids(source)
        artifact = _compile(
            source,
            regions=(
                _scope_region("page-one", first_table, 0, 4, "scope-a"),
                _scope_region("page-two", second_table, 0, 4, "scope-a"),
            ),
            scopes=(_scope("scope-a", ("page-one", "page-two")),),
        )

        receipt = _receipt(source, artifact, _parent_input(source, artifact))

        self.assertEqual(
            receipt.row_domain_status, D2RegionScopeRowDomainStatus.ROW_DOMAIN_READY
        )
        self.assertEqual(
            tuple(item.physical_table_id for item in receipt.physical_row_domains),
            (first_table, second_table),
        )
        self.assertEqual(
            tuple(
                (binding.table_id, binding.row_index)
                for domain in receipt.physical_row_domains
                for binding in domain.row_bindings
            ),
            (
                (first_table, 0),
                (first_table, 1),
                (second_table, 0),
                (second_table, 1),
            ),
        )

    def test_partial_row_is_not_row_domain_ready_and_exposes_overlap(self) -> None:
        source = _source_fixture((1,), rows=2, columns=2)
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(
                _scope_region("partial", table_id, 0, 1, "scope-a"),
                _false_region("remainder", table_id, 1, 4),
            ),
            scopes=(_scope("scope-a", ("partial",)),),
        )

        receipt = _receipt(source, artifact, _parent_input(source, artifact))

        self.assertEqual(
            receipt.row_domain_status,
            D2RegionScopeRowDomainStatus.ROW_DOMAIN_NOT_READY,
        )
        self.assertIn(
            D2RegionScopeEligibilityReason.SCOPE_REGION_NOT_FULL_ROW_ELIGIBLE,
            receipt.row_domain_reason_codes,
        )
        self.assertIn(
            D2RegionScopeEligibilityReason.SCOPE_REGION_CELL_EXACT_COVER_FAILED,
            receipt.row_domain_reason_codes,
        )
        self.assertEqual(receipt.excluded_row_overlap_coordinates, ((table_id, 0),))

    def test_nested_false_unresolved_and_prose_rows_are_explicitly_excluded(
        self,
    ) -> None:
        source = _source_fixture((1,), rows=5, columns=2)
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(
                _scope_region("owner", table_id, 0, 2, "scope-a"),
                D2NestedValueTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                    region_key="nested",
                    physical_table_id=table_id,
                    cell_spans=(_span(2, 4),),
                    nested_owner_region_key="owner",
                    reason_codes=("nested_value",),
                ),
                _false_region("false-row", table_id, 4, 6),
                D2UnresolvedTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.UNRESOLVED,
                    region_key="unresolved-row",
                    physical_table_id=table_id,
                    cell_spans=(_span(6, 8),),
                    reason_codes=("semantic_boundary_unresolved",),
                ),
                D2LogicalObjectPartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                    region_key="prose-row",
                    physical_table_id=table_id,
                    cell_spans=(_span(8, 10),),
                    logical_object_key="prose-object",
                    reason_codes=("ordinary_content_inside_table_frame",),
                ),
            ),
            scopes=(_scope("scope-a", ("owner",)),),
        )

        receipt = _receipt(source, artifact, _parent_input(source, artifact))

        self.assertEqual(
            receipt.row_domain_status, D2RegionScopeRowDomainStatus.ROW_DOMAIN_READY
        )
        self.assertEqual(receipt.excluded_row_overlap_coordinates, ())
        self.assertEqual(
            {item.disposition for item in receipt.excluded_row_domains},
            {
                D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION,
                D2TableRegionDispositionKind.UNRESOLVED,
                D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
            },
        )
        self.assertEqual(
            tuple(
                row
                for item in receipt.excluded_row_domains
                for row in item.covered_row_indices
            ),
            (1, 2, 3, 4),
        )

    def test_exact_base_fact_and_explicit_header_body_make_materialization_ready(
        self,
    ) -> None:
        source = _source_fixture((1,), rows=2, columns=2)
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(_scope_region("all", table_id, 0, 4, "scope-a"),),
            scopes=(_scope("scope-a", ("all",)),),
        )
        parent_input = _parent_input(source, artifact)
        first_pass = _receipt(source, artifact, parent_input)
        rows = first_pass.physical_row_domains[0].row_bindings
        explicit_roles = (
            _role(rows[0], D2RegionScopeRowRole.HEADER),
            _role(rows[1], D2RegionScopeRowRole.BODY),
        )
        fact = _whole_table_fact(source)
        projection = build_table_record_projection(
            table_fact=fact,
            mode=TableRecordProjectionMode.WHOLE_TABLE,
            projection_revision=7,
        )

        receipt = build_d2_region_scope_materializer_eligibility_receipt(
            source,
            artifact,
            parent_input,
            logical_scope_id=artifact.logical_scopes[0].logical_scope_id,
            explicit_row_roles=explicit_roles,
            base_projection=projection,
        )

        self.assertEqual(
            receipt.base_fact_binding.relation,
            D2RegionScopeBaseFactRelation.SCOPE_EXACT,
        )
        self.assertEqual(
            receipt.materialization_status,
            D2RegionScopeMaterializationStatus.MATERIALIZATION_READY,
        )
        self.assertEqual(
            receipt.materialization_reason_codes,
            (D2RegionScopeEligibilityReason.SCOPE_LOCAL_MATERIALIZER_INPUTS_VERIFIED,),
        )

    def test_whole_table_fact_cannot_masquerade_as_subscope(self) -> None:
        source = _source_fixture((1,), rows=4, columns=2)
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(
                _scope_region("scope-a-region", table_id, 0, 4, "scope-a"),
                _scope_region("scope-b-region", table_id, 4, 8, "scope-b"),
            ),
            scopes=(
                _scope("scope-a", ("scope-a-region",)),
                _scope("scope-b", ("scope-b-region",)),
            ),
        )
        parent_input = _parent_input(source, artifact)
        first_pass = _receipt(source, artifact, parent_input)
        rows = first_pass.physical_row_domains[0].row_bindings

        receipt = build_d2_region_scope_materializer_eligibility_receipt(
            source,
            artifact,
            parent_input,
            logical_scope_id=artifact.logical_scopes[0].logical_scope_id,
            explicit_row_roles=(
                _role(rows[0], D2RegionScopeRowRole.HEADER),
                _role(rows[1], D2RegionScopeRowRole.BODY),
            ),
            base_table_fact=_whole_table_fact(source),
        )

        self.assertEqual(
            receipt.row_domain_status, D2RegionScopeRowDomainStatus.ROW_DOMAIN_READY
        )
        self.assertEqual(
            receipt.base_fact_binding.relation,
            D2RegionScopeBaseFactRelation.WHOLE_TABLE_SUPERSET,
        )
        self.assertEqual(
            receipt.materialization_status,
            D2RegionScopeMaterializationStatus.MATERIALIZATION_NOT_READY,
        )
        self.assertIn(
            D2RegionScopeEligibilityReason.BASE_FACT_NOT_SCOPE_EXACT,
            receipt.materialization_reason_codes,
        )

    def test_tamper_and_base_fact_revision_drift_are_rejected(self) -> None:
        source = _source_fixture((1,), rows=2, columns=2)
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(_scope_region("all", table_id, 0, 4, "scope-a"),),
            scopes=(_scope("scope-a", ("all",)),),
        )
        parent_input = _parent_input(source, artifact)
        tampered_region = artifact.regions[0].model_copy(
            update={"covered_row_indices": (0,)}
        )
        tampered_artifact = artifact.model_copy(update={"regions": (tampered_region,)})

        with self.assertRaisesRegex(ValueError, "row alignment|region SHA"):
            _receipt(source, tampered_artifact, parent_input)

        drifted_fact = _whole_table_fact(
            source,
            source_ir_canonical_sha256="d" * 64,
        )
        with self.assertRaisesRegex(ValueError, "SourceIR revision drifted"):
            build_d2_region_scope_materializer_eligibility_receipt(
                source,
                artifact,
                parent_input,
                logical_scope_id=artifact.logical_scopes[0].logical_scope_id,
                base_table_fact=drifted_fact,
            )

        table_drifted_fact = _whole_table_fact(
            source,
            table_content_sha256_override="f" * 64,
        )
        with self.assertRaisesRegex(ValueError, "physical TableIR revision drifted"):
            build_d2_region_scope_materializer_eligibility_receipt(
                source,
                artifact,
                parent_input,
                logical_scope_id=artifact.logical_scopes[0].logical_scope_id,
                base_table_fact=table_drifted_fact,
            )

    def test_receipt_hash_rejects_tampering(self) -> None:
        source = _source_fixture((1,), rows=2, columns=2)
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(_scope_region("all", table_id, 0, 4, "scope-a"),),
            scopes=(_scope("scope-a", ("all",)),),
        )
        receipt = _receipt(source, artifact, _parent_input(source, artifact))
        payload = receipt.model_dump(mode="json")
        payload["logical_scope_id"] = "d2-table-logical-scope-" + "f" * 32

        with self.assertRaisesRegex(ValueError, "receipt SHA"):
            D2RegionScopeMaterializerEligibilityReceiptV1.model_validate(payload)


def _receipt(source, artifact, parent_input, *, scope_index: int = 0):
    return build_d2_region_scope_materializer_eligibility_receipt(
        source,
        artifact,
        parent_input,
        logical_scope_id=artifact.logical_scopes[scope_index].logical_scope_id,
    )


def _role(row, role: D2RegionScopeRowRole):
    return build_d2_region_scope_explicit_row_role(
        table_id=row.table_id,
        row_index=row.row_index,
        cell_ids=row.cell_ids,
        role=role,
        evidence_ref=f"explicit-row-role:{row.table_id}:{row.row_index}",
        evidence_sha256="e" * 64,
    )


def _parent_input(source: SourceIRSnapshot, artifact):
    return build_d2_table_region_parent_input(
        source,
        artifact,
        expected_kit_bundle_sha256=KIT_SHA,
        expected_d1_table_map_artifact_id=D1_TABLE_MAP_ID,
        expected_d1_table_map_artifact_sha256=D1_TABLE_MAP_SHA,
        expected_region_artifact_id=artifact.artifact_id,
        expected_region_artifact_sha256=artifact.artifact_sha256,
    ).stage_input


def _compile(
    source: SourceIRSnapshot,
    *,
    regions: tuple[object, ...],
    scopes: tuple[D2TableLogicalScopeCandidateV1, ...] = (),
):
    candidate = D2TableRegionCandidateArtifactV1(
        source_ir_id=source.source_ir_id,
        source_sha256=source.source_sha256,
        source_ir_canonical_sha256=d2_table_region_source_ir_canonical_sha256(source),
        kit_bundle_sha256=KIT_SHA,
        d1_table_map_artifact_id=D1_TABLE_MAP_ID,
        d1_table_map_artifact_sha256=D1_TABLE_MAP_SHA,
        regions=regions,
        logical_scopes=scopes,
    )
    return compile_d2_table_regions(
        source,
        candidate,
        expected_kit_bundle_sha256=KIT_SHA,
        expected_d1_table_map_artifact_id=D1_TABLE_MAP_ID,
        expected_d1_table_map_artifact_sha256=D1_TABLE_MAP_SHA,
    )


def _scope_region(key: str, table_id: str, start: int, end: int, scope_key: str):
    return D2LogicalScopePartTableRegionCandidateV1(
        disposition=D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
        region_key=key,
        physical_table_id=table_id,
        cell_spans=(_span(start, end),),
        logical_scope_key=scope_key,
        reason_codes=("logical_grid",),
    )


def _false_region(key: str, table_id: str, start: int, end: int):
    return D2NonBusinessFalseTableRegionCandidateV1(
        disposition=D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION,
        region_key=key,
        physical_table_id=table_id,
        cell_spans=(_span(start, end),),
        reason_codes=("layout_only",),
    )


def _scope(key: str, region_keys: tuple[str, ...]):
    return D2TableLogicalScopeCandidateV1(
        logical_scope_key=key,
        ordered_region_keys=region_keys,
        reason_codes=("same_logical_table",),
    )


def _span(start: int, end: int):
    return D2PhysicalTableCellOrdinalSpanV1(
        start_cell_ordinal=start,
        end_cell_ordinal_exclusive=end,
    )


def _table_ids(source: SourceIRSnapshot) -> tuple[str, ...]:
    return tuple(
        node.table_payload.table_id
        for node in source.nodes
        if node.table_payload is not None
    )


def _whole_table_fact(
    source: SourceIRSnapshot,
    *,
    source_ir_canonical_sha256: str | None = None,
    table_content_sha256_override: str | None = None,
):
    parts: list[TableFactPartV1] = []
    whole_refs: list[StructureSourceRef] = []
    for node in source.nodes:
        table = node.table_payload
        if table is None:
            continue
        cells = tuple(
            sorted(table.cells, key=lambda item: (item.row, item.column, item.cell_id))
        )
        refs = normalize_table_record_source_refs(
            (
                StructureSourceRef(
                    kind=StructureSourceRefKind.SOURCE_NODE,
                    source_id=node.source_node_id,
                ),
                *tuple(
                    StructureSourceRef(
                        kind=StructureSourceRefKind.TABLE_CELL,
                        source_id=cell.cell_id,
                    )
                    for cell in cells
                ),
            )
        )
        whole_refs.extend(refs)
        parts.append(
            TableFactPartV1(
                table_id=table.table_id,
                source_order=node.source_order,
                row_count=table.row_count,
                column_count=table.column_count,
                cell_ids=tuple(cell.cell_id for cell in cells),
                table_content_sha256=(
                    table_content_sha256_override or canonical_model_sha256(table)
                ),
                source_refs=refs,
            )
        )
    return build_table_fact_binding(
        source_ir_id=source.source_ir_id,
        source_sha256=source.source_sha256,
        source_ir_canonical_sha256=(
            source_ir_canonical_sha256
            or d2_table_region_source_ir_canonical_sha256(source)
        ),
        structure_snapshot_id="structure-snapshot-1",
        logical_table_id="whole-table-fact-1",
        owner_structure_node_id="structure-table-owner-1",
        table_parts=tuple(parts),
        whole_table_source_refs=tuple(whole_refs),
    )


def _source_fixture(
    pages: tuple[int, ...],
    *,
    rows: int,
    columns: int,
) -> SourceIRSnapshot:
    adapter_id = "region-materializer-eligibility-test"
    adapter_version = "1.0.0"
    nodes: list[SourceNode] = []
    for table_index, page_number in enumerate(pages):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=table_index,
            bbox=(10.0, 10.0, 300.0, 300.0),
        )
        table_id = stable_table_id(
            source_sha256=SOURCE_SHA,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            native_locator=locator,
        )
        cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, row, column),
                row=row,
                column=column,
                raw_text=f"t{table_index}-r{row}-c{column}",
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=page_number,
                    table_index=table_index,
                    row_index=row,
                    column_index=column,
                    bbox=(
                        float(column * 100),
                        float(row * 40),
                        float((column + 1) * 100),
                        float((row + 1) * 40),
                    ),
                ),
            )
            for row in range(rows)
            for column in range(columns)
        )
        table = TableIR(
            table_id=table_id,
            row_count=rows,
            column_count=columns,
            cells=cells,
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_SHA,
                    adapter_id=adapter_id,
                    adapter_version=adapter_version,
                    kind=SourceNodeKind.TABLE,
                    native_locator=locator,
                ),
                kind=SourceNodeKind.TABLE,
                source_order=table_index,
                raw_text="",
                native_locator=locator,
                table_payload=table,
            )
        )
    document_id = stable_document_id(SOURCE_SHA)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_SHA,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=SOURCE_SHA,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )


if __name__ == "__main__":
    unittest.main()
