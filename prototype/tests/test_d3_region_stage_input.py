from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import unittest

from pydantic import ValidationError

from app.application.agent_workflow.d2_decision_candidate import (
    d2_source_node_content_sha256,
)
from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalObjectV1,
    D2LogicalStructureProposalV1,
    D2SourceHandleKind,
    stable_d2_derived_container_id,
    stable_d2_logical_object_sha256,
    stable_d2_logical_structure_proposal_sha256,
    stable_d2_source_segment_id,
    stable_d2_source_segment_sha256,
    validate_d2_logical_structure_proposal,
)
from app.application.agent_workflow.d2_logical_structure_v2 import (
    D2BaseTableReplacementCandidateV1,
    D2BaseTableReplacementDisposition,
    D2RegionLogicalObjectPlacementMode,
    D2RegionLogicalObjectPlacementV1,
    D2RegionLogicalScopePlacementV1,
    D2RetainedBaseObjectPlacementV1,
    D2SupplementalObjectOwnerReferenceKindV1,
    D2TableRawTextCellDerivationV1,
    D2TableSupplementalDispositionKindV1,
    D2TableSupplementalEvidenceKindV1,
    D2TableSupplementalEvidencePlacementV1,
    D2V2BaseSourceSegmentRefV1,
    D2V2SourceSegmentKind,
    build_d2_region_placement_plan,
    build_d2_table_supplemental_object_owner_placements,
    compile_d2_logical_structure_v2,
    stable_d2_v2_source_segment_id,
    stable_d2_v2_source_segment_sha256,
)
from app.application.agent_workflow.d2_table_region_parent_input import (
    build_d2_table_region_parent_input,
)
from app.application.agent_workflow.d2_table_regions import (
    D2LogicalObjectPartTableRegionCandidateV1,
    D2NestedValueTableRegionCandidateV1,
    D2TableRegionDispositionKind,
    D2UnresolvedTableRegionCandidateV1,
)
from app.application.agent_workflow.d3_region_stage_input import (
    D3EvidenceLedgerKindV1,
    D3RegionStageInputError,
    D3RegionStageInputStatus,
    D3UnifiedEvidenceKindV3,
    D3UnifiedExactSourceSegmentSetV3,
    D3UnifiedSegmentRoleV3,
    D3UnresolvedScopeKindV1,
    build_d3_region_stage_input_lineage,
    materialize_d3_region_stage_input,
    _resolve_base_segment,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceAssetKind,
    SourceAssetRef,
    SourceNode,
    SourceNodeKind,
    stable_node_id,
)

if __package__:
    from .test_d2_logical_structure_v2 import (
        _base_proposal_and_receipt,
        _d1_identity,
        _false_region,
        _kit_identity,
        _regions,
        _scope_candidate,
        _scope_region,
        _source_fixture,
        _span,
    )
else:
    from test_d2_logical_structure_v2 import (
        _base_proposal_and_receipt,
        _d1_identity,
        _false_region,
        _kit_identity,
        _regions,
        _scope_candidate,
        _scope_region,
        _source_fixture,
        _span,
    )


