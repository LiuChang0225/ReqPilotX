from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.application.agent_workflow.d2_decision_candidate import (
    D2CandidateSourceRangeV1,
)
from app.application.agent_workflow.d2_decision_candidate_v3 import (
    D2LogicalStructureDecisionCandidateV3,
    D2RegionLogicalObjectPlacementDecisionCandidateV3,
    D2RegionLogicalScopePlacementDecisionCandidateV3,
    D2TableRegionDecisionCandidateV3,
    D2TableSupplementalEvidencePlacementDecisionCandidateV3,
    D2UnifiedLogicalObjectDecisionCandidateV3,
    compile_and_validate_d2_logical_structure_candidate_v3,
)
from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalStructureStatus,
    D2SourceHandleKind,
    D2ValidatedD1ArtifactIdentityV1,
)
from app.application.agent_workflow.d2_logical_structure_v2 import (
    D2BaseTableReplacementDisposition,
    D2TableSupplementalDispositionKindV1,
    D2TableSupplementalEvidenceKindV1,
    D2V2LogicalObjectOrigin,
)
from app.application.agent_workflow.d2_table_regions import (
    D2LogicalObjectPartTableRegionCandidateV1,
    D2LogicalScopePartTableRegionCandidateV1,
    D2NestedValueTableRegionCandidateV1,
    D2NonBusinessFalseTableRegionCandidateV1,
    D2PhysicalTableCellOrdinalSpanV1,
    D2TableLogicalScopeCandidateV1,
    D2TableRegionDispositionKind,
    D2UnresolvedTableRegionCandidateV1,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentKitIdentityV1,
    AgentStageArtifactStatus,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
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
RUN_ID = "agent-run-" + "1" * 32
KIT_SHA = "3" * 64


class D2DecisionCandidateV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _source_fixture()
        self.kit = _kit_identity()
        self.d1 = _d1_identity(self.kit)
        self.table_map = AgentD1TableMapArtifactIdentityV1(
            source_sha256=SOURCE_SHA,
            artifact_id="d1-table-map-" + "4" * 32,
            artifact_sha256="5" * 64,
        )
        self.table_ids = tuple(
            node.table_payload.table_id
            for node in self.source.nodes
            if node.table_payload is not None
        )

    def test_same_physical_table_supports_two_scopes_and_replaces_placeholders(
        self,
    ) -> None:
        candidate = self._candidate(
            regions=(
                _scope_region("first-half", self.table_ids[0], 0, 2, "scope-a"),
                _scope_region("second-half", self.table_ids[0], 2, 4, "scope-b"),
                _false_region("layout-table", self.table_ids[1], 0, 4),
            ),
            scopes=(
                _scope("scope-a", ("first-half",)),
                _scope("scope-b", ("second-half",)),
            ),
        )

        compiled = self._compile(candidate)
        proposal = compiled.final_compilation.proposal

        self.assertEqual(proposal.status, D2LogicalStructureStatus.COMPLETE)
        scope_objects = tuple(
            item
            for item in proposal.logical_objects
            if item.origin is D2V2LogicalObjectOrigin.REGION_LOGICAL_SCOPE
        )
        self.assertEqual(len(scope_objects), 2)
        base_placeholders = {
            item.logical_object_id
            for item in compiled.base_compilation.proposal.logical_objects
            if item.object_kind is D2LogicalObjectKind.TABLE
        }
        self.assertEqual(len(base_placeholders), 2)
        self.assertTrue(
            base_placeholders.isdisjoint(
                item.logical_object_id for item in proposal.logical_objects
            )
        )
        self.assertEqual(
            {item.base_logical_object_id for item in proposal.base_table_replacements},
            base_placeholders,
        )
        self.assertTrue(
            all(
                len(scope.physical_table_ids) == 1
                for scope in compiled.base_compilation.proposal.logical_table_scopes
            )
        )
        self.assertEqual(
            len(compiled.region_parent_input.stage_input.source_segments),
            4,
        )

    def test_one_region_scope_continues_across_physical_tables(self) -> None:
        candidate = self._candidate(
            regions=(
                _scope_region("page-one", self.table_ids[0], 0, 4, "scope-a"),
                _scope_region("page-two", self.table_ids[1], 0, 4, "scope-a"),
            ),
            scopes=(_scope("scope-a", ("page-one", "page-two")),),
        )

        compiled = self._compile(candidate)
        scope_object = next(
            item
            for item in compiled.final_compilation.proposal.logical_objects
            if item.origin is D2V2LogicalObjectOrigin.REGION_LOGICAL_SCOPE
        )

        self.assertEqual(
            tuple(item.physical_table_id for item in scope_object.source_segments),
            self.table_ids,
        )
        self.assertEqual(
            tuple(
                item.replacement_logical_object_ids
                for item in compiled.final_compilation.proposal.base_table_replacements
            ),
            ((scope_object.logical_object_id,), (scope_object.logical_object_id,)),
        )
        self.assertEqual(
            len(
                compiled.region_parent_input.stage_input.logical_scopes[
                    0
                ].ordered_segment_ids
            ),
            8,
        )

    def test_table_frame_prose_merges_into_non_table_section(self) -> None:
        candidate = self._candidate(
            regions=(
                D2LogicalObjectPartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                    region_key="frame-prose",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(0, 4),),
                    logical_object_key="frame-prose-object",
                    reason_codes=("ordinary_content_inside_table_frame",),
                ),
                _false_region("layout-table", self.table_ids[1], 0, 4),
            ),
            object_placements=(
                D2RegionLogicalObjectPlacementDecisionCandidateV3(
                    target_object_key="section",
                    source_region_logical_object_keys=("frame-prose-object",),
                    reason_codes=("merge_table_frame_prose",),
                ),
            ),
        )

        compiled = self._compile(candidate)
        section = next(
            item
            for item in compiled.final_compilation.proposal.logical_objects
            if item.object_kind is D2LogicalObjectKind.SECTION
        )

        self.assertEqual(
            section.origin,
            D2V2LogicalObjectOrigin.RETAINED_BASE_WITH_REGION_CONTENT,
        )
        self.assertEqual(len(section.source_segments), 2)
        self.assertEqual(
            section.source_region_logical_object_ids,
            (compiled.table_region_artifact.logical_objects[0].logical_object_id,),
        )
        self.assertEqual(
            len(
                compiled.region_parent_input.stage_input.logical_objects[
                    0
                ].ordered_segment_ids
            ),
            4,
        )

    def test_nested_false_and_unresolved_regions_remain_explicit(self) -> None:
        candidate = self._candidate(
            regions=(
                _scope_region("owner", self.table_ids[0], 0, 1, "scope-a"),
                D2NestedValueTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                    region_key="nested",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(1, 2),),
                    nested_owner_region_key="owner",
                    reason_codes=("nested_value",),
                ),
                _false_region("layout", self.table_ids[0], 2, 3),
                D2UnresolvedTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.UNRESOLVED,
                    region_key="uncertain",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(3, 4),),
                    reason_codes=("ambiguous_region",),
                ),
                _false_region("layout-table", self.table_ids[1], 0, 4),
            ),
            scopes=(_scope("scope-a", ("owner",)),),
        )

        compiled = self._compile(candidate)
        proposal = compiled.final_compilation.proposal

        self.assertEqual(proposal.status, D2LogicalStructureStatus.PARTIAL)
        self.assertEqual(len(proposal.nested_region_ids), 1)
        self.assertEqual(len(proposal.false_region_ids), 2)
        self.assertEqual(len(proposal.unresolved_region_ids), 1)
        self.assertEqual(
            compiled.region_parent_input.stage_input.logical_scopes[
                0
            ].nested_region_ids,
            proposal.nested_region_ids,
        )
        self.assertEqual(
            proposal.base_table_replacements[0].disposition,
            D2BaseTableReplacementDisposition.PRESERVED_AS_UNRESOLVED_LINEAGE,
        )

    def test_missing_supplemental_is_partial_and_explicit_owner_is_complete(
        self,
    ) -> None:
        regions = (
            _scope_region("first", self.table_ids[0], 0, 4, "scope-a"),
            _scope_region("second", self.table_ids[1], 0, 4, "scope-b"),
        )
        scopes = (
            _scope("scope-a", ("first",)),
            _scope("scope-b", ("second",)),
        )
        missing = self._candidate(
            regions=regions,
            scopes=scopes,
            supplemental=(),
        )
        explicit = self._candidate(regions=regions, scopes=scopes)

        missing_result = self._compile(missing)
        explicit_result = self._compile(explicit)

        self.assertEqual(
            missing_result.final_compilation.proposal.status,
            D2LogicalStructureStatus.PARTIAL,
        )
        self.assertEqual(
            len(
                missing_result.final_compilation.proposal.unresolved_supplemental_evidence_ids
            ),
            6,
        )
        self.assertEqual(
            explicit_result.final_compilation.proposal.status,
            D2LogicalStructureStatus.COMPLETE,
        )
        self.assertFalse(
            explicit_result.final_compilation.proposal.unresolved_supplemental_evidence_ids
        )

    def test_unknown_key_cycle_and_lineage_drift_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unknown target key"):
            self._candidate(
                regions=(
                    D2LogicalObjectPartTableRegionCandidateV1(
                        disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                        region_key="frame-prose",
                        physical_table_id=self.table_ids[0],
                        cell_spans=(_span(0, 4),),
                        logical_object_key="frame-prose-object",
                        reason_codes=("ordinary_content_inside_table_frame",),
                    ),
                    _false_region("layout-table", self.table_ids[1], 0, 4),
                ),
                object_placements=(
                    D2RegionLogicalObjectPlacementDecisionCandidateV3(
                        target_object_key="missing",
                        source_region_logical_object_keys=("frame-prose-object",),
                        reason_codes=("invalid_target",),
                    ),
                ),
            )

        cyclic_objects = (
            _section_object(parent="context"),
            D2UnifiedLogicalObjectDecisionCandidateV3(
                object_key="context",
                object_kind=D2LogicalObjectKind.CONTEXT,
                parent_object_key="section",
                reason_codes=("context_container",),
            ),
        )
        with self.assertRaisesRegex(ValidationError, "contains a cycle"):
            D2LogicalStructureDecisionCandidateV3(
                logical_objects=cyclic_objects,
                table_regions=D2TableRegionDecisionCandidateV3(
                    regions=(
                        _false_region("first", self.table_ids[0], 0, 4),
                        _false_region("second", self.table_ids[1], 0, 4),
                    )
                ),
            )

        valid = self._candidate(
            regions=(
                _scope_region("first", self.table_ids[0], 0, 4, "scope-a"),
                _scope_region("second", self.table_ids[1], 0, 4, "scope-b"),
            ),
            scopes=(
                _scope("scope-a", ("first",)),
                _scope("scope-b", ("second",)),
            ),
        )
        drifted_kit = self.kit.model_copy(update={"bundle_sha256": "f" * 64})
        with self.assertRaisesRegex(ValueError, "lineage is inconsistent"):
            compile_and_validate_d2_logical_structure_candidate_v3(
                valid,
                source_ir=self.source,
                expected_d1_artifact=self.d1,
                expected_d1_table_map_artifact=self.table_map,
                expected_kit_identity=drifted_kit,
            )

    def test_wire_anyof_keeps_disposition_specific_fields_strict(self) -> None:
        candidate = self._candidate(
            regions=(
                _scope_region("first", self.table_ids[0], 0, 4, "scope-a"),
                _false_region("second", self.table_ids[1], 0, 4),
            ),
            scopes=(_scope("scope-a", ("first",)),),
        )
        payload = candidate.model_dump(mode="json")
        payload["table_regions"]["regions"][0]["disposition"] = (
            D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION.value
        )

        with self.assertRaises(ValidationError):
            D2LogicalStructureDecisionCandidateV3.model_validate(payload)

    def test_stable_replay_is_collection_order_independent(self) -> None:
        regions = (
            _scope_region("first", self.table_ids[0], 0, 4, "scope-a"),
            _scope_region("second", self.table_ids[1], 0, 4, "scope-b"),
        )
        scopes = (
            _scope("scope-a", ("first",)),
            _scope("scope-b", ("second",)),
        )
        canonical = self._candidate(regions=regions, scopes=scopes)
        shuffled = self._candidate(
            regions=tuple(reversed(regions)),
            scopes=tuple(reversed(scopes)),
            supplemental=tuple(reversed(_supplemental(self.source))),
        )

        first = self._compile(canonical)
        replay = self._compile(canonical)
        reordered = self._compile(shuffled)

        self.assertEqual(first.model_dump(mode="json"), replay.model_dump(mode="json"))
        self.assertEqual(
            first.final_compilation.proposal.proposal_sha256,
            reordered.final_compilation.proposal.proposal_sha256,
        )
        self.assertEqual(
            first.final_compilation.validation_receipt.receipt_sha256,
            reordered.final_compilation.validation_receipt.receipt_sha256,
        )

    def _candidate(
        self,
        *,
        regions: tuple[object, ...],
        scopes: tuple[D2TableLogicalScopeCandidateV1, ...] = (),
        object_placements: tuple[
            D2RegionLogicalObjectPlacementDecisionCandidateV3, ...
        ] = (),
        supplemental: tuple[
            D2TableSupplementalEvidencePlacementDecisionCandidateV3, ...
        ]
        | None = None,
    ) -> D2LogicalStructureDecisionCandidateV3:
        if supplemental is None:
            supplemental = _supplemental(self.source)
        return D2LogicalStructureDecisionCandidateV3(
            logical_objects=(_section_object(),),
            table_regions=D2TableRegionDecisionCandidateV3(
                regions=regions,
                logical_scopes=scopes,
            ),
            region_logical_scope_placements=tuple(
                D2RegionLogicalScopePlacementDecisionCandidateV3(
                    logical_scope_key=item.logical_scope_key,
                    parent_object_key="section",
                    reason_codes=("place_scope_under_section",),
                )
                for item in scopes
            ),
            region_logical_object_placements=object_placements,
            supplemental_evidence_placements=supplemental,
        )

    def _compile(self, candidate: D2LogicalStructureDecisionCandidateV3):
        return compile_and_validate_d2_logical_structure_candidate_v3(
            candidate,
            source_ir=self.source,
            expected_d1_artifact=self.d1,
            expected_d1_table_map_artifact=self.table_map,
            expected_kit_identity=self.kit,
        )


