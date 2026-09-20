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

from pydantic import ValidationError

from app.application.agent_workflow.d2_table_regions import (
    D2EmptyPhysicalTableDispositionCandidateV1,
    D2EmptyPhysicalTableDispositionKind,
    D2LogicalObjectPartTableRegionCandidateV1,
    D2LogicalScopePartTableRegionCandidateV1,
    D2NestedValueTableRegionCandidateV1,
    D2NonBusinessFalseTableRegionCandidateV1,
    D2PhysicalTableCellOrdinalSpanV1,
    D2TableLogicalScopeCandidateV1,
    D2TableRegionArtifactStatus,
    D2TableRegionArtifactV1,
    D2TableRegionCandidateArtifactV1,
    D2TableRegionDispositionKind,
    D2TableRegionGapPolicy,
    D2TableRegionRowAlignment,
    D2TableRegionRowMaterializerEligibility,
    D2UnresolvedTableRegionCandidateV1,
    compile_d2_table_regions,
    d2_table_region_source_ir_canonical_sha256,
    stable_d2_compiled_table_region_sha256,
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


SOURCE_SHA = "a" * 64
KIT_SHA = "b" * 64
D1_TABLE_MAP_ID = "d1-table-map-" + "1" * 32
D1_TABLE_MAP_SHA = "e" * 64


class D2TableRegionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _source_fixture((1, 2))
        self.table_ids = tuple(
            node.table_payload.table_id
            for node in self.source.nodes
            if node.table_payload is not None
        )

    def test_one_physical_table_supports_two_sibling_logical_regions(self) -> None:
        first_table = self.table_ids[0]
        candidate = _candidate(
            self.source,
            regions=(
                _scope_region("scope-a-region", first_table, 0, 2, "scope-a"),
                _scope_region("scope-b-region", first_table, 2, 4, "scope-b"),
                _false_region("second-table-false", self.table_ids[1], 0, 4),
            ),
            scopes=(
                _scope("scope-a", ("scope-a-region",)),
                _scope("scope-b", ("scope-b-region",)),
            ),
        )

        artifact = _compile(self.source, candidate)

        first_coverage = artifact.physical_table_cell_coverage[0]
        self.assertEqual(len(first_coverage.ordered_region_ids), 2)
        self.assertEqual(len(artifact.logical_scopes), 2)
        self.assertEqual(artifact.status, D2TableRegionArtifactStatus.COMPLETE)
        self.assertEqual(
            tuple(
                ref.canonical_cell_ordinal
                for region in artifact.regions[:2]
                for ref in region.cell_refs
            ),
            (0, 1, 2, 3),
        )

    def test_logical_scope_continues_across_physical_tables_by_region_key(self) -> None:
        candidate = _candidate(
            self.source,
            regions=(
                _scope_region("continuation-p1", self.table_ids[0], 0, 4, "scope-a"),
                _scope_region("continuation-p2", self.table_ids[1], 0, 4, "scope-a"),
            ),
            scopes=(
                _scope(
                    "scope-a",
                    ("continuation-p1", "continuation-p2"),
                ),
            ),
        )

        artifact = _compile(self.source, candidate)

        logical_scope = artifact.logical_scopes[0]
        self.assertEqual(len(logical_scope.ordered_region_ids), 2)
        scope_regions = tuple(
            region
            for region in artifact.regions
            if region.logical_scope_id == logical_scope.logical_scope_id
        )
        self.assertEqual(
            tuple(region.physical_table_id for region in scope_regions),
            self.table_ids,
        )
        self.assertEqual(
            tuple(region.cell_refs[0].page_number for region in scope_regions),
            (1, 2),
        )

    def test_same_table_can_hold_logical_table_and_ordinary_prose(self) -> None:
        candidate = _candidate(
            self.source,
            regions=(
                _scope_region("logical-grid", self.table_ids[0], 0, 2, "scope-a"),
                D2LogicalObjectPartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                    region_key="ordinary-prose",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(2, 4),),
                    logical_object_key="paragraph-a",
                    reason_codes=("ordinary_content_inside_table_frame",),
                ),
                _false_region("second-table-false", self.table_ids[1], 0, 4),
            ),
            scopes=(_scope("scope-a", ("logical-grid",)),),
        )

        artifact = _compile(self.source, candidate)

        ordinary = next(
            region
            for region in artifact.regions
            if region.disposition is D2TableRegionDispositionKind.LOGICAL_OBJECT_PART
        )
        self.assertIsNotNone(ordinary.logical_object_id)
        self.assertIsNone(ordinary.logical_scope_id)
        self.assertEqual(len(artifact.logical_objects), 1)
        dumped = artifact.model_dump(mode="json")
        payload = json.dumps(dumped, ensure_ascii=False)
        self.assertNotIn("table-1-cell-2", payload)
        self.assertNotIn("table-1-cell-3", payload)
        self.assertNotIn("raw_text", _nested_keys(dumped))
        self.assertNotIn("formula", _nested_keys(dumped))
        self.assertNotIn("display_value", _nested_keys(dumped))
        first_ref = ordinary.cell_refs[0]
        self.assertEqual((first_ref.row, first_ref.column), (1, 0))
        self.assertEqual(first_ref.bbox, (0.0, 40.0, 100.0, 80.0))
        self.assertRegex(first_ref.cell_evidence_sha256, r"^[0-9a-f]{64}$")
        scope_region = next(
            region
            for region in artifact.regions
            if region.disposition is D2TableRegionDispositionKind.LOGICAL_SCOPE_PART
        )
        self.assertEqual(
            scope_region.row_alignment, D2TableRegionRowAlignment.FULL_ROWS
        )
        self.assertEqual(
            scope_region.row_materializer_eligibility,
            D2TableRegionRowMaterializerEligibility.SHAPE_ELIGIBLE_FULL_ROWS,
        )
        self.assertEqual(
            ordinary.row_materializer_eligibility,
            D2TableRegionRowMaterializerEligibility.INELIGIBLE_DISPOSITION,
        )

    def test_noncontiguous_scope_nested_and_false_regions_are_exact(self) -> None:
        candidate = _candidate(
            self.source,
            regions=(
                D2LogicalScopePartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
                    region_key="owner-region",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(0, 1), _span(3, 4)),
                    logical_scope_key="scope-a",
                    reason_codes=("logical_grid",),
                ),
                D2NestedValueTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                    region_key="nested-region",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(1, 2),),
                    nested_owner_region_key="owner-region",
                    reason_codes=("nested_value",),
                ),
                _false_region("layout-false", self.table_ids[0], 2, 3),
                _false_region("second-table-false", self.table_ids[1], 0, 4),
            ),
            scopes=(_scope("scope-a", ("owner-region",)),),
        )

        artifact = _compile(self.source, candidate)

        owner = next(
            region
            for region in artifact.regions
            if region.candidate_region_key == "owner-region"
        )
        nested = next(
            region
            for region in artifact.regions
            if region.candidate_region_key == "nested-region"
        )
        false = next(
            region
            for region in artifact.regions
            if region.candidate_region_key == "layout-false"
        )
        self.assertEqual(nested.nested_owner_region_id, owner.region_id)
        self.assertIsNone(false.logical_scope_id)
        self.assertIsNone(false.logical_object_id)
        self.assertIsNone(false.nested_owner_region_id)
        self.assertEqual(
            tuple(
                (span.start_cell_ordinal, span.end_cell_ordinal_exclusive)
                for span in owner.cell_spans
            ),
            ((0, 1), (3, 4)),
        )

    def test_disposition_fields_are_strict_and_nested_owner_must_exist(self) -> None:
        with self.assertRaises(ValidationError):
            D2LogicalScopePartTableRegionCandidateV1.model_validate(
                {
                    "disposition": "LOGICAL_SCOPE_PART",
                    "region_key": "scope-region",
                    "physical_table_id": self.table_ids[0],
                    "cell_spans": [
                        {"start_cell_ordinal": 0, "end_cell_ordinal_exclusive": 4}
                    ],
                    "logical_scope_key": "scope-a",
                    "nested_owner_region_key": "other-region",
                    "reason_codes": ["logical_grid"],
                }
            )

        orphan = D2NestedValueTableRegionCandidateV1(
            disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
            region_key="nested-region",
            physical_table_id=self.table_ids[0],
            cell_spans=(_span(0, 4),),
            nested_owner_region_key="missing-owner",
            reason_codes=("nested_value",),
        )
        with self.assertRaisesRegex(
            ValidationError, "owner must reference an existing"
        ):
            _candidate(self.source, regions=(orphan,))

    def test_overlap_unknown_out_of_bounds_and_reordered_spans_are_rejected(
        self,
    ) -> None:
        overlap = _candidate(
            self.source,
            regions=(
                _false_region("first", self.table_ids[0], 0, 3),
                _false_region("second", self.table_ids[0], 2, 4),
                _false_region("second-table", self.table_ids[1], 0, 4),
            ),
        )
        with self.assertRaisesRegex(ValueError, "overlapping regions"):
            _compile(self.source, overlap)

        out_of_bounds = _candidate(
            self.source,
            regions=(
                _false_region("too-far", self.table_ids[0], 0, 5),
                _false_region("second-table", self.table_ids[1], 0, 4),
            ),
        )
        with self.assertRaisesRegex(ValueError, "exceeds physical TableIR"):
            _compile(self.source, out_of_bounds)

        unknown = _candidate(
            self.source,
            regions=(_false_region("unknown", "missing-table", 0, 1),),
        )
        with self.assertRaisesRegex(ValueError, "unknown physical TableIR"):
            _compile(self.source, unknown)

        with self.assertRaisesRegex(ValidationError, "canonical ascending order"):
            D2NonBusinessFalseTableRegionCandidateV1(
                disposition=D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION,
                region_key="reordered",
                physical_table_id=self.table_ids[0],
                cell_spans=(_span(2, 3), _span(0, 1)),
                reason_codes=("layout_only",),
            )

    def test_reordered_logical_scope_members_are_rejected_by_host(self) -> None:
        candidate = _candidate(
            self.source,
            regions=(
                _scope_region("page-one", self.table_ids[0], 0, 4, "scope-a"),
                _scope_region("page-two", self.table_ids[1], 0, 4, "scope-a"),
            ),
            scopes=(_scope("scope-a", ("page-two", "page-one")),),
        )

        with self.assertRaisesRegex(ValueError, "canonical physical-cell order"):
            _compile(self.source, candidate)

    def test_gap_policy_can_auto_fill_only_missing_cells_as_unresolved(self) -> None:
        candidate = _candidate(
            self.source,
            regions=(
                _scope_region("middle", self.table_ids[0], 1, 3, "scope-a"),
                _false_region("second-table", self.table_ids[1], 0, 4),
            ),
            scopes=(_scope("scope-a", ("middle",)),),
        )

        with self.assertRaisesRegex(ValueError, "must disposition every"):
            _compile(self.source, candidate)

        artifact = _compile(
            self.source,
            candidate,
            gap_policy=D2TableRegionGapPolicy.AUTO_FILL_UNRESOLVED,
        )

        self.assertEqual(artifact.status, D2TableRegionArtifactStatus.PARTIAL)
        self.assertEqual(len(artifact.host_auto_filled_region_ids), 1)
        unresolved = next(
            region for region in artifact.regions if region.host_auto_filled
        )
        self.assertEqual(
            tuple(
                (span.start_cell_ordinal, span.end_cell_ordinal_exclusive)
                for span in unresolved.cell_spans
            ),
            ((0, 1), (3, 4)),
        )
        self.assertEqual(
            tuple(ref.canonical_cell_ordinal for ref in unresolved.cell_refs),
            (0, 3),
        )
        self.assertEqual(
            artifact.physical_table_cell_coverage[0].canonical_cell_ids,
            tuple(
                ref.cell_id
                for ref in sorted(
                    (
                        ref
                        for region in artifact.regions
                        if region.physical_table_id == self.table_ids[0]
                        for ref in region.cell_refs
                    ),
                    key=lambda item: item.canonical_cell_ordinal,
                )
            ),
        )
        middle = next(
            region
            for region in artifact.regions
            if region.candidate_region_key == "middle"
        )
        self.assertEqual(middle.row_alignment, D2TableRegionRowAlignment.PARTIAL_ROWS)
        self.assertEqual(
            middle.row_materializer_eligibility,
            D2TableRegionRowMaterializerEligibility.INELIGIBLE_PARTIAL_ROW,
        )

    def test_shuffled_candidate_collections_compile_to_stable_replay(self) -> None:
        first = _scope_region("first", self.table_ids[0], 0, 4, "scope-a")
        second = _scope_region("second", self.table_ids[1], 0, 4, "scope-b")
        scope_a = _scope("scope-a", ("first",))
        scope_b = _scope("scope-b", ("second",))
        canonical_candidate = _candidate(
            self.source,
            regions=(first, second),
            scopes=(scope_a, scope_b),
        )
        shuffled_candidate = _candidate(
            self.source,
            regions=(second, first),
            scopes=(scope_b, scope_a),
        )

        canonical = _compile(self.source, canonical_candidate)
        shuffled = _compile(self.source, shuffled_candidate)

        self.assertEqual(shuffled.artifact_id, canonical.artifact_id)
        self.assertEqual(shuffled.artifact_sha256, canonical.artifact_sha256)
        self.assertEqual(
            shuffled.model_dump(mode="json"),
            canonical.model_dump(mode="json"),
        )

    def test_artifact_id_binds_disposition_and_status_not_only_cell_identity(
        self,
    ) -> None:
        source = _source_fixture((1,))
        table_id = source.nodes[0].table_payload.table_id
        false_artifact = _compile(
            source,
            _candidate(
                source,
                regions=(_false_region("same-cells", table_id, 0, 4),),
            ),
        )
        unresolved_artifact = _compile(
            source,
            _candidate(
                source,
                regions=(_unresolved_region("same-cells", table_id, 0, 4),),
            ),
        )

        self.assertEqual(
            false_artifact.regions[0].region_id,
            unresolved_artifact.regions[0].region_id,
        )
        self.assertNotEqual(
            false_artifact.regions[0].region_sha256,
            unresolved_artifact.regions[0].region_sha256,
        )
        self.assertNotEqual(false_artifact.artifact_id, unresolved_artifact.artifact_id)
        self.assertEqual(false_artifact.status, D2TableRegionArtifactStatus.COMPLETE)
        self.assertEqual(
            unresolved_artifact.status,
            D2TableRegionArtifactStatus.PARTIAL,
        )

    def test_region_id_is_local_while_artifact_binds_full_source_revision(
        self,
    ) -> None:
        complete_source = _source_fixture((1,))
        partial_source = complete_source.model_copy(
            update={"status": SourceIRStatus.PARTIAL}
        )
        table_id = complete_source.nodes[0].table_payload.table_id
        complete = _compile(
            complete_source,
            _candidate(
                complete_source,
                regions=(_false_region("same-local-region", table_id, 0, 4),),
            ),
        )
        partial = _compile(
            partial_source,
            _candidate(
                partial_source,
                regions=(_false_region("same-local-region", table_id, 0, 4),),
            ),
        )

        self.assertEqual(complete.regions[0].region_id, partial.regions[0].region_id)
        self.assertNotEqual(
            complete.regions[0].region_sha256,
            partial.regions[0].region_sha256,
        )
        self.assertNotEqual(complete.artifact_id, partial.artifact_id)

    def test_artifact_revalidates_nested_business_owner_and_cycles(self) -> None:
        candidate = _candidate(
            self.source,
            regions=(
                _scope_region("owner", self.table_ids[0], 0, 1, "scope-a"),
                D2NestedValueTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                    region_key="nested-a",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(1, 2),),
                    nested_owner_region_key="owner",
                    reason_codes=("nested_value",),
                ),
                D2NestedValueTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                    region_key="nested-b",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(2, 3),),
                    nested_owner_region_key="nested-a",
                    reason_codes=("nested_value",),
                ),
                _false_region("false", self.table_ids[0], 3, 4),
                _false_region("second-table", self.table_ids[1], 0, 4),
            ),
            scopes=(_scope("scope-a", ("owner",)),),
        )
        artifact = _compile(self.source, candidate)
        nested_a = next(
            region
            for region in artifact.regions
            if region.candidate_region_key == "nested-a"
        )
        nested_b = next(
            region
            for region in artifact.regions
            if region.candidate_region_key == "nested-b"
        )
        false = next(
            region
            for region in artifact.regions
            if region.candidate_region_key == "false"
        )

        false_owned = _replace_nested_owner(nested_a, false.region_id)
        with self.assertRaisesRegex(ValidationError, "business-bearing region"):
            _validate_with_replaced_regions(artifact, (false_owned,))

        cycle_a = _replace_nested_owner(nested_a, nested_b.region_id)
        with self.assertRaisesRegex(ValidationError, "must be acyclic"):
            _validate_with_replaced_regions(artifact, (cycle_a,))

    def test_empty_table_has_explicit_or_host_unresolved_disposition(self) -> None:
        source = _source_fixture((1,), empty_table_indices=(0,))
        table_id = source.nodes[0].table_payload.table_id
        no_disposition = _candidate(source, regions=())

        with self.assertRaisesRegex(ValueError, "empty physical TableIR requires"):
            _compile(source, no_disposition)

        auto = _compile(
            source,
            no_disposition,
            gap_policy=D2TableRegionGapPolicy.AUTO_FILL_UNRESOLVED,
        )
        self.assertEqual(auto.status, D2TableRegionArtifactStatus.PARTIAL)
        self.assertEqual(auto.regions, ())
        self.assertEqual(auto.unresolved_empty_table_ids, (table_id,))
        self.assertEqual(auto.host_auto_filled_empty_table_ids, (table_id,))
        self.assertEqual(
            auto.physical_table_cell_coverage[0].empty_table_disposition,
            D2EmptyPhysicalTableDispositionKind.UNRESOLVED,
        )

        explicit_false = _candidate(
            source,
            regions=(),
            empty_table_dispositions=(
                D2EmptyPhysicalTableDispositionCandidateV1(
                    physical_table_id=table_id,
                    disposition=(
                        D2EmptyPhysicalTableDispositionKind.NON_BUSINESS_FALSE_REGION
                    ),
                    reason_codes=("layout_only",),
                ),
            ),
        )
        false_artifact = _compile(source, explicit_false)
        self.assertEqual(false_artifact.status, D2TableRegionArtifactStatus.COMPLETE)
        self.assertEqual(false_artifact.unresolved_empty_table_ids, ())

    def test_cell_coverage_separates_supplemental_text_and_visual_evidence(
        self,
    ) -> None:
        source = _source_fixture((1,), supplemental_table_indices=(0,))
        table_id = source.nodes[0].table_payload.table_id
        artifact = _compile(
            source,
            _candidate(
                source,
                regions=(_false_region("all-cells", table_id, 0, 4),),
            ),
        )
        coverage = artifact.physical_table_cell_coverage[0]

        self.assertEqual(coverage.coverage_scope, "TABLE_IR_CELL_ANCHORS_ONLY")
        self.assertEqual(
            coverage.supplemental_text_scope,
            "HASH_BOUND_NOT_CELL_DISPOSITIONED",
        )
        self.assertNotEqual(coverage.table_node_raw_text_sha256, "0" * 64)
        self.assertNotEqual(coverage.table_notes_sha256, "0" * 64)
        self.assertEqual(len(coverage.supplemental_visual_evidence), 1)

    def test_all_runner_arrays_have_explicit_schema_bounds(self) -> None:
        for model in (D2TableRegionCandidateArtifactV1, D2TableRegionArtifactV1):
            missing: list[str] = []
            _collect_unbounded_arrays(model.model_json_schema(), "$", missing)
            self.assertEqual(missing, [], f"unbounded arrays in {model.__name__}")

    def test_source_and_kit_drift_are_rejected(self) -> None:
        candidate = _candidate(
            self.source,
            regions=(
                _false_region("first-table", self.table_ids[0], 0, 4),
                _false_region("second-table", self.table_ids[1], 0, 4),
            ),
        )

        stale_source_candidate = candidate.model_copy(
            update={"source_ir_canonical_sha256": "c" * 64}
        )
        with self.assertRaisesRegex(ValueError, "SourceIR identity is stale"):
            _compile(self.source, stale_source_candidate)

        with self.assertRaisesRegex(ValueError, "Agent Kit identity is stale"):
            compile_d2_table_regions(
                self.source,
                candidate,
                expected_kit_bundle_sha256="d" * 64,
                expected_d1_table_map_artifact_id=D1_TABLE_MAP_ID,
                expected_d1_table_map_artifact_sha256=D1_TABLE_MAP_SHA,
            )

        stale_d1_candidate = candidate.model_copy(
            update={"d1_table_map_artifact_sha256": "f" * 64}
        )
        with self.assertRaisesRegex(ValueError, "D1 table-map lineage is stale"):
            _compile(self.source, stale_d1_candidate)