class D3RegionStageInputTests(unittest.TestCase):
    def test_empty_table_cell_base_segment_replays_authoritative_d2_hash(self) -> None:
        original = _source_fixture()
        table_node = next(
            item for item in original.nodes if item.kind is SourceNodeKind.TABLE
        )
        assert table_node.table_payload is not None
        cells = list(table_node.table_payload.cells)
        cells[0] = cells[0].model_copy(update={"raw_text": ""})
        empty_table = table_node.table_payload.model_copy(
            update={"cells": tuple(cells)}
        )
        empty_node = table_node.model_copy(update={"table_payload": empty_table})
        source = original.model_copy(
            update={
                "nodes": tuple(
                    empty_node
                    if item.source_node_id == table_node.source_node_id
                    else item
                    for item in original.nodes
                )
            }
        )
        fixture = _Fixture(source=source)
        prepared = fixture.prepare(fixture.complete_scope_artifact())
        parent_segment = next(
            item
            for item in prepared.parent_input.source_segments
            if item.cell_ids == (cells[0].cell_id,)
        )
        base_ref = _base_source_ref(
            source=source,
            node=empty_node,
            handle_kind=D2SourceHandleKind.TABLE_CELL,
            source_handle=cells[0].cell_id,
            source_content_sha256=hashlib.sha256(b"").hexdigest(),
            page_numbers=parent_segment.page_numbers,
        )

        resolved = _resolve_base_segment(
            base_ref,
            logical_object_id=fixture.text_object.logical_object_id,
            source=source,
            nodes={item.source_node_id: item for item in source.nodes},
            parent_input=prepared.parent_input,
            intra_node_ordinal=0,
        )

        self.assertEqual(resolved.source_text, "")
        self.assertEqual(
            resolved.source_content_sha256, hashlib.sha256(b"").hexdigest()
        )
        with self.assertRaisesRegex(D3RegionStageInputError, "BASE_CONTENT_DRIFT"):
            _resolve_base_segment(
                base_ref.model_copy(update={"base_source_segment_sha256": "f" * 64}),
                logical_object_id=fixture.text_object.logical_object_id,
                source=source,
                nodes={item.source_node_id: item for item in source.nodes},
                parent_input=prepared.parent_input,
                intra_node_ordinal=0,
            )

    def test_asset_base_segment_uses_d2_exclude_none_canonical_hash(self) -> None:
        fixture = _Fixture()
        prepared = fixture.prepare(fixture.complete_scope_artifact())
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=4,
            block_index=0,
        )
        asset = SourceAssetRef(
            asset_id="asset-" + "d" * 32,
            kind=SourceAssetKind.FIGURE,
            content_sha256="e" * 64,
            media_type="image/png",
            storage_key="source-assets/ee/figure.png",
            native_locator=locator,
        )
        node = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=fixture.source.source_sha256,
                adapter_id=fixture.source.adapter_id,
                adapter_version=fixture.source.adapter_version,
                kind=SourceNodeKind.FIGURE,
                native_locator=locator,
            ),
            kind=SourceNodeKind.FIGURE,
            source_order=len(fixture.source.nodes),
            raw_text="",
            native_locator=locator,
            asset_ref=asset.asset_id,
        )
        source = fixture.source.model_copy(
            update={
                "nodes": (*fixture.source.nodes, node),
                "assets": (*fixture.source.assets, asset),
            }
        )
        authoritative_sha = d2_source_node_content_sha256(node)
        full_dump_sha = _canonical_sha(node.model_dump(mode="json"))
        self.assertNotEqual(authoritative_sha, full_dump_sha)
        base_ref = _base_source_ref(
            source=source,
            node=node,
            handle_kind=D2SourceHandleKind.SOURCE_NODE,
            source_handle=node.source_node_id,
            source_content_sha256=authoritative_sha,
            page_numbers=(4,),
        )

        resolved = _resolve_base_segment(
            base_ref,
            logical_object_id=fixture.text_object.logical_object_id,
            source=source,
            nodes={item.source_node_id: item for item in source.nodes},
            parent_input=prepared.parent_input,
            intra_node_ordinal=0,
        )

        self.assertEqual(resolved.source_text, "")
        self.assertEqual(resolved.source_content_sha256, authoritative_sha)

    def test_host_order_covers_same_table_siblings_and_cross_table_scope(self) -> None:
        fixture = _Fixture()
        first_table, second_table = fixture.table_ids
        artifact = _regions(
            fixture.source,
            regions=(
                _scope_region("scope-a-first", first_table, 0, 2, "scope-a"),
                _scope_region("scope-b", first_table, 2, 4, "scope-b"),
                _scope_region("scope-a-second", second_table, 0, 4, "scope-a"),
            ),
            scopes=(
                _scope_candidate(
                    "scope-a",
                    ("scope-a-first", "scope-a-second"),
                ),
                _scope_candidate("scope-b", ("scope-b",)),
            ),
        )

        result = fixture.materialize(artifact)
        segment_set = result.segment_set
        positions = tuple(item.source_position for item in segment_set.segments)

        self.assertEqual(positions, tuple(sorted(positions)))
        self.assertEqual(len(positions), len(set(positions)))
        table_cells = tuple(
            item
            for item in segment_set.segments
            if item.evidence_kind is D3UnifiedEvidenceKindV3.TABLE_CELL
        )
        self.assertEqual(
            tuple(item.source_position for item in table_cells),
            (
                (1, 10, 0, 0),
                (1, 10, 1, 0),
                (1, 10, 2, 0),
                (1, 10, 3, 0),
                (2, 10, 0, 0),
                (2, 10, 1, 0),
                (2, 10, 2, 0),
                (2, 10, 3, 0),
            ),
        )
        scope_a_id = next(
            item.logical_scope_id
            for item in artifact.logical_scopes
            if item.candidate_scope_key == "scope-a"
        )
        scope_b_id = next(
            item.logical_scope_id
            for item in artifact.logical_scopes
            if item.candidate_scope_key == "scope-b"
        )
        scope_a = _object_scope(segment_set, scope_a_id)
        scope_b = _object_scope(segment_set, scope_b_id)
        a_cells = tuple(
            _segment(segment_set, segment_id)
            for segment_id in scope_a.primary_segment_ids
        )
        b_cells = tuple(
            _segment(segment_set, segment_id)
            for segment_id in scope_b.primary_segment_ids
        )
        self.assertEqual(
            tuple(item.source_text for item in a_cells),
            (
                "table-1-cell-0",
                "table-1-cell-1",
                "table-2-cell-0",
                "table-2-cell-1",
                "table-2-cell-2",
                "table-2-cell-3",
            ),
        )
        self.assertEqual(
            tuple(item.source_text for item in b_cells),
            ("table-1-cell-2", "table-1-cell-3"),
        )
        self.assertTrue(
            set(scope_a.primary_segment_ids).isdisjoint(scope_b.primary_segment_ids)
        )

    def test_prose_merge_orders_base_range_before_region_cells(self) -> None:
        fixture = _Fixture()
        first_table, second_table = fixture.table_ids
        artifact = _regions(
            fixture.source,
            regions=(
                D2LogicalObjectPartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                    region_key="prose-in-frame",
                    physical_table_id=first_table,
                    cell_spans=(_span(0, 4),),
                    logical_object_key="prose-a",
                    reason_codes=("ordinary_content_inside_table_frame",),
                ),
                _false_region("layout-second-table", second_table, 0, 4),
            ),
        )
        placement = D2RegionLogicalObjectPlacementV1(
            placement_key="merge-prose",
            source_region_logical_object_ids=(
                artifact.logical_objects[0].logical_object_id,
            ),
            mode=D2RegionLogicalObjectPlacementMode.MERGE_RETAINED_BASE,
            target_base_logical_object_id=fixture.text_object.logical_object_id,
            parent_logical_object_id=None,
            reason_codes=("merge_with_source_paragraph",),
        )

        result = fixture.materialize(
            artifact,
            object_placements=(placement,),
        )
        owner = _object_scope(
            result.segment_set,
            fixture.text_object.logical_object_id,
        )
        primary = tuple(
            _segment(result.segment_set, item) for item in owner.primary_segment_ids
        )

        self.assertEqual(primary[0].evidence_kind, D3UnifiedEvidenceKindV3.BASE_TEXT)
        self.assertEqual(primary[0].source_text, "A source-faithful provision.")
        self.assertEqual(
            tuple(item.source_text for item in primary[1:]),
            tuple(f"table-1-cell-{index}" for index in range(4)),
        )
        self.assertEqual(
            owner.ordered_segment_ids,
            tuple(
                item.segment_id
                for item in sorted(
                    (
                        _segment(result.segment_set, segment_id)
                        for segment_id in owner.ordered_segment_ids
                    ),
                    key=lambda item: item.source_position,
                )
            ),
        )

    def test_nested_false_and_unresolved_are_evidence_not_parent_segments(
        self,
    ) -> None:
        fixture = _Fixture()
        first_table, second_table = fixture.table_ids
        artifact = _regions(
            fixture.source,
            regions=(
                _scope_region("owner", first_table, 0, 1, "scope-a"),
                D2NestedValueTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                    region_key="nested",
                    physical_table_id=first_table,
                    cell_spans=(_span(1, 2),),
                    nested_owner_region_key="owner",
                    reason_codes=("nested_value",),
                ),
                _false_region("layout", first_table, 2, 3),
                D2UnresolvedTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.UNRESOLVED,
                    region_key="uncertain",
                    physical_table_id=first_table,
                    cell_spans=(_span(3, 4),),
                    reason_codes=("semantic_boundary_unresolved",),
                ),
                _false_region("second-layout", second_table, 0, 4),
            ),
            scopes=(_scope_candidate("scope-a", ("owner",)),),
        )

        result = fixture.materialize(
            artifact,
            replacement_dispositions=(
                D2BaseTableReplacementDisposition.PRESERVED_AS_UNRESOLVED_LINEAGE,
                D2BaseTableReplacementDisposition.REPLACED_BY_REGION_TOPOLOGY,
            ),
        )
        segment_set = result.segment_set
        nested = next(
            item
            for item in segment_set.evidence_ledger
            if item.evidence_kind is D3EvidenceLedgerKindV1.NESTED_REGION
        )
        false = next(
            item
            for item in segment_set.evidence_ledger
            if item.evidence_kind is D3EvidenceLedgerKindV1.NON_BUSINESS_REGION
            and item.source_node_order == 1
        )
        unresolved = next(
            item
            for item in segment_set.evidence_ledger
            if item.evidence_kind is D3EvidenceLedgerKindV1.UNRESOLVED_REGION
        )

        self.assertEqual(segment_set.status, D3RegionStageInputStatus.PARTIAL)
        self.assertTrue(nested.selected_parent_assignment_eligible)
        self.assertIsNotNone(nested.ultimate_owner_logical_object_id)
        self.assertTrue(nested.owner_segment_ids)
        self.assertIsNone(false.ultimate_owner_logical_object_id)
        self.assertIsNone(unresolved.ultimate_owner_logical_object_id)
        primary_text = tuple(
            item.source_text
            for item in segment_set.segments
            if item.segment_role is D3UnifiedSegmentRoleV3.PRIMARY_EXACT
        )
        self.assertNotIn("table-1-cell-1", primary_text)
        self.assertNotIn("table-1-cell-2", primary_text)
        self.assertNotIn("table-1-cell-3", primary_text)
        self.assertIn(
            D3UnresolvedScopeKindV1.REGION,
            {item.scope_kind for item in segment_set.unresolved_scopes},
        )

    def test_cross_table_scope_nested_evidence_binds_only_its_owner_region(
        self,
    ) -> None:
        fixture = _Fixture()
        first_table, second_table = fixture.table_ids
        artifact = _regions(
            fixture.source,
            regions=(
                _scope_region("owner", first_table, 0, 2, "scope-a"),
                D2NestedValueTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                    region_key="nested",
                    physical_table_id=first_table,
                    cell_spans=(_span(2, 3),),
                    nested_owner_region_key="owner",
                    reason_codes=("nested_value",),
                ),
                _scope_region("first-tail", first_table, 3, 4, "scope-a"),
                _scope_region("second", second_table, 0, 4, "scope-a"),
            ),
            scopes=(
                _scope_candidate(
                    "scope-a",
                    ("owner", "first-tail", "second"),
                ),
            ),
        )
        prepared = fixture.prepare(artifact)

        result = prepared.materialize()

        source_nested = prepared.parent_input.nested_region_evidence[0]
        nested = next(
            item
            for item in result.segment_set.evidence_ledger
            if item.evidence_kind is D3EvidenceLedgerKindV1.NESTED_REGION
        )
        owner_region_segments = tuple(
            item.segment_id
            for item in result.segment_set.segments
            if item.region_parent_segment_id
            in set(source_nested.owner_source_segment_ids)
        )
        owner_object_segments = next(
            item
            for item in result.segment_set.logical_objects
            if item.logical_object_id == nested.ultimate_owner_logical_object_id
        ).primary_segment_ids

        self.assertEqual(
            nested.owner_segment_ids,
            source_nested.owner_source_segment_ids,
        )
        self.assertEqual(nested.owner_segment_ids, owner_region_segments)
        self.assertLess(len(nested.owner_segment_ids), len(owner_object_segments))

    def test_supplemental_dispositions_preserve_context_without_cell_duplication(
        self,
    ) -> None:
        source = _source_fixture()
        nodes = list(source.nodes)
        for index in (1, 2):
            node = nodes[index]
            assert node.table_payload is not None
            nodes[index] = node.model_copy(
                update={
                    "raw_text": "\n".join(
                        cell.raw_text
                        for cell in sorted(
                            node.table_payload.cells,
                            key=lambda item: (item.row, item.column, item.cell_id),
                        )
                    )
                }
            )
        fixture = _Fixture(source=source.model_copy(update={"nodes": tuple(nodes)}))
        first_table, second_table = fixture.table_ids
        artifact = _regions(
            fixture.source,
            regions=(
                _scope_region("first", first_table, 0, 4, "scope-a"),
                _scope_region("second", second_table, 0, 4, "scope-b"),
            ),
            scopes=(
                _scope_candidate("scope-a", ("first",)),
                _scope_candidate("scope-b", ("second",)),
            ),
        )
        scope_a_id = next(
            item.logical_scope_id
            for item in artifact.logical_scopes
            if item.candidate_scope_key == "scope-a"
        )
        placements = (
            _supplemental_placement(
                fixture,
                artifact,
                table_id=first_table,
                evidence_kind=D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT,
                disposition=D2TableSupplementalDispositionKindV1.CELL_DERIVED,
                derived=True,
            ),
            _supplemental_placement(
                fixture,
                artifact,
                table_id=first_table,
                evidence_kind=D2TableSupplementalEvidenceKindV1.TABLE_NOTES,
                disposition=D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE,
                owner_scope_id=scope_a_id,
            ),
            _supplemental_placement(
                fixture,
                artifact,
                table_id=first_table,
                evidence_kind=D2TableSupplementalEvidenceKindV1.TABLE_VISUAL,
                disposition=D2TableSupplementalDispositionKindV1.NON_BUSINESS,
            ),
            _supplemental_placement(
                fixture,
                artifact,
                table_id=second_table,
                evidence_kind=D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT,
                disposition=D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT,
                owner_object_id=fixture.text_object.logical_object_id,
            ),
            _supplemental_placement(
                fixture,
                artifact,
                table_id=second_table,
                evidence_kind=D2TableSupplementalEvidenceKindV1.TABLE_NOTES,
                disposition=D2TableSupplementalDispositionKindV1.NON_BUSINESS,
            ),
            _supplemental_placement(
                fixture,
                artifact,
                table_id=second_table,
                evidence_kind=D2TableSupplementalEvidenceKindV1.TABLE_VISUAL,
                disposition=D2TableSupplementalDispositionKindV1.UNRESOLVED,
            ),
        )

        result = fixture.materialize(
            artifact,
            supplemental_placements=placements,
        )
        segment_set = result.segment_set
        derived = next(
            item
            for item in segment_set.evidence_ledger
            if item.disposition
            == D2TableSupplementalDispositionKindV1.CELL_DERIVED.value
        )
        context = tuple(
            item
            for item in segment_set.segments
            if item.segment_role is D3UnifiedSegmentRoleV3.OWNED_CONTEXT
        )

        self.assertFalse(
            any(
                item.supplemental_evidence_id == derived.evidence_id for item in context
            )
        )
        self.assertEqual(len(derived.derived_cell_ids), 4)
        self.assertTrue(
            any(
                item.evidence_kind is D3UnifiedEvidenceKindV3.TABLE_NOTE
                for item in context
            )
        )
        self.assertTrue(
            any(
                item.evidence_kind is D3UnifiedEvidenceKindV3.TABLE_NODE_RAW_TEXT
                for item in context
            )
        )
        visual = tuple(
            item
            for item in segment_set.evidence_ledger
            if item.evidence_kind is D3EvidenceLedgerKindV1.TABLE_SUPPLEMENTAL
            and item.disposition
            in {
                D2TableSupplementalDispositionKindV1.NON_BUSINESS.value,
                D2TableSupplementalDispositionKindV1.UNRESOLVED.value,
            }
            and item.evidence_refs
        )
        self.assertTrue(visual)
        self.assertEqual(segment_set.status, D3RegionStageInputStatus.PARTIAL)
        self.assertIn(
            D3UnresolvedScopeKindV1.SUPPLEMENTAL,
            {item.scope_kind for item in segment_set.unresolved_scopes},
        )

    def test_owned_visual_without_selectable_owner_segments_becomes_local_unresolved(
        self,
    ) -> None:
        fixture = _Fixture()
        empty_owner_id = _add_empty_context_owner(fixture)
        artifact = fixture.complete_scope_artifact()
        placements = _empty_owner_supplemental_placements(
            fixture,
            artifact,
            owner_id=empty_owner_id,
            raw_text_owned=False,
            visual_owned=True,
        )
        prepared = fixture.prepare(
            artifact,
            supplemental_placements=placements,
        )

        result = prepared.materialize()

        visual_ref = f"table-visual:{fixture.table_ids[0]}"
        visual = next(
            item
            for item in result.segment_set.evidence_ledger
            if visual_ref in item.evidence_refs
        )
        unresolved = next(
            item
            for item in result.segment_set.unresolved_scopes
            if visual.evidence_id in item.evidence_ids
        )

        self.assertEqual(
            visual.disposition,
            D2TableSupplementalDispositionKindV1.UNRESOLVED.value,
        )
        self.assertIsNone(visual.ultimate_owner_logical_object_id)
        self.assertEqual(visual.owner_segment_ids, ())
        self.assertIn(visual_ref, visual.evidence_refs)
        self.assertEqual(
            unresolved.scope_kind,
            D3UnresolvedScopeKindV1.SUPPLEMENTAL,
        )
        self.assertEqual(
            unresolved.reason_codes,
            ("supplemental_owner_has_no_selectable_segments",),
        )
        self.assertEqual(result.segment_set.status, D3RegionStageInputStatus.PARTIAL)

        tampered = visual.model_copy(
            update={
                "ultimate_owner_logical_object_id": empty_owner_id,
                "owner_segment_ids": (),
            }
        )
        with self.assertRaisesRegex(ValidationError, "ultimate owner segments"):
            D3UnifiedExactSourceSegmentSetV3.model_validate(
                result.segment_set.model_copy(
                    update={
                        "evidence_ledger": tuple(
                            tampered if item.evidence_id == visual.evidence_id else item
                            for item in result.segment_set.evidence_ledger
                        )
                    }
                ).model_dump(mode="json")
            )

    def test_owned_text_context_makes_empty_container_selectable(self) -> None:
        fixture = _Fixture()
        empty_owner_id = _add_empty_context_owner(fixture)
        artifact = fixture.complete_scope_artifact()
        placements = _empty_owner_supplemental_placements(
            fixture,
            artifact,
            owner_id=empty_owner_id,
            raw_text_owned=True,
            visual_owned=True,
        )
        prepared = fixture.prepare(
            artifact,
            supplemental_placements=placements,
        )

        result = prepared.materialize()

        raw_text = next(
            item
            for item in result.segment_set.evidence_ledger
            if item.evidence_id
            == next(
                placement.evidence_id
                for placement in prepared.proposal.physical_table_evidence[
                    0
                ].supplemental_dispositions
                if placement.evidence_kind
                is D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT
            )
        )
        visual = next(
            item
            for item in result.segment_set.evidence_ledger
            if f"table-visual:{fixture.table_ids[0]}" in item.evidence_refs
        )
        self.assertEqual(
            raw_text.disposition,
            D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT.value,
        )
        self.assertEqual(raw_text.ultimate_owner_logical_object_id, empty_owner_id)
        self.assertTrue(raw_text.owner_segment_ids)
        self.assertTrue(
            any(
                item.supplemental_evidence_id == raw_text.evidence_id
                for item in result.segment_set.segments
            )
        )
        self.assertFalse(
            any(
                raw_text.evidence_id in scope.evidence_ids
                for scope in result.segment_set.unresolved_scopes
            )
        )
        self.assertEqual(
            visual.disposition,
            D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT.value,
        )
        self.assertEqual(visual.ultimate_owner_logical_object_id, empty_owner_id)
        self.assertEqual(visual.owner_segment_ids, raw_text.owner_segment_ids)
        self.assertEqual(result.segment_set.status, D3RegionStageInputStatus.COMPLETE)

    def test_source_kit_parent_receipt_and_duplicate_evidence_drift_fail_closed(
        self,
    ) -> None:
        fixture = _Fixture()
        artifact = fixture.complete_scope_artifact()
        prepared = fixture.prepare(artifact)

        changed_node = fixture.source.nodes[0].model_copy(
            update={"raw_text": "changed source"}
        )
        changed_source = fixture.source.model_copy(
            update={"nodes": (changed_node, *fixture.source.nodes[1:])}
        )
        with self.assertRaisesRegex(D3RegionStageInputError, "SOURCE_DRIFT"):
            prepared.materialize(source=changed_source)

        table_node = fixture.source.nodes[1]
        assert table_node.table_payload is not None
        changed_cells = list(table_node.table_payload.cells)
        changed_cells[0] = changed_cells[0].model_copy(
            update={"raw_text": "tampered physical cell"}
        )
        changed_table_node = table_node.model_copy(
            update={
                "table_payload": table_node.table_payload.model_copy(
                    update={"cells": tuple(changed_cells)}
                )
            }
        )
        changed_table_source = fixture.source.model_copy(
            update={
                "nodes": (
                    fixture.source.nodes[0],
                    changed_table_node,
                    *fixture.source.nodes[2:],
                )
            }
        )
        with self.assertRaisesRegex(D3RegionStageInputError, "SOURCE_DRIFT"):
            prepared.materialize(source=changed_table_source)

        first_partition = prepared.parent_input.physical_partitions[0]
        tampered_partition = first_partition.model_copy(
            update={"table_ir_sha256": "e" * 64}
        )
        tampered_parent_input = prepared.parent_input.model_copy(
            update={
                "physical_partitions": (
                    tampered_partition,
                    *prepared.parent_input.physical_partitions[1:],
                )
            }
        )
        with self.assertRaisesRegex(D3RegionStageInputError, "PREDECESSOR_INVALID"):
            prepared.materialize(parent_input=tampered_parent_input)

        drifted_lineage = prepared.lineage.model_copy(
            update={"kit_bundle_sha256": "f" * 64}
        )
        with self.assertRaisesRegex(D3RegionStageInputError, "PREDECESSOR_INVALID"):
            prepared.materialize(lineage=drifted_lineage)

        first_id = prepared.parent_input.source_segments[0].segment_id
        duplicate_receipt = prepared.parent_receipt.model_copy(
            update={"source_segment_ids": (first_id, first_id)}
        )
        with self.assertRaisesRegex(D3RegionStageInputError, "PREDECESSOR_INVALID"):
            prepared.materialize(parent_receipt=duplicate_receipt)

    def test_repeatability_and_content_free_manifest_tamper_detection(self) -> None:
        fixture = _Fixture()
        artifact = fixture.complete_scope_artifact()
        prepared = fixture.prepare(artifact)

        first = prepared.materialize()
        second = prepared.materialize()

        self.assertEqual(first, second)
        manifest_json = json.dumps(first.manifest.model_dump(mode="json"))
        self.assertTrue(first.manifest.content_free)
        self.assertNotIn("A source-faithful provision", manifest_json)
        self.assertNotIn("table-1-cell", manifest_json)
        self.assertNotIn("note-1", manifest_json)

        payload = first.segment_set.model_dump(mode="json")
        payload["segments"][0]["source_text"] = "tampered"
        with self.assertRaisesRegex(ValidationError, "text SHA mismatch"):
            D3UnifiedExactSourceSegmentSetV3.model_validate(payload)


