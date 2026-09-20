from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

if __package__:
    from .private_eval import require_private_eval
else:
    from private_eval import require_private_eval

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from app.application.agent_workflow.d2_table_regions import (
    D2LogicalObjectPartTableRegionCandidateV1,
    D2LogicalScopePartTableRegionCandidateV1,
    D2NestedValueTableRegionCandidateV1,
    D2NonBusinessFalseTableRegionCandidateV1,
    D2PhysicalTableCellOrdinalSpanV1,
    D2TableLogicalScopeCandidateV1,
    D2TableRegionCandidateArtifactV1,
    D2TableRegionDispositionKind,
    compile_d2_table_regions,
    d2_table_region_source_ir_canonical_sha256,
)
from app.application.agent_workflow.pds_region_boundary_oracle import (
    PDS_PRIVATE_REGION_BOUNDARY_ORACLE_AUTHORITY,
    PDS_PRIVATE_REGION_BOUNDARY_ORACLE_PATH,
    PDS_PRIVATE_REGION_BOUNDARY_SOURCE_SHA256,
    evaluate_pds_private_region_boundary_oracle,
    load_pds_private_region_boundary_oracle,
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


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from canonical_d2_app_server_canary import _d2_region_summary


KIT_SHA = "b" * 64
D1_TABLE_MAP_ID = "d1-table-map-" + "1" * 32
D1_TABLE_MAP_SHA = "c" * 64


class PdsPrivateRegionBoundaryOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_private_eval("private_oracles/pds_region_boundary_oracle.v1.json")
        cls.source = _source_fixture()

    def test_expected_private_topology_passes_both_axes(self) -> None:
        artifact = _compile_expected(self.source)

        result = evaluate_pds_private_region_boundary_oracle(artifact)

        assert result is not None
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["coverage_verdict"], "PASS")
        self.assertEqual(result["boundary_verdict"], "PASS")
        self.assertEqual(result["tail_grouping_verdict"], "PASS")
        self.assertEqual(result["expected_region_count"], 40)
        self.assertEqual(result["observed_region_count"], 40)
        self.assertEqual(result["reason_codes"], ())
        self.assertEqual(
            result["authority"], PDS_PRIVATE_REGION_BOUNDARY_ORACLE_AUTHORITY
        )
        serialized = json.dumps(result, sort_keys=True).lower()
        self.assertNotIn("raw_text", serialized)
        self.assertNotIn("source_text", serialized)
        self.assertNotIn("/users/", serialized)

    def test_manifest_encodes_the_reviewed_front_spans_and_tail_groups(self) -> None:
        manifest, _ = load_pds_private_region_boundary_oracle()
        expected_front = {
            1: (
                (0, 23, "LOGICAL_SCOPE_PART", "L1"),
                (23, 44, "LOGICAL_SCOPE_PART", "L2"),
                (44, 48, "LOGICAL_OBJECT_PART", None),
            ),
            2: (
                (0, 1, "LOGICAL_OBJECT_PART", None),
                (1, 3, "LOGICAL_SCOPE_PART", "L3"),
                (3, 59, "LOGICAL_SCOPE_PART", "L4"),
            ),
            3: (
                (0, 146, "LOGICAL_SCOPE_PART", "L4"),
                (146, 167, "LOGICAL_SCOPE_PART", "L5"),
            ),
            4: (
                (0, 54, "LOGICAL_SCOPE_PART", "L5"),
                (54, 108, "LOGICAL_SCOPE_PART", "L6"),
            ),
            5: (
                (0, 30, "LOGICAL_SCOPE_PART", "L6"),
                (30, 46, "LOGICAL_SCOPE_PART", "L7"),
                (46, 56, "LOGICAL_SCOPE_PART", "L8"),
            ),
            6: (
                (0, 6, "LOGICAL_SCOPE_PART", "L8"),
                (6, 1062, "LOGICAL_OBJECT_PART", None),
            ),
            7: (
                (0, 98, "LOGICAL_OBJECT_PART", None),
                (98, 161, "LOGICAL_SCOPE_PART", "L9"),
            ),
            8: ((0, 170, "LOGICAL_OBJECT_PART", None),),
        }
        observed_front = {
            table.table_ordinal: tuple(
                (
                    region.cell_span[0],
                    region.cell_span[1],
                    region.disposition.value,
                    region.scope_label,
                )
                for region in table.regions
            )
            for table in manifest.tables[:8]
        }
        scopes: dict[str, list[int]] = {}
        nested: list[tuple[int, int]] = []
        for table in manifest.tables[8:]:
            self.assertEqual(table.regions[0].cell_span, "WHOLE_TABLE")
            region = table.regions[0]
            if region.scope_label is not None:
                scopes.setdefault(region.scope_label, []).append(table.table_ordinal)
            if region.nested_owner is not None:
                nested.append((table.table_ordinal, region.nested_owner.table_ordinal))

        self.assertEqual(observed_front, expected_front)
        self.assertEqual(scopes["L10"], [9, 11, 12, 13, 14, 15])
        self.assertEqual(scopes["L11"], [16, 18, 19, 20, 21, 22])
        self.assertEqual(scopes["L12"], [23])
        self.assertEqual(scopes["L13"], [24])
        self.assertEqual(scopes["L14"], [25])
        self.assertEqual(scopes["L15"], [26, 27, 28, 29, 30])
        self.assertEqual(nested, [(10, 9), (17, 16)])

    def test_one_region_per_front_table_fails_boundary_but_keeps_coverage_and_tail(
        self,
    ) -> None:
        artifact = _compile_current_one_region_front(self.source)

        result = evaluate_pds_private_region_boundary_oracle(artifact)

        assert result is not None
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["coverage_verdict"], "PASS")
        self.assertEqual(result["boundary_verdict"], "FAIL")
        self.assertEqual(result["tail_grouping_verdict"], "PASS")
        self.assertEqual(result["front_region_boundary_gap_table_count"], 7)
        self.assertEqual(
            result["front_boundary_gap_table_ordinals"],
            tuple(range(1, 8)),
        )
        self.assertEqual(result["tail_region_boundary_gap_table_count"], 0)
        self.assertIn("FRONT_REGION_BOUNDARY_GAP", result["reason_codes"])

    def test_scope_tamper_fails_tail_grouping_without_faking_coverage_failure(
        self,
    ) -> None:
        artifact = _compile_expected(self.source, tamper_tail_scope=True)

        result = evaluate_pds_private_region_boundary_oracle(artifact)

        assert result is not None
        self.assertEqual(result["coverage_verdict"], "PASS")
        self.assertEqual(result["tail_region_boundary_gap_table_count"], 0)
        self.assertEqual(result["tail_grouping_verdict"], "FAIL")
        self.assertGreater(result["tail_scope_group_mismatch_count"], 0)
        self.assertIn("SCOPE_GROUPING_MISMATCH", result["reason_codes"])

    def test_oracle_is_source_gated_and_canary_summary_is_posthoc_only(self) -> None:
        artifact = _compile_expected(self.source)
        proposal = SimpleNamespace(
            source_sha256=PDS_PRIVATE_REGION_BOUNDARY_SOURCE_SHA256,
            status=SimpleNamespace(value="COMPLETE"),
            logical_objects=(),
            table_region_dispositions=(),
            physical_table_evidence=(),
            base_table_replacements=(),
            nested_region_ids=(),
            false_region_ids=(),
            unresolved_region_ids=(),
            unresolved_empty_table_ids=(),
            unresolved_supplemental_evidence_ids=(),
            unresolved_codes=(),
        )
        summary = _d2_region_summary(
            SimpleNamespace(
                proposal=proposal,
                host_validation_receipt=SimpleNamespace(receipt_sha256="d" * 64),
                stage_artifact=SimpleNamespace(
                    status=SimpleNamespace(value="VALIDATED"),
                    artifact_id="agent-artifact-" + "e" * 32,
                    payload_sha256="f" * 64,
                ),
                execution_receipt=SimpleNamespace(receipt_sha256="1" * 64),
                compilation=SimpleNamespace(table_region_artifact=artifact),
            )
        )
        other_source_artifact = _compile_expected(
            _source_fixture(source_sha256="9" * 64)
        )

        self.assertEqual(
            summary["private_region_boundary_oracle"]["verdict"],
            "PASS",
        )
        self.assertIsNone(
            evaluate_pds_private_region_boundary_oracle(other_source_artifact)
        )

    def test_manifest_is_private_text_free_and_locked_to_reference_identity(
        self,
    ) -> None:
        manifest, manifest_sha = load_pds_private_region_boundary_oracle()
        serialized = PDS_PRIVATE_REGION_BOUNDARY_ORACLE_PATH.read_text(
            encoding="utf-8"
        ).lower()

        self.assertEqual(
            manifest.authority, PDS_PRIVATE_REGION_BOUNDARY_ORACLE_AUTHORITY
        )
        self.assertEqual(len(manifest_sha), 64)
        self.assertNotIn("raw_text", serialized)
        self.assertNotIn("source_text", serialized)
        self.assertNotIn("/users/", serialized)