def _candidate(
    source: SourceIRSnapshot,
    *,
    regions: tuple[object, ...],
    scopes: tuple[D2TableLogicalScopeCandidateV1, ...] = (),
    empty_table_dispositions: tuple[
        D2EmptyPhysicalTableDispositionCandidateV1, ...
    ] = (),
) -> D2TableRegionCandidateArtifactV1:
    return D2TableRegionCandidateArtifactV1(
        source_ir_id=source.source_ir_id,
        source_sha256=source.source_sha256,
        source_ir_canonical_sha256=d2_table_region_source_ir_canonical_sha256(source),
        kit_bundle_sha256=KIT_SHA,
        d1_table_map_artifact_id=D1_TABLE_MAP_ID,
        d1_table_map_artifact_sha256=D1_TABLE_MAP_SHA,
        regions=regions,
        logical_scopes=scopes,
        empty_table_dispositions=empty_table_dispositions,
    )


def _compile(
    source: SourceIRSnapshot,
    candidate: D2TableRegionCandidateArtifactV1,
    *,
    gap_policy: D2TableRegionGapPolicy = D2TableRegionGapPolicy.REJECT,
):
    return compile_d2_table_regions(
        source,
        candidate,
        expected_kit_bundle_sha256=KIT_SHA,
        expected_d1_table_map_artifact_id=D1_TABLE_MAP_ID,
        expected_d1_table_map_artifact_sha256=D1_TABLE_MAP_SHA,
        gap_policy=gap_policy,
    )