def _section_object(
    parent: str | None = None,
) -> D2UnifiedLogicalObjectDecisionCandidateV3:
    source = _source_fixture().nodes[0]
    return D2UnifiedLogicalObjectDecisionCandidateV3(
        object_key="section",
        object_kind=D2LogicalObjectKind.SECTION,
        parent_object_key=parent,
        source_ranges=(
            D2CandidateSourceRangeV1(
                handle_kind=D2SourceHandleKind.SOURCE_NODE,
                source_handle=source.source_node_id,
            ),
        ),
        reason_codes=("source_section",),
    )


def _supplemental(
    source: SourceIRSnapshot,
) -> tuple[D2TableSupplementalEvidencePlacementDecisionCandidateV3, ...]:
    return tuple(
        D2TableSupplementalEvidencePlacementDecisionCandidateV3(
            physical_table_id=node.table_payload.table_id,
            evidence_kind=evidence_kind,
            disposition=D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT,
            owner_object_key="section",
            reason_codes=("bind_supplemental_to_section",),
        )
        for node in source.nodes
        if node.table_payload is not None
        for evidence_kind in D2TableSupplementalEvidenceKindV1
    )


def _scope_region(
    key: str,
    table_id: str,
    start: int,
    end: int,
    scope_key: str,
) -> D2LogicalScopePartTableRegionCandidateV1:
    return D2LogicalScopePartTableRegionCandidateV1(
        disposition=D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
        region_key=key,
        physical_table_id=table_id,
        cell_spans=(_span(start, end),),
        logical_scope_key=scope_key,
        reason_codes=("logical_grid",),
    )