class _Prepared:
    def __init__(
        self,
        *,
        source,
        proposal,
        receipt,
        parent_input,
        parent_receipt,
        lineage,
    ) -> None:
        self.source = source
        self.proposal = proposal
        self.receipt = receipt
        self.parent_input = parent_input
        self.parent_receipt = parent_receipt
        self.lineage = lineage

    def materialize(
        self,
        *,
        source=None,
        lineage=None,
        parent_input=None,
        parent_receipt=None,
    ):
        return materialize_d3_region_stage_input(
            self.source if source is None else source,
            proposal=self.proposal,
            validation_receipt=self.receipt,
            parent_input=self.parent_input if parent_input is None else parent_input,
            parent_input_receipt=(
                self.parent_receipt if parent_receipt is None else parent_receipt
            ),
            expected_lineage=self.lineage if lineage is None else lineage,
        )


class _Fixture:
    def __init__(self, *, source=None) -> None:
        self.source = _source_fixture() if source is None else source
        self.kit = _kit_identity()
        self.d1 = _d1_identity(self.kit)
        self.table_map = AgentD1TableMapArtifactIdentityV1(
            source_sha256=self.source.source_sha256,
            artifact_id="d1-table-map-" + "4" * 32,
            artifact_sha256="5" * 64,
        )
        self.base, self.base_receipt = _base_proposal_and_receipt(
            self.source,
            self.kit,
            self.d1,
            self.table_map,
        )
        self.text_object = next(
            item
            for item in self.base.logical_objects
            if item.object_kind is not D2LogicalObjectKind.TABLE
        )
        self.base_table_objects = tuple(
            item
            for item in self.base.logical_objects
            if item.object_kind is D2LogicalObjectKind.TABLE
        )
        self.table_ids = tuple(
            node.table_payload.table_id
            for node in self.source.nodes
            if node.kind is SourceNodeKind.TABLE and node.table_payload is not None
        )

    def complete_scope_artifact(self):
        first_table, second_table = self.table_ids
        return _regions(
            self.source,
            regions=(
                _scope_region("first", first_table, 0, 4, "scope-a"),
                _scope_region("second", second_table, 0, 4, "scope-b"),
            ),
            scopes=(
                _scope_candidate("scope-a", ("first",)),
                _scope_candidate("scope-b", ("second",)),
            ),
        )

    def materialize(self, artifact, **kwargs):
        return self.prepare(artifact, **kwargs).materialize()

    def prepare(
        self,
        artifact,
        *,
        object_placements=(),
        replacement_dispositions=None,
        supplemental_placements=None,
    ) -> _Prepared:
        if supplemental_placements is None:
            supplemental_placements = (
                build_d2_table_supplemental_object_owner_placements(
                    self.source,
                    artifact,
                    owner_reference_kind=(
                        D2SupplementalObjectOwnerReferenceKindV1.RETAINED_BASE_OBJECT
                    ),
                    owner_reference=self.text_object.logical_object_id,
                    reason_codes=("bind_table_supplemental_evidence",),
                )
            )
        dispositions = replacement_dispositions or tuple(
            D2BaseTableReplacementDisposition.REPLACED_BY_REGION_TOPOLOGY
            for _ in self.base_table_objects
        )
        retained = tuple(
            D2RetainedBaseObjectPlacementV1(
                base_logical_object_id=item.logical_object_id,
                parent_logical_object_id=item.parent_logical_object_id,
                reason_codes=("retain_base_object",),
            )
            for item in self.base.logical_objects
            if item.object_kind is not D2LogicalObjectKind.TABLE
        )
        replacements = tuple(
            D2BaseTableReplacementCandidateV1(
                base_logical_object_id=item.logical_object_id,
                disposition=disposition,
                reason_codes=("replace_whole_table_with_regions",),
            )
            for item, disposition in zip(
                self.base_table_objects,
                dispositions,
                strict=True,
            )
        )
        scope_placements = tuple(
            D2RegionLogicalScopePlacementV1(
                logical_scope_id=item.logical_scope_id,
                parent_logical_object_id=self.text_object.logical_object_id,
                reason_codes=("place_region_scope",),
            )
            for item in artifact.logical_scopes
        )
        plan = build_d2_region_placement_plan(
            base_proposal=self.base,
            base_validation_receipt=self.base_receipt,
            table_region_artifact=artifact,
            retained_base_objects=retained,
            base_table_replacements=replacements,
            region_logical_scope_placements=scope_placements,
            region_logical_object_placements=object_placements,
            supplemental_evidence_placements=supplemental_placements,
        )
        compiled = compile_d2_logical_structure_v2(
            self.source,
            base_proposal=self.base,
            base_validation_receipt=self.base_receipt,
            table_region_artifact=artifact,
            placement_plan=plan,
            expected_kit_identity=self.kit,
        )
        parent = build_d2_table_region_parent_input(
            self.source,
            artifact,
            expected_kit_bundle_sha256=self.kit.bundle_sha256,
            expected_d1_table_map_artifact_id=self.table_map.artifact_id,
            expected_d1_table_map_artifact_sha256=self.table_map.artifact_sha256,
            expected_region_artifact_id=artifact.artifact_id,
            expected_region_artifact_sha256=artifact.artifact_sha256,
        )
        lineage = build_d3_region_stage_input_lineage(
            proposal=compiled.proposal,
            validation_receipt=compiled.validation_receipt,
            parent_input=parent.stage_input,
            parent_input_receipt=parent.receipt,
        )
        return _Prepared(
            source=self.source,
            proposal=compiled.proposal,
            receipt=compiled.validation_receipt,
            parent_input=parent.stage_input,
            parent_receipt=parent.receipt,
            lineage=lineage,
        )