def _scope_region(
    region_key: str,
    table_id: str,
    start: int,
    end: int,
    scope_key: str,
) -> D2LogicalScopePartTableRegionCandidateV1:
    return D2LogicalScopePartTableRegionCandidateV1(
        disposition=D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
        region_key=region_key,
        physical_table_id=table_id,
        cell_spans=(_span(start, end),),
        logical_scope_key=scope_key,
        reason_codes=("logical_grid",),
    )


def _false_region(
    region_key: str,
    table_id: str,
    start: int,
    end: int,
) -> D2NonBusinessFalseTableRegionCandidateV1:
    return D2NonBusinessFalseTableRegionCandidateV1(
        disposition=D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION,
        region_key=region_key,
        physical_table_id=table_id,
        cell_spans=(_span(start, end),),
        reason_codes=("layout_only",),
    )


def _unresolved_region(
    region_key: str,
    table_id: str,
    start: int,
    end: int,
) -> D2UnresolvedTableRegionCandidateV1:
    return D2UnresolvedTableRegionCandidateV1(
        disposition=D2TableRegionDispositionKind.UNRESOLVED,
        region_key=region_key,
        physical_table_id=table_id,
        cell_spans=(_span(start, end),),
        reason_codes=("semantic_boundary_unresolved",),
    )