def _false_region(
    key: str,
    table_id: str,
    start: int,
    end: int,
) -> D2NonBusinessFalseTableRegionCandidateV1:
    return D2NonBusinessFalseTableRegionCandidateV1(
        disposition=D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION,
        region_key=key,
        physical_table_id=table_id,
        cell_spans=(_span(start, end),),
        reason_codes=("layout_only",),
    )


def _scope(
    key: str,
    region_keys: tuple[str, ...],
) -> D2TableLogicalScopeCandidateV1:
    return D2TableLogicalScopeCandidateV1(
        logical_scope_key=key,
        ordered_region_keys=region_keys,
        reason_codes=("same_logical_table",),
    )


def _span(start: int, end: int) -> D2PhysicalTableCellOrdinalSpanV1:
    return D2PhysicalTableCellOrdinalSpanV1(
        start_cell_ordinal=start,
        end_cell_ordinal_exclusive=end,
    )


def _kit_identity() -> AgentKitIdentityV1:
    return AgentKitIdentityV1(
        kit_id="reqpilotx-extraction",
        kit_version="0.7.0",
        manifest_sha256="2" * 64,
        bundle_sha256=KIT_SHA,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.0.0",
    )


def _d1_identity(kit: AgentKitIdentityV1) -> D2ValidatedD1ArtifactIdentityV1:
    return D2ValidatedD1ArtifactIdentityV1(
        artifact_id="agent-artifact-" + "6" * 32,
        artifact_sha256="7" * 64,
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA,
        status=AgentStageArtifactStatus.VALIDATED,
        proposal_sha256="8" * 64,
        evidence_sha256="9" * 64,
        validator_receipt_sha256="b" * 64,
        page_count=3,
        kit_bundle_sha256=kit.bundle_sha256,
    )


def _source_fixture() -> SourceIRSnapshot:
    adapter_id = "d2-v3-test"
    adapter_version = "1.0.0"
    text_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
    )
    nodes: list[SourceNode] = [
        SourceNode(
            source_node_id=stable_node_id(
                source_sha256=SOURCE_SHA,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                kind=SourceNodeKind.PARAGRAPH,
                native_locator=text_locator,
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=0,
            raw_text="A source-faithful provision.",
            native_locator=text_locator,
        )
    ]
    for table_index in range(2):
        page_number = table_index + 2
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=table_index,
            bbox=(10.0, 10.0, 200.0, 100.0),
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
                raw_text=f"table-{table_index + 1}-cell-{row * 2 + column}",
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=page_number,
                    table_index=table_index,
                    row_index=row,
                    column_index=column,
                ),
            )
            for row in range(2)
            for column in range(2)
        )
        table = TableIR(
            table_id=table_id,
            row_count=2,
            column_count=2,
            cells=cells,
            notes=(f"note-{table_index + 1}",),
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
                source_order=table_index + 1,
                raw_text=f"table render text {table_index + 1}",
                native_locator=locator,
                visual_locator=locator,
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