def _base_source_ref(
    *,
    source,
    node: SourceNode,
    handle_kind: D2SourceHandleKind,
    source_handle: str,
    source_content_sha256: str,
    page_numbers: tuple[int, ...],
) -> D2V2BaseSourceSegmentRefV1:
    base_id = stable_d2_source_segment_id(
        source_sha256=source.source_sha256,
        handle_kind=handle_kind,
        source_handle=source_handle,
        start_offset=None,
        end_offset=None,
    )
    base_sha = stable_d2_source_segment_sha256(
        segment_id=base_id,
        source_sha256=source.source_sha256,
        handle_kind=handle_kind,
        source_handle=source_handle,
        owner_source_node_id=node.source_node_id,
        source_order=node.source_order,
        page_numbers=page_numbers,
        start_offset=None,
        end_offset=None,
        source_content_sha256=source_content_sha256,
    )
    segment_id = stable_d2_v2_source_segment_id(
        segment_kind=D2V2SourceSegmentKind.BASE_SOURCE_SEGMENT,
        source_ir_id=source.source_ir_id,
        source_identity=base_id,
        source_identity_sha256=base_sha,
    )
    values = {
        "segment_id": segment_id,
        "source_ir_id": source.source_ir_id,
        "source_sha256": source.source_sha256,
        "source_order": node.source_order,
        "page_numbers": page_numbers,
        "base_source_segment_id": base_id,
        "base_source_segment_sha256": base_sha,
        "handle_kind": handle_kind,
        "source_handle": source_handle,
        "owner_source_node_id": node.source_node_id,
        "start_offset": None,
        "end_offset": None,
    }
    draft = D2V2BaseSourceSegmentRefV1.model_construct(
        **values,
        segment_sha256="0" * 64,
    )
    return D2V2BaseSourceSegmentRefV1(
        **values,
        segment_sha256=stable_d2_v2_source_segment_sha256(draft),
    )