def _compile_expected(
    source: SourceIRSnapshot,
    *,
    tamper_tail_scope: bool = False,
):
    manifest, _ = load_pds_private_region_boundary_oracle()
    table_ids = _table_ids(source)
    region_keys: dict[tuple[int, int], str] = {}
    regions: list[object] = []
    scopes: dict[str, list[str]] = {}
    for table in manifest.tables:
        cell_count = len(source.nodes[table.table_ordinal - 1].table_payload.cells)
        for region_ordinal, expected in enumerate(table.regions, start=1):
            key = f"t{table.table_ordinal}-r{region_ordinal}"
            region_keys[(table.table_ordinal, region_ordinal)] = key
            start, end = (
                (0, cell_count)
                if expected.cell_span == "WHOLE_TABLE"
                else expected.cell_span
            )
            span = _span(start, end)
            if expected.disposition is D2TableRegionDispositionKind.LOGICAL_SCOPE_PART:
                label = expected.scope_label
                assert label is not None
                scope_key = label.lower()
                if tamper_tail_scope and table.table_ordinal == 11:
                    scope_key = "tampered-l10"
                scopes.setdefault(scope_key, []).append(key)
                regions.append(
                    D2LogicalScopePartTableRegionCandidateV1(
                        disposition=expected.disposition,
                        region_key=key,
                        physical_table_id=table_ids[table.table_ordinal - 1],
                        cell_spans=(span,),
                        logical_scope_key=scope_key,
                        reason_codes=("private_reference",),
                    )
                )
            elif (
                expected.disposition is D2TableRegionDispositionKind.LOGICAL_OBJECT_PART
            ):
                regions.append(
                    D2LogicalObjectPartTableRegionCandidateV1(
                        disposition=expected.disposition,
                        region_key=key,
                        physical_table_id=table_ids[table.table_ordinal - 1],
                        cell_spans=(span,),
                        logical_object_key=f"object-{key}",
                        reason_codes=("private_reference",),
                    )
                )
            elif (
                expected.disposition is D2TableRegionDispositionKind.NESTED_VALUE_REGION
            ):
                assert expected.nested_owner is not None
                regions.append(
                    D2NestedValueTableRegionCandidateV1(
                        disposition=expected.disposition,
                        region_key=key,
                        physical_table_id=table_ids[table.table_ordinal - 1],
                        cell_spans=(span,),
                        nested_owner_region_key=region_keys[
                            (
                                expected.nested_owner.table_ordinal,
                                expected.nested_owner.region_ordinal,
                            )
                        ],
                        reason_codes=("private_reference",),
                    )
                )
            else:
                regions.append(
                    D2NonBusinessFalseTableRegionCandidateV1(
                        disposition=expected.disposition,
                        region_key=key,
                        physical_table_id=table_ids[table.table_ordinal - 1],
                        cell_spans=(span,),
                        reason_codes=("private_reference",),
                    )
                )
    return _compile(
        source,
        regions=tuple(regions),
        scopes=tuple(
            D2TableLogicalScopeCandidateV1(
                logical_scope_key=scope_key,
                ordered_region_keys=tuple(keys),
                reason_codes=("private_reference",),
            )
            for scope_key, keys in scopes.items()
        ),
    )


