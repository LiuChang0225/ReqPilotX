from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalObjectV1,
    D2LogicalStructureProposalV1,
    D2LogicalStructureStatus,
    D2PhysicalTableDispositionKind,
    D2PhysicalTableDispositionV1,
    D2SourceEvidenceDispositionKind,
    D2SourceEvidenceDispositionV1,
    D2SourceHandleKind,
    D2SourceSegmentV1,
    D2ValidatedD1ArtifactIdentityV1,
    stable_d2_logical_object_id,
    stable_d2_logical_object_sha256,
    stable_d2_logical_structure_proposal_sha256,
    stable_d2_source_segment_id,
    stable_d2_source_segment_sha256,
    stable_d2_source_disposition_id,
    stable_d2_source_disposition_sha256,
    stable_d2_table_disposition_sha256,
    validate_d2_logical_structure_proposal,
)
from app.application.agent_workflow.d2_logical_structure_v2 import (
    D2BaseTableReplacementCandidateV1,
    D2BaseTableReplacementDisposition,
    D2LogicalObjectV2,
    D2LogicalStructureProposalV2,
    D2RegionLogicalObjectPlacementMode,
    D2RegionLogicalObjectPlacementV1,
    D2RegionLogicalScopePlacementV1,
    D2RetainedBaseObjectPlacementV1,
    D2SupplementalObjectOwnerReferenceKindV1,
    D2V2LogicalObjectOrigin,
    D2V2TableRegionSourceSegmentRefV1,
    build_d2_region_placement_plan,
    build_d2_table_supplemental_object_owner_placements,
    compile_d2_logical_structure_v2,
    stable_d2_logical_structure_proposal_v2_sha256,
    stable_d2_v2_logical_object_sha256,
)
from app.application.agent_workflow.d2_table_regions import (
    D2EmptyPhysicalTableDispositionCandidateV1,
    D2EmptyPhysicalTableDispositionKind,
    D2LogicalObjectPartTableRegionCandidateV1,
    D2LogicalScopePartTableRegionCandidateV1,
    D2NestedValueTableRegionCandidateV1,
    D2NonBusinessFalseTableRegionCandidateV1,
    D2PhysicalTableCellOrdinalSpanV1,
    D2TableLogicalScopeCandidateV1,
    D2TableRegionArtifactV1,
    D2TableRegionCandidateArtifactV1,
    D2TableRegionDispositionKind,
    D2UnresolvedTableRegionCandidateV1,
    compile_d2_table_regions,
    d2_table_region_source_ir_canonical_sha256,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentKitIdentityV1,
    AgentStageArtifactStatus,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    AgentLogicalTableScopeV1,
    build_agent_logical_table_scope,
    source_ir_canonical_sha256,
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


class D2LogicalStructureV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _source_fixture()
        self.kit = _kit_identity()
        self.d1 = _d1_identity(self.kit)
        self.table_map = AgentD1TableMapArtifactIdentityV1(
            source_sha256=SOURCE_SHA,
            artifact_id="d1-table-map-" + "4" * 32,
            artifact_sha256="5" * 64,
        )
        self.table_nodes = tuple(
            node for node in self.source.nodes if node.kind is SourceNodeKind.TABLE
        )
        self.table_ids = tuple(
            node.table_payload.table_id
            for node in self.table_nodes
            if node.table_payload is not None
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

    def test_same_physical_table_can_replace_one_v1_table_with_two_scopes(self) -> None:
        artifact = _regions(
            self.source,
            regions=(
                _scope_region("first-half", self.table_ids[0], 0, 2, "scope-a"),
                _scope_region("second-half", self.table_ids[0], 2, 4, "scope-b"),
                _false_region("second-table-layout", self.table_ids[1], 0, 4),
            ),
            scopes=(
                _scope_candidate("scope-a", ("first-half",)),
                _scope_candidate("scope-b", ("second-half",)),
            ),
        )
        plan = self._plan(artifact)

        compiled = self._compile(artifact, plan)
        proposal = compiled.proposal

        self.assertEqual(proposal.status, D2LogicalStructureStatus.COMPLETE)
        self.assertEqual(
            {
                item.logical_object_id
                for item in proposal.logical_objects
                if item.origin is D2V2LogicalObjectOrigin.REGION_LOGICAL_SCOPE
            },
            {item.logical_scope_id for item in artifact.logical_scopes},
        )
        self.assertTrue(
            set(item.logical_object_id for item in self.base_table_objects).isdisjoint(
                item.logical_object_id for item in proposal.logical_objects
            )
        )
        first_replacement = proposal.base_table_replacements[0]
        self.assertEqual(len(first_replacement.replacement_logical_object_ids), 2)
        self.assertEqual(len(first_replacement.region_ids), 2)
        self.assertEqual(
            compiled.validation_receipt.artifact_sha256,
            proposal.proposal_sha256,
        )

    def test_one_region_scope_can_continue_across_two_base_table_objects(self) -> None:
        artifact = _regions(
            self.source,
            regions=(
                _scope_region("page-one", self.table_ids[0], 0, 4, "scope-a"),
                _scope_region("page-two", self.table_ids[1], 0, 4, "scope-a"),
            ),
            scopes=(_scope_candidate("scope-a", ("page-one", "page-two")),),
        )
        compiled = self._compile(artifact, self._plan(artifact))

        scope_object = next(
            item
            for item in compiled.proposal.logical_objects
            if item.origin is D2V2LogicalObjectOrigin.REGION_LOGICAL_SCOPE
        )
        self.assertEqual(len(scope_object.source_segments), 2)
        self.assertEqual(
            tuple(item.physical_table_id for item in scope_object.source_segments),
            self.table_ids,
        )
        self.assertEqual(
            tuple(
                item.replacement_logical_object_ids
                for item in compiled.proposal.base_table_replacements
            ),
            ((scope_object.logical_object_id,), (scope_object.logical_object_id,)),
        )

    def test_logical_object_part_explicitly_merges_into_retained_non_table(
        self,
    ) -> None:
        artifact = _regions(
            self.source,
            regions=(
                D2LogicalObjectPartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                    region_key="ordinary-prose",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(0, 4),),
                    logical_object_key="prose-a",
                    reason_codes=("ordinary_content_inside_table_frame",),
                ),
                _false_region("second-table-layout", self.table_ids[1], 0, 4),
            ),
        )
        region_object_id = artifact.logical_objects[0].logical_object_id
        placement = D2RegionLogicalObjectPlacementV1(
            placement_key="merge-prose",
            source_region_logical_object_ids=(region_object_id,),
            mode=D2RegionLogicalObjectPlacementMode.MERGE_RETAINED_BASE,
            target_base_logical_object_id=self.text_object.logical_object_id,
            parent_logical_object_id=None,
            reason_codes=("merge_with_source_paragraph",),
        )
        compiled = self._compile(
            artifact,
            self._plan(artifact, object_placements=(placement,)),
        )

        retained = next(
            item
            for item in compiled.proposal.logical_objects
            if item.logical_object_id == self.text_object.logical_object_id
        )
        self.assertEqual(
            retained.origin,
            D2V2LogicalObjectOrigin.RETAINED_BASE_WITH_REGION_CONTENT,
        )
        self.assertEqual(retained.source_region_logical_object_ids, (region_object_id,))
        self.assertEqual(len(retained.source_segments), 2)
        region_segment = next(
            item
            for item in retained.source_segments
            if isinstance(item, D2V2TableRegionSourceSegmentRefV1)
        )
        dumped = region_segment.model_dump(mode="json")
        self.assertNotIn("cell_refs", dumped)
        disposition = next(
            item
            for item in compiled.proposal.table_region_dispositions
            if item.region_id == region_segment.table_region_id
        )
        self.assertEqual(disposition.logical_object_id, retained.logical_object_id)

    def test_new_region_object_uses_a_v2_owned_stable_id(self) -> None:
        artifact = _regions(
            self.source,
            regions=(
                D2LogicalObjectPartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                    region_key="ordinary-prose",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(0, 4),),
                    logical_object_key="prose-a",
                    reason_codes=("ordinary_content_inside_table_frame",),
                ),
                _false_region("second-table-layout", self.table_ids[1], 0, 4),
            ),
        )
        source_region_object_id = artifact.logical_objects[0].logical_object_id
        placement = D2RegionLogicalObjectPlacementV1(
            placement_key="new-prose",
            source_region_logical_object_ids=(source_region_object_id,),
            mode=D2RegionLogicalObjectPlacementMode.NEW_REGION_OBJECT,
            object_kind=D2LogicalObjectKind.CONTEXT,
            parent_logical_object_id=self.text_object.logical_object_id,
            reason_codes=("ordinary_table_content",),
        )
        compiled = self._compile(
            artifact,
            self._plan(artifact, object_placements=(placement,)),
        )

        region_object = next(
            item
            for item in compiled.proposal.logical_objects
            if item.origin is D2V2LogicalObjectOrigin.REGION_LOGICAL_OBJECT
        )
        self.assertRegex(
            region_object.logical_object_id,
            r"^d2-v2-region-object-[0-9a-f]{32}$",
        )
        self.assertNotEqual(
            region_object.logical_object_id,
            source_region_object_id,
        )

    def test_nested_false_and_unresolved_regions_remain_explicit(self) -> None:
        artifact = _regions(
            self.source,
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
                    reason_codes=("semantic_boundary_unresolved",),
                ),
                _false_region("second-table-layout", self.table_ids[1], 0, 4),
            ),
            scopes=(_scope_candidate("scope-a", ("owner",)),),
        )
        compiled = self._compile(
            artifact,
            self._plan(
                artifact,
                replacement_dispositions=(
                    D2BaseTableReplacementDisposition.PRESERVED_AS_UNRESOLVED_LINEAGE,
                    D2BaseTableReplacementDisposition.REPLACED_BY_REGION_TOPOLOGY,
                ),
            ),
        )
        proposal = compiled.proposal

        self.assertEqual(proposal.status, D2LogicalStructureStatus.PARTIAL)
        self.assertEqual(len(proposal.nested_region_ids), 1)
        self.assertEqual(len(proposal.false_region_ids), 2)
        self.assertEqual(len(proposal.unresolved_region_ids), 1)
        owner = next(
            item
            for item in proposal.logical_objects
            if item.origin is D2V2LogicalObjectOrigin.REGION_LOGICAL_SCOPE
        )
        self.assertEqual(owner.nested_evidence_region_ids, proposal.nested_region_ids)
        nested_disposition = next(
            item
            for item in proposal.table_region_dispositions
            if item.region_id in proposal.nested_region_ids
        )
        self.assertIsNotNone(nested_disposition.nested_owner_region_id)

    def test_binds_raw_text_notes_visual_and_disposition_sensitive_identity(
        self,
    ) -> None:
        false_artifact = _regions(
            self.source,
            regions=(
                _false_region("first", self.table_ids[0], 0, 4),
                _false_region("second", self.table_ids[1], 0, 4),
            ),
        )
        unresolved_artifact = _regions(
            self.source,
            regions=(
                D2UnresolvedTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.UNRESOLVED,
                    region_key="first",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(0, 4),),
                    reason_codes=("semantic_boundary_unresolved",),
                ),
                _false_region("second", self.table_ids[1], 0, 4),
            ),
        )
        self.assertNotEqual(false_artifact.artifact_id, unresolved_artifact.artifact_id)
        self.assertNotEqual(
            false_artifact.artifact_sha256,
            unresolved_artifact.artifact_sha256,
        )

        complete = self._compile(false_artifact, self._plan(false_artifact))
        partial = self._compile(
            unresolved_artifact,
            self._plan(
                unresolved_artifact,
                replacement_dispositions=(
                    D2BaseTableReplacementDisposition.PRESERVED_AS_UNRESOLVED_LINEAGE,
                    D2BaseTableReplacementDisposition.REPLACED_BY_REGION_TOPOLOGY,
                ),
            ),
        )
        self.assertNotEqual(
            complete.validation_receipt.artifact_id,
            partial.validation_receipt.artifact_id,
        )
        evidence = complete.proposal.physical_table_evidence[0]
        self.assertEqual(
            evidence.raw_text_sha256,
            hashlib.sha256("table render text 1".encode()).hexdigest(),
        )
        self.assertRegex(evidence.notes_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(evidence.visual_residual_sha256, r"^[0-9a-f]{64}$")

    def test_present_supplemental_evidence_requires_owner_or_remains_partial(
        self,
    ) -> None:
        artifact = _regions(
            self.source,
            regions=(
                _false_region("first", self.table_ids[0], 0, 4),
                _false_region("second", self.table_ids[1], 0, 4),
            ),
        )

        unplaced = self._compile(
            artifact,
            self._plan(artifact, supplemental_placements=()),
        )
        owned = self._compile(artifact, self._plan(artifact))

        self.assertEqual(unplaced.proposal.status, D2LogicalStructureStatus.PARTIAL)
        self.assertEqual(
            len(unplaced.proposal.unresolved_supplemental_evidence_ids),
            6,
        )
        self.assertEqual(owned.proposal.status, D2LogicalStructureStatus.COMPLETE)
        self.assertEqual(owned.proposal.unresolved_supplemental_evidence_ids, ())
        owner = next(
            item
            for item in owned.proposal.logical_objects
            if item.logical_object_id == self.text_object.logical_object_id
        )
        self.assertEqual(len(owner.supplemental_evidence_ids), 6)

        placements = build_d2_table_supplemental_object_owner_placements(
            self.source,
            artifact,
            owner_reference_kind=(
                D2SupplementalObjectOwnerReferenceKindV1.RETAINED_BASE_OBJECT
            ),
            owner_reference=self.text_object.logical_object_id,
            reason_codes=("bind_table_supplemental_evidence",),
        )
        tampered = (
            placements[0].model_copy(update={"evidence_sha256": "f" * 64}),
            *placements[1:],
        )
        tampered_plan = self._plan(
            artifact,
            supplemental_placements=tampered,
        )
        with self.assertRaisesRegex(ValueError, "identity drifted"):
            self._compile(artifact, tampered_plan)

    def test_rejects_lineage_drift_duplicate_mapping_and_missing_replacement(
        self,
    ) -> None:
        artifact = _regions(
            self.source,
            regions=(
                D2LogicalObjectPartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                    region_key="ordinary-prose",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(0, 4),),
                    logical_object_key="prose-a",
                    reason_codes=("ordinary_content_inside_table_frame",),
                ),
                _false_region("second", self.table_ids[1], 0, 4),
            ),
        )
        source_object_id = artifact.logical_objects[0].logical_object_id
        first = D2RegionLogicalObjectPlacementV1(
            placement_key="first",
            source_region_logical_object_ids=(source_object_id,),
            mode=D2RegionLogicalObjectPlacementMode.NEW_REGION_OBJECT,
            object_kind=D2LogicalObjectKind.CONTEXT,
            parent_logical_object_id=None,
            reason_codes=("ordinary_table_content",),
        )
        second = first.model_copy(update={"placement_key": "second"})
        duplicate_plan = self._plan(
            artifact,
            object_placements=(first, second),
        )
        with self.assertRaisesRegex(ValueError, "explicit mapping"):
            self._compile(artifact, duplicate_plan)

        missing_replacement = build_d2_region_placement_plan(
            base_proposal=self.base,
            base_validation_receipt=self.base_receipt,
            table_region_artifact=artifact,
            retained_base_objects=self._retained(),
            base_table_replacements=(self._replacements(artifact)[0],),
            region_logical_object_placements=(first,),
        )
        with self.assertRaisesRegex(ValueError, "every base TABLE"):
            self._compile(artifact, missing_replacement)

        good_plan = self._plan(artifact, object_placements=(first,))
        drifted_receipt = self.base_receipt.model_copy(
            update={"proposal_sha256": "f" * 64}
        )
        with self.assertRaisesRegex(ValueError, "lineage drifted"):
            compile_d2_logical_structure_v2(
                self.source,
                base_proposal=self.base,
                base_validation_receipt=drifted_receipt,
                table_region_artifact=artifact,
                placement_plan=good_plan,
                expected_kit_identity=self.kit,
            )

    def test_rejects_duplicate_region_cell_evidence_across_v2_objects(self) -> None:
        artifact = _regions(
            self.source,
            regions=(
                _scope_region("first-half", self.table_ids[0], 0, 2, "scope-a"),
                _scope_region("second-half", self.table_ids[0], 2, 4, "scope-b"),
                _false_region("second-table-layout", self.table_ids[1], 0, 4),
            ),
            scopes=(
                _scope_candidate("scope-a", ("first-half",)),
                _scope_candidate("scope-b", ("second-half",)),
            ),
        )
        proposal = self._compile(artifact, self._plan(artifact)).proposal
        retained = next(
            item
            for item in proposal.logical_objects
            if item.logical_object_id == self.text_object.logical_object_id
        )
        scope = next(
            item
            for item in proposal.logical_objects
            if item.origin is D2V2LogicalObjectOrigin.REGION_LOGICAL_SCOPE
        )
        duplicate_segment = scope.source_segments[0]
        retained_values = {
            field_name: getattr(retained, field_name)
            for field_name in D2LogicalObjectV2.model_fields
            if field_name != "object_sha256"
        }
        retained_values.update(
            origin=D2V2LogicalObjectOrigin.RETAINED_BASE_WITH_REGION_CONTENT,
            source_segments=(*retained.source_segments, duplicate_segment),
            source_region_logical_object_ids=("d2-table-logical-object-" + "e" * 32,),
        )
        retained_draft = D2LogicalObjectV2.model_construct(
            **retained_values,
            object_sha256="0" * 64,
        )
        duplicated_owner = D2LogicalObjectV2(
            **retained_values,
            object_sha256=stable_d2_v2_logical_object_sha256(retained_draft),
        )
        objects = tuple(
            duplicated_owner
            if item.logical_object_id == retained.logical_object_id
            else item
            for item in proposal.logical_objects
        )
        proposal_values = {
            field_name: getattr(proposal, field_name)
            for field_name in D2LogicalStructureProposalV2.model_fields
            if field_name not in {"proposal_sha256", "logical_objects"}
        }
        proposal_draft = D2LogicalStructureProposalV2.model_construct(
            **proposal_values,
            logical_objects=objects,
            proposal_sha256="0" * 64,
        )
        with self.assertRaisesRegex(ValidationError, "source segment ownership"):
            D2LogicalStructureProposalV2(
                **proposal_values,
                logical_objects=objects,
                proposal_sha256=(
                    stable_d2_logical_structure_proposal_v2_sha256(proposal_draft)
                ),
            )

    def test_stable_replay_canonicalizes_placement_collection_order(self) -> None:
        artifact = _regions(
            self.source,
            regions=(
                _scope_region("first", self.table_ids[0], 0, 4, "scope-a"),
                _scope_region("second", self.table_ids[1], 0, 4, "scope-b"),
            ),
            scopes=(
                _scope_candidate("scope-a", ("first",)),
                _scope_candidate("scope-b", ("second",)),
            ),
        )
        canonical = self._plan(artifact)
        shuffled = build_d2_region_placement_plan(
            base_proposal=self.base,
            base_validation_receipt=self.base_receipt,
            table_region_artifact=artifact,
            retained_base_objects=tuple(reversed(self._retained())),
            base_table_replacements=tuple(reversed(self._replacements(artifact))),
            region_logical_scope_placements=tuple(
                reversed(self._scope_placements(artifact))
            ),
            supplemental_evidence_placements=tuple(
                reversed(
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
            ),
        )

        first = self._compile(artifact, canonical)
        second = self._compile(artifact, shuffled)

        self.assertEqual(canonical.plan_sha256, shuffled.plan_sha256)
        self.assertEqual(first.proposal, second.proposal)
        self.assertEqual(first.validation_receipt, second.validation_receipt)

    def test_validated_partial_base_forces_partial_successor(self) -> None:
        base_values = {
            field_name: getattr(self.base, field_name)
            for field_name in D2LogicalStructureProposalV1.model_fields
            if field_name not in {"status", "unresolved_codes", "proposal_sha256"}
        }
        base_draft = D2LogicalStructureProposalV1.model_construct(
            **base_values,
            status=D2LogicalStructureStatus.PARTIAL,
            unresolved_codes=("base_structure_uncertainty",),
            proposal_sha256="0" * 64,
        )
        partial_base = D2LogicalStructureProposalV1(
            **base_values,
            status=D2LogicalStructureStatus.PARTIAL,
            unresolved_codes=("base_structure_uncertainty",),
            proposal_sha256=stable_d2_logical_structure_proposal_sha256(base_draft),
        )
        partial_receipt = validate_d2_logical_structure_proposal(
            self.source,
            partial_base,
            expected_d1_artifact=self.d1,
            expected_d1_table_map_artifact=self.table_map,
            expected_kit_identity=self.kit,
        )
        artifact = _regions(
            self.source,
            regions=(
                _false_region("first", self.table_ids[0], 0, 4),
                _false_region("second", self.table_ids[1], 0, 4),
            ),
        )
        plan = build_d2_region_placement_plan(
            base_proposal=partial_base,
            base_validation_receipt=partial_receipt,
            table_region_artifact=artifact,
            retained_base_objects=self._retained(),
            base_table_replacements=self._replacements(artifact),
            supplemental_evidence_placements=(
                build_d2_table_supplemental_object_owner_placements(
                    self.source,
                    artifact,
                    owner_reference_kind=(
                        D2SupplementalObjectOwnerReferenceKindV1.RETAINED_BASE_OBJECT
                    ),
                    owner_reference=self.text_object.logical_object_id,
                    reason_codes=("bind_table_supplemental_evidence",),
                )
            ),
        )

        compiled = compile_d2_logical_structure_v2(
            self.source,
            base_proposal=partial_base,
            base_validation_receipt=partial_receipt,
            table_region_artifact=artifact,
            placement_plan=plan,
            expected_kit_identity=self.kit,
        )

        self.assertEqual(compiled.proposal.status, D2LogicalStructureStatus.PARTIAL)
        self.assertIn(
            "base_logical_structure_partial",
            compiled.proposal.unresolved_codes,
        )

    def test_empty_table_is_explicit_partial_instead_of_false_complete(self) -> None:
        source = _source_fixture(empty_tables=True)
        base, receipt = _base_proposal_and_receipt(
            source,
            self.kit,
            self.d1,
            self.table_map,
        )
        empty_table_id = source.nodes[-1].table_payload.table_id
        artifact = _regions(
            source,
            regions=(),
            empty_dispositions=(
                D2EmptyPhysicalTableDispositionCandidateV1(
                    physical_table_id=empty_table_id,
                    disposition=D2EmptyPhysicalTableDispositionKind.UNRESOLVED,
                    reason_codes=("empty_table_unresolved",),
                ),
            ),
        )
        retained = tuple(
            D2RetainedBaseObjectPlacementV1(
                base_logical_object_id=item.logical_object_id,
                parent_logical_object_id=item.parent_logical_object_id,
                reason_codes=("retain_base_object",),
            )
            for item in base.logical_objects
            if item.object_kind is not D2LogicalObjectKind.TABLE
        )
        table_object = next(
            item
            for item in base.logical_objects
            if item.object_kind is D2LogicalObjectKind.TABLE
        )
        plan = build_d2_region_placement_plan(
            base_proposal=base,
            base_validation_receipt=receipt,
            table_region_artifact=artifact,
            retained_base_objects=retained,
            base_table_replacements=(
                D2BaseTableReplacementCandidateV1(
                    base_logical_object_id=table_object.logical_object_id,
                    disposition=(
                        D2BaseTableReplacementDisposition.PRESERVED_AS_UNRESOLVED_LINEAGE
                    ),
                    reason_codes=("empty_table_unresolved",),
                ),
            ),
        )

        compiled = compile_d2_logical_structure_v2(
            source,
            base_proposal=base,
            base_validation_receipt=receipt,
            table_region_artifact=artifact,
            placement_plan=plan,
            expected_kit_identity=self.kit,
        )

        self.assertEqual(compiled.proposal.status, D2LogicalStructureStatus.PARTIAL)
        self.assertIn("empty_table_ir_unresolved", compiled.proposal.unresolved_codes)
        table_evidence = compiled.proposal.physical_table_evidence[0]
        self.assertRegex(
            table_evidence.physical_region_coverage_sha256,
            r"^[0-9a-f]{64}$",
        )
        self.assertIsNotNone(table_evidence.empty_table_disposition)

    def _compile(self, artifact, plan):
        return compile_d2_logical_structure_v2(
            self.source,
            base_proposal=self.base,
            base_validation_receipt=self.base_receipt,
            table_region_artifact=artifact,
            placement_plan=plan,
            expected_kit_identity=self.kit,
        )

    def _retained(self):
        return tuple(
            D2RetainedBaseObjectPlacementV1(
                base_logical_object_id=item.logical_object_id,
                parent_logical_object_id=item.parent_logical_object_id,
                reason_codes=("retain_base_object",),
            )
            for item in self.base.logical_objects
            if item.object_kind is not D2LogicalObjectKind.TABLE
        )

    def _replacements(
        self,
        artifact,
        dispositions: tuple[D2BaseTableReplacementDisposition, ...] | None = None,
    ):
        dispositions = dispositions or tuple(
            D2BaseTableReplacementDisposition.REPLACED_BY_REGION_TOPOLOGY
            for _ in self.base_table_objects
        )
        return tuple(
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

    def _scope_placements(self, artifact):
        return tuple(
            D2RegionLogicalScopePlacementV1(
                logical_scope_id=item.logical_scope_id,
                parent_logical_object_id=self.text_object.logical_object_id,
                reason_codes=("place_region_scope",),
            )
            for item in artifact.logical_scopes
        )

    def _plan(
        self,
        artifact,
        *,
        object_placements=(),
        replacement_dispositions=None,
        supplemental_placements=None,
    ):
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
        return build_d2_region_placement_plan(
            base_proposal=self.base,
            base_validation_receipt=self.base_receipt,
            table_region_artifact=artifact,
            retained_base_objects=self._retained(),
            base_table_replacements=self._replacements(
                artifact,
                replacement_dispositions,
            ),
            region_logical_scope_placements=self._scope_placements(artifact),
            region_logical_object_placements=object_placements,
            supplemental_evidence_placements=supplemental_placements,
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


def _source_fixture(*, empty_tables: bool = False) -> SourceIRSnapshot:
    adapter_id = "d2-v2-test"
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
    table_count = 1 if empty_tables else 2
    for table_index in range(table_count):
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
        cells = (
            ()
            if empty_tables
            else tuple(
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


def _base_proposal_and_receipt(
    source: SourceIRSnapshot,
    kit: AgentKitIdentityV1,
    d1: D2ValidatedD1ArtifactIdentityV1,
    table_map: AgentD1TableMapArtifactIdentityV1,
):
    text_node = source.nodes[0]
    text_segment = _text_segment(source, text_node)
    text_object = _base_text_object(source, text_segment)
    table_nodes = tuple(
        node for node in source.nodes if node.kind is SourceNodeKind.TABLE
    )
    scopes = tuple(
        build_agent_logical_table_scope(
            source,
            d1_table_map_artifact=table_map,
            physical_table_ids=(node.table_payload.table_id,),
            reason_codes=("base_whole_table",),
        )
        for node in table_nodes
    )
    table_objects = tuple(_base_table_object(scope) for scope in scopes)
    dispositions = tuple(
        _base_table_disposition(scope.logical_object_id, scope.physical_table_ids[0])
        for scope in scopes
    )
    source_disposition = _base_source_disposition(
        text_segment,
        text_object.logical_object_id,
    )
    values = {
        "run_id": RUN_ID,
        "source_sha256": source.source_sha256,
        "source_ir_id": source.source_ir_id,
        "source_ir_canonical_sha256": source_ir_canonical_sha256(source),
        "kit_identity": kit,
        "predecessor_d1_artifact": d1,
        "d1_table_map_artifact": table_map,
        "status": D2LogicalStructureStatus.COMPLETE,
        "logical_objects": tuple(
            sorted(
                (text_object, *table_objects),
                key=lambda item: (item.source_order, item.logical_object_id),
            )
        ),
        "logical_table_scopes": scopes,
        "source_evidence_dispositions": (source_disposition,),
        "physical_table_dispositions": dispositions,
        "unresolved_codes": (),
    }
    draft = D2LogicalStructureProposalV1.model_construct(
        **values,
        proposal_sha256="0" * 64,
    )
    proposal = D2LogicalStructureProposalV1(
        **values,
        proposal_sha256=stable_d2_logical_structure_proposal_sha256(draft),
    )
    receipt = validate_d2_logical_structure_proposal(
        source,
        proposal,
        expected_d1_artifact=d1,
        expected_d1_table_map_artifact=table_map,
        expected_kit_identity=kit,
    )
    return proposal, receipt


def _text_segment(source: SourceIRSnapshot, node: SourceNode) -> D2SourceSegmentV1:
    start = 0
    end = len(node.raw_text)
    content_sha = hashlib.sha256(node.raw_text.encode()).hexdigest()
    segment_id = stable_d2_source_segment_id(
        source_sha256=source.source_sha256,
        handle_kind=D2SourceHandleKind.SOURCE_NODE,
        source_handle=node.source_node_id,
        start_offset=start,
        end_offset=end,
    )
    values = {
        "segment_id": segment_id,
        "source_sha256": source.source_sha256,
        "handle_kind": D2SourceHandleKind.SOURCE_NODE,
        "source_handle": node.source_node_id,
        "owner_source_node_id": node.source_node_id,
        "source_order": node.source_order,
        "page_numbers": (node.native_locator.page_number,),
        "start_offset": start,
        "end_offset": end,
        "source_content_sha256": content_sha,
    }
    return D2SourceSegmentV1(
        **values,
        segment_sha256=stable_d2_source_segment_sha256(**values),
    )


def _base_text_object(
    source: SourceIRSnapshot,
    segment: D2SourceSegmentV1,
) -> D2LogicalObjectV1:
    object_id = stable_d2_logical_object_id(
        source_sha256=source.source_sha256,
        object_kind=D2LogicalObjectKind.SECTION,
        source_segment_ids=(segment.segment_id,),
    )
    values = {
        "logical_object_id": object_id,
        "object_kind": D2LogicalObjectKind.SECTION,
        "parent_logical_object_id": None,
        "source_order": segment.source_order,
        "page_numbers": segment.page_numbers,
        "source_segments": (segment,),
        "logical_table_scope_ids": (),
        "reason_codes": ("source_boundary",),
    }
    return D2LogicalObjectV1(
        **values,
        object_sha256=stable_d2_logical_object_sha256(**values),
    )


def _base_table_object(scope: AgentLogicalTableScopeV1) -> D2LogicalObjectV1:
    values = {
        "logical_object_id": scope.logical_object_id,
        "object_kind": D2LogicalObjectKind.TABLE,
        "parent_logical_object_id": None,
        "source_order": scope.physical_parts[0].source_order,
        "page_numbers": tuple(
            sorted(
                {page for part in scope.physical_parts for page in part.page_numbers}
            )
        ),
        "source_segments": (),
        "logical_table_scope_ids": (scope.logical_object_id,),
        "reason_codes": ("base_whole_table",),
    }
    return D2LogicalObjectV1(
        **values,
        object_sha256=stable_d2_logical_object_sha256(**values),
    )


def _base_table_disposition(
    scope_id: str,
    table_id: str,
) -> D2PhysicalTableDispositionV1:
    values = {
        "table_id": table_id,
        "disposition": D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART,
        "logical_table_scope_id": scope_id,
        "nested_owner_table_id": None,
        "reason_codes": ("base_whole_table",),
        "evidence_refs": (f"document-table-map:{table_id}",),
    }
    return D2PhysicalTableDispositionV1(
        **values,
        disposition_sha256=stable_d2_table_disposition_sha256(**values),
    )


def _base_source_disposition(
    segment: D2SourceSegmentV1,
    object_id: str,
) -> D2SourceEvidenceDispositionV1:
    disposition_id = stable_d2_source_disposition_id(
        source_sha256=segment.source_sha256,
        source_node_id=segment.owner_source_node_id,
        start_offset=segment.start_offset,
        end_offset=segment.end_offset,
    )
    values = {
        "disposition_id": disposition_id,
        "source_sha256": segment.source_sha256,
        "source_node_id": segment.owner_source_node_id,
        "source_order": segment.source_order,
        "start_offset": segment.start_offset,
        "end_offset": segment.end_offset,
        "disposition": D2SourceEvidenceDispositionKind.LOGICAL_OBJECT_PART,
        "logical_object_id": object_id,
        "source_segment_id": segment.segment_id,
        "reason_codes": ("source_disposition",),
        "evidence_refs": (segment.owner_source_node_id,),
    }
    return D2SourceEvidenceDispositionV1(
        **values,
        disposition_sha256=stable_d2_source_disposition_sha256(**values),
    )


def _regions(
    source: SourceIRSnapshot,
    *,
    regions: tuple[object, ...],
    scopes: tuple[D2TableLogicalScopeCandidateV1, ...] = (),
    empty_dispositions: tuple[D2EmptyPhysicalTableDispositionCandidateV1, ...] = (),
) -> D2TableRegionArtifactV1:
    candidate = D2TableRegionCandidateArtifactV1(
        source_ir_id=source.source_ir_id,
        source_sha256=source.source_sha256,
        source_ir_canonical_sha256=d2_table_region_source_ir_canonical_sha256(source),
        kit_bundle_sha256=KIT_SHA,
        d1_table_map_artifact_id="d1-table-map-" + "4" * 32,
        d1_table_map_artifact_sha256="5" * 64,
        regions=regions,
        logical_scopes=scopes,
        empty_table_dispositions=empty_dispositions,
    )
    return compile_d2_table_regions(
        source,
        candidate,
        expected_kit_bundle_sha256=KIT_SHA,
        expected_d1_table_map_artifact_id="d1-table-map-" + "4" * 32,
        expected_d1_table_map_artifact_sha256="5" * 64,
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


def _scope_candidate(
    key: str,
    regions: tuple[str, ...],
) -> D2TableLogicalScopeCandidateV1:
    return D2TableLogicalScopeCandidateV1(
        logical_scope_key=key,
        ordered_region_keys=regions,
        reason_codes=("same_logical_table",),
    )


def _span(start: int, end: int) -> D2PhysicalTableCellOrdinalSpanV1:
    return D2PhysicalTableCellOrdinalSpanV1(
        start_cell_ordinal=start,
        end_cell_ordinal_exclusive=end,
    )


if __name__ == "__main__":
    unittest.main()