def _supplemental_placement(
    fixture: _Fixture,
    artifact,
    *,
    table_id: str,
    evidence_kind: D2TableSupplementalEvidenceKindV1,
    disposition: D2TableSupplementalDispositionKindV1,
    owner_scope_id: str | None = None,
    owner_object_id: str | None = None,
    derived: bool = False,
) -> D2TableSupplementalEvidencePlacementV1:
    node = next(
        item
        for item in fixture.source.nodes
        if item.table_payload is not None and item.table_payload.table_id == table_id
    )
    assert node.table_payload is not None
    coverage = next(
        item
        for item in artifact.physical_table_cell_coverage
        if item.physical_table_id == table_id
    )
    if evidence_kind is D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT:
        evidence_ref = f"table-node-raw-text:{table_id}"
        evidence_sha = hashlib.sha256(node.raw_text.encode()).hexdigest()
    elif evidence_kind is D2TableSupplementalEvidenceKindV1.TABLE_NOTES:
        evidence_ref = f"table-notes:{table_id}"
        evidence_sha = _canonical_sha(list(node.table_payload.notes))
    else:
        evidence_ref = f"table-visual:{table_id}"
        evidence_sha = _canonical_sha(
            [
                item.model_dump(mode="json")
                for item in coverage.supplemental_visual_evidence
            ]
        )
    values = {
        "physical_table_id": table_id,
        "evidence_kind": evidence_kind,
        "evidence_ref": evidence_ref,
        "evidence_sha256": evidence_sha,
        "disposition": disposition,
        "reason_codes": ("test_supplemental_placement",),
    }
    if owner_scope_id is not None:
        values["owner_region_logical_scope_id"] = owner_scope_id
    if owner_object_id is not None:
        values["owner_reference_kind"] = (
            D2SupplementalObjectOwnerReferenceKindV1.RETAINED_BASE_OBJECT
        )
        values["owner_reference"] = owner_object_id
    if derived:
        values["cell_derivation"] = (
            D2TableRawTextCellDerivationV1.ORDERED_CELL_RAW_TEXT_JOIN_LF_V1
        )
        values["derived_cell_ids"] = tuple(
            cell.cell_id
            for cell in sorted(
                node.table_payload.cells,
                key=lambda item: (item.row, item.column, item.cell_id),
            )
        )
    return D2TableSupplementalEvidencePlacementV1(**values)