def _compile_current_one_region_front(source: SourceIRSnapshot):
    manifest, _ = load_pds_private_region_boundary_oracle()
    table_ids = _table_ids(source)
    front_regions: list[object] = []
    front_scopes: list[D2TableLogicalScopeCandidateV1] = []
    for table_ordinal in range(1, 9):
        key = f"front-whole-{table_ordinal}"
        scope_key = f"front-scope-{table_ordinal}"
        cell_count = len(source.nodes[table_ordinal - 1].table_payload.cells)
        if table_ordinal == 8:
            front_regions.append(
                D2LogicalObjectPartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                    region_key=key,
                    physical_table_id=table_ids[table_ordinal - 1],
                    cell_spans=(_span(0, cell_count),),
                    logical_object_key="front-flow-text-object",
                    reason_codes=("one_region_per_table",),
                )
            )
            continue
        front_regions.append(
            D2LogicalScopePartTableRegionCandidateV1(
                disposition=D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
                region_key=key,
                physical_table_id=table_ids[table_ordinal - 1],
                cell_spans=(_span(0, cell_count),),
                logical_scope_key=scope_key,
                reason_codes=("one_region_per_table",),
            )
        )
        front_scopes.append(
            D2TableLogicalScopeCandidateV1(
                logical_scope_key=scope_key,
                ordered_region_keys=(key,),
                reason_codes=("one_region_per_table",),
            )
        )
    exact_tail = _tail_candidates(source, manifest=manifest)
    return _compile(
        source,
        regions=(*front_regions, *exact_tail[0]),
        scopes=(*front_scopes, *exact_tail[1]),
    )