def _scope(
    scope_key: str,
    ordered_region_keys: tuple[str, ...],
) -> D2TableLogicalScopeCandidateV1:
    return D2TableLogicalScopeCandidateV1(
        logical_scope_key=scope_key,
        ordered_region_keys=ordered_region_keys,
        reason_codes=("same_logical_table",),
    )


def _span(start: int, end: int) -> D2PhysicalTableCellOrdinalSpanV1:
    return D2PhysicalTableCellOrdinalSpanV1(
        start_cell_ordinal=start,
        end_cell_ordinal_exclusive=end,
    )


def _source_fixture(
    pages: tuple[int, ...],
    *,
    empty_table_indices: tuple[int, ...] = (),
    supplemental_table_indices: tuple[int, ...] = (),
) -> SourceIRSnapshot:
    adapter_id = "table-region-test"
    adapter_version = "1.0.0"
    nodes: list[SourceNode] = []
    for table_index, page_number in enumerate(pages):
        table_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=table_index,
            bbox=(10.0, 10.0, 200.0, 100.0),
        )
        table_id = stable_table_id(
            source_sha256=SOURCE_SHA,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            native_locator=table_locator,
        )
        cells = (
            tuple(
                TableCellIR(
                    cell_id=stable_cell_id(table_id, row, column),
                    row=row,
                    column=column,
                    raw_text=f"table-{table_index + 1}-cell-{row * 2 + column}",
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
                for row in range(2)
                for column in range(2)
            )
            if table_index not in empty_table_indices
            else ()
        )
        table = TableIR(
            table_id=table_id,
            row_count=2,
            column_count=2,
            cells=cells,
            notes=("visible table note",)
            if table_index in supplemental_table_indices
            else (),
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_SHA,
                    adapter_id=adapter_id,
                    adapter_version=adapter_version,
                    kind=SourceNodeKind.TABLE,
                    native_locator=table_locator,
                ),
                kind=SourceNodeKind.TABLE,
                source_order=table_index,
                raw_text=(
                    "visible text inside the table frame"
                    if table_index in supplemental_table_indices
                    else ""
                ),
                native_locator=table_locator,
                visual_locator=(
                    table_locator if table_index in supplemental_table_indices else None
                ),
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


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(
            *(_nested_keys(item) for item in value.values()),
        )
    if isinstance(value, list):
        return set().union(*(_nested_keys(item) for item in value))
    return set()


def _replace_nested_owner(region, owner_region_id: str):
    values = {
        "region_id": region.region_id,
        "candidate_region_key": region.candidate_region_key,
        "source_ir_id": region.source_ir_id,
        "source_sha256": region.source_sha256,
        "source_ir_canonical_sha256": region.source_ir_canonical_sha256,
        "kit_bundle_sha256": region.kit_bundle_sha256,
        "physical_table_id": region.physical_table_id,
        "table_source_node_id": region.table_source_node_id,
        "physical_table_source_order": region.physical_table_source_order,
        "cell_spans": region.cell_spans,
        "cell_refs": region.cell_refs,
        "disposition": region.disposition,
        "logical_scope_id": region.logical_scope_id,
        "logical_object_id": region.logical_object_id,
        "nested_owner_region_id": owner_region_id,
        "reason_codes": region.reason_codes,
        "host_auto_filled": region.host_auto_filled,
        "covered_row_indices": region.covered_row_indices,
        "fully_covered_row_indices": region.fully_covered_row_indices,
        "partially_covered_row_indices": region.partially_covered_row_indices,
        "row_alignment": region.row_alignment,
        "row_materializer_eligibility": region.row_materializer_eligibility,
    }
    return region.model_copy(
        update={
            "nested_owner_region_id": owner_region_id,
            "region_sha256": stable_d2_compiled_table_region_sha256(**values),
        }
    )


def _validate_with_replaced_regions(
    artifact: D2TableRegionArtifactV1,
    replacements: tuple[object, ...],
) -> None:
    replacements_by_id = {item.region_id: item for item in replacements}
    payload = artifact.model_dump(mode="json")
    payload["regions"] = [
        replacements_by_id.get(region.region_id, region).model_dump(mode="json")
        for region in artifact.regions
    ]
    D2TableRegionArtifactV1.model_validate(payload)


def _collect_unbounded_arrays(value: object, path: str, missing: list[str]) -> None:
    if isinstance(value, dict):
        if value.get("type") == "array" and "maxItems" not in value:
            missing.append(path)
        for key, item in value.items():
            _collect_unbounded_arrays(item, f"{path}.{key}", missing)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _collect_unbounded_arrays(item, f"{path}[{index}]", missing)


if __name__ == "__main__":
    unittest.main()