def _add_empty_context_owner(fixture: _Fixture) -> str:
    child = fixture.text_object
    descendant_ids = tuple(item.segment_id for item in child.source_segments)
    owner_id = stable_d2_derived_container_id(
        source_sha256=fixture.source.source_sha256,
        object_kind=D2LogicalObjectKind.CONTEXT,
        descendant_source_segment_ids=descendant_ids,
        descendant_logical_table_scope_ids=(),
        parent_logical_object_id=None,
    )
    owner_values = {
        "logical_object_id": owner_id,
        "object_kind": D2LogicalObjectKind.CONTEXT,
        "parent_logical_object_id": None,
        "source_order": child.source_order,
        "page_numbers": child.page_numbers,
        "source_segments": (),
        "logical_table_scope_ids": (),
        "reason_codes": ("derived_container_child_evidence",),
    }
    owner = D2LogicalObjectV1(
        **owner_values,
        object_sha256=stable_d2_logical_object_sha256(**owner_values),
    )
    child_values = {
        name: getattr(child, name)
        for name in D2LogicalObjectV1.model_fields
        if name != "object_sha256"
    }
    child_values["parent_logical_object_id"] = owner_id
    bound_child = D2LogicalObjectV1(
        **child_values,
        object_sha256=stable_d2_logical_object_sha256(**child_values),
    )
    logical_objects = tuple(
        sorted(
            (
                owner,
                *(
                    bound_child
                    if item.logical_object_id == child.logical_object_id
                    else item
                    for item in fixture.base.logical_objects
                ),
            ),
            key=lambda item: (item.source_order, item.logical_object_id),
        )
    )
    proposal_values = {
        name: getattr(fixture.base, name)
        for name in D2LogicalStructureProposalV1.model_fields
        if name != "proposal_sha256"
    }
    proposal_values["logical_objects"] = logical_objects
    draft = D2LogicalStructureProposalV1.model_construct(
        **proposal_values,
        proposal_sha256="0" * 64,
    )
    proposal = D2LogicalStructureProposalV1(
        **proposal_values,
        proposal_sha256=stable_d2_logical_structure_proposal_sha256(draft),
    )
    receipt = validate_d2_logical_structure_proposal(
        fixture.source,
        proposal,
        expected_d1_artifact=fixture.d1,
        expected_d1_table_map_artifact=fixture.table_map,
        expected_kit_identity=fixture.kit,
    )
    fixture.base = proposal
    fixture.base_receipt = receipt
    fixture.text_object = bound_child
    return owner_id