def _tail_candidates(source: SourceIRSnapshot, *, manifest):  # type: ignore[no-untyped-def]
    table_ids = _table_ids(source)
    region_keys: dict[tuple[int, int], str] = {}
    regions: list[object] = []
    scopes: dict[str, list[str]] = {}
    for table in manifest.tables[8:]:
        expected = table.regions[0]
        key = f"t{table.table_ordinal}-r1"
        region_keys[(table.table_ordinal, 1)] = key
        cell_count = len(source.nodes[table.table_ordinal - 1].table_payload.cells)
        if expected.disposition is D2TableRegionDispositionKind.LOGICAL_SCOPE_PART:
            assert expected.scope_label is not None
            scope_key = expected.scope_label.lower()
            scopes.setdefault(scope_key, []).append(key)
            regions.append(
                D2LogicalScopePartTableRegionCandidateV1(
                    disposition=expected.disposition,
                    region_key=key,
                    physical_table_id=table_ids[table.table_ordinal - 1],
                    cell_spans=(_span(0, cell_count),),
                    logical_scope_key=scope_key,
                    reason_codes=("private_reference",),
                )
            )
        elif expected.disposition is D2TableRegionDispositionKind.NESTED_VALUE_REGION:
            assert expected.nested_owner is not None
            regions.append(
                D2NestedValueTableRegionCandidateV1(
                    disposition=expected.disposition,
                    region_key=key,
                    physical_table_id=table_ids[table.table_ordinal - 1],
                    cell_spans=(_span(0, cell_count),),
                    nested_owner_region_key=region_keys[
                        (expected.nested_owner.table_ordinal, 1)
                    ],
                    reason_codes=("private_reference",),
                )
            )
    return tuple(regions), tuple(
        D2TableLogicalScopeCandidateV1(
            logical_scope_key=scope_key,
            ordered_region_keys=tuple(keys),
            reason_codes=("private_reference",),
        )
        for scope_key, keys in scopes.items()
    )


def _compile(
    source: SourceIRSnapshot,
    *,
    regions: tuple[object, ...],
    scopes: tuple[D2TableLogicalScopeCandidateV1, ...],
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


def _source_fixture(
    *,
    source_sha256: str = PDS_PRIVATE_REGION_BOUNDARY_SOURCE_SHA256,
) -> SourceIRSnapshot:
    adapter_id = "pds-private-region-oracle-test"
    adapter_version = "1.0.0"
    first_counts = (48, 59, 167, 108, 56, 1062, 161, 170)
    cell_counts = (*first_counts, *(2 for _ in range(22)))
    nodes: list[SourceNode] = []
    for table_index, cell_count in enumerate(cell_counts):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=table_index + 1,
            table_index=table_index,
        )
        table_id = stable_table_id(
            source_sha256=source_sha256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            native_locator=locator,
        )
        cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, ordinal, 0),
                row=ordinal,
                column=0,
                raw_text=f"cell-{ordinal}",
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=table_index + 1,
                    table_index=table_index,
                    row_index=ordinal,
                    column_index=0,
                ),
            )
            for ordinal in range(cell_count)
        )
        table = TableIR(
            table_id=table_id,
            row_count=cell_count,
            column_count=1,
            cells=cells,
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=source_sha256,
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
    document_id = stable_document_id(source_sha256)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=source_sha256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )


def _table_ids(source: SourceIRSnapshot) -> tuple[str, ...]:
    return tuple(
        node.table_payload.table_id
        for node in source.nodes
        if node.table_payload is not None
    )


def _span(start: int, end: int) -> D2PhysicalTableCellOrdinalSpanV1:
    return D2PhysicalTableCellOrdinalSpanV1(
        start_cell_ordinal=start,
        end_cell_ordinal_exclusive=end,
    )


if __name__ == "__main__":
    unittest.main()