def _empty_owner_supplemental_placements(
    fixture: _Fixture,
    artifact,
    *,
    owner_id: str,
    raw_text_owned: bool,
    visual_owned: bool,
) -> tuple[D2TableSupplementalEvidencePlacementV1, ...]:
    first_table, second_table = fixture.table_ids

    def placement(
        table_id: str,
        kind: D2TableSupplementalEvidenceKindV1,
        *,
        owned: bool = False,
    ) -> D2TableSupplementalEvidencePlacementV1:
        return _supplemental_placement(
            fixture,
            artifact,
            table_id=table_id,
            evidence_kind=kind,
            disposition=(
                D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT
                if owned
                else D2TableSupplementalDispositionKindV1.NON_BUSINESS
            ),
            owner_object_id=owner_id if owned else None,
        )

    return (
        placement(
            first_table,
            D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT,
            owned=raw_text_owned,
        ),
        placement(first_table, D2TableSupplementalEvidenceKindV1.TABLE_NOTES),
        placement(
            first_table,
            D2TableSupplementalEvidenceKindV1.TABLE_VISUAL,
            owned=visual_owned,
        ),
        placement(second_table, D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT),
        placement(second_table, D2TableSupplementalEvidenceKindV1.TABLE_NOTES),
        placement(second_table, D2TableSupplementalEvidenceKindV1.TABLE_VISUAL),
    )


def _object_scope(segment_set, object_id):
    return next(
        item
        for item in segment_set.logical_objects
        if item.logical_object_id == object_id
    )


def _segment(segment_set, segment_id):
    return next(item for item in segment_set.segments if item.segment_id == segment_id)


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


if __name__ == "__main__":
    unittest.main()
