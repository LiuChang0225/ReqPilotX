from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from pathlib import Path
import tempfile

from app.adapters.agents.codex_app_server.canonical_stage_hosts import (
    CanonicalParentProvisionObjectBatchCompiler,
)
from app.adapters.agents.codex_app_server.d3_region_stage_input_workspace import (
    materialize_d3_region_stage_input_workspace,
)
from app.application.agent_workflow.d2_decision_candidate_v3 import (
    D2LogicalStructureDecisionCandidateV3,
    D2RegionLogicalObjectPlacementDecisionCandidateV3,
    D2RegionLogicalScopePlacementDecisionCandidateV3,
    D2TableRegionDecisionCandidateV3,
    D2UnifiedLogicalObjectDecisionCandidateV3,
    compile_and_validate_d2_logical_structure_candidate_v3,
)
from app.application.agent_workflow.d2_logical_structure import D2LogicalObjectKind
from app.application.agent_workflow.d2_logical_structure_v2 import (
    D2V2LogicalObjectOrigin,
)
from app.application.agent_workflow.d2_table_regions import (
    D2LogicalObjectPartTableRegionCandidateV1,
    D2NestedValueTableRegionCandidateV1,
    D2TableRegionDispositionKind,
)
from app.application.agent_workflow.d3_parent_provisions import (
    ParentProvisionStatus,
)
from app.application.agent_workflow.d3_region_stage_input import (
    D3EvidenceLedgerKindV1,
    D3UnifiedEvidenceKindV3,
    build_d3_region_stage_input_lineage,
    materialize_d3_region_stage_input,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectBatchError,
    ParentProvisionObjectExecutionMode,
    build_parent_provision_object_batch_plan,
)
from app.application.agent_workflow.region_parent_runtime_adapter import (
    RegionParentRuntimeAdapterError,
    adapt_region_parent_runtime,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    source_ir_canonical_sha256,
)

if __package__:
    from .test_d2_decision_candidate_v3 import (
        SOURCE_SHA,
        _d1_identity,
        _false_region,
        _kit_identity,
        _scope,
        _scope_region,
        _section_object,
        _source_fixture,
        _span,
        _supplemental,
    )
else:
    from test_d2_decision_candidate_v3 import (
        SOURCE_SHA,
        _d1_identity,
        _false_region,
        _kit_identity,
        _scope,
        _scope_region,
        _section_object,
        _source_fixture,
        _span,
        _supplemental,
    )


class RegionParentRuntimeAdapterTests(unittest.TestCase):
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

    def test_same_table_two_scopes_feed_object_batches_with_row_major_cells(
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

        compiled, stage_input, runtime = self._runtime(candidate)
        batch_compiler = CanonicalParentProvisionObjectBatchCompiler(
            d2_identity=runtime.d2_identity,
            logical_objects=runtime.logical_objects,
            source_segments=runtime.source_segments,
            stage_input_bundle_sha256=runtime.workspace_bundle_sha256,
            kit_identity=self.kit,
            stage_input_manifest_sha256=runtime.stage_input_manifest_sha256,
            runtime_adapter_sha256=runtime.adapter_sha256,
        )
        plan = batch_compiler.build_plan()
        table_cells = tuple(
            item
            for item in runtime.source_segments
            if item.handle_kind == "TABLE_CELL"
        )

        self.assertEqual(
            plan.stage_input_manifest_sha256,
            runtime.stage_input_manifest_sha256,
        )
        self.assertEqual(plan.runtime_adapter_sha256, runtime.adapter_sha256)
        self.assertTrue(
            all(
                item.stage_input_manifest_sha256
                == runtime.stage_input_manifest_sha256
                and item.runtime_adapter_sha256 == runtime.adapter_sha256
                for item in plan.targets
            )
        )
        with self.assertRaisesRegex(
            ParentProvisionObjectBatchError,
            "REGION_RUNTIME_PINS_REQUIRED",
        ):
            build_parent_provision_object_batch_plan(
                d2_identity=runtime.d2_identity,
                logical_objects=runtime.logical_objects,
                source_segments=runtime.source_segments,
                stage_input_bundle_sha256=runtime.workspace_bundle_sha256,
            )
        repinned = build_parent_provision_object_batch_plan(
            d2_identity=runtime.d2_identity,
            logical_objects=runtime.logical_objects,
            source_segments=runtime.source_segments,
            stage_input_bundle_sha256=runtime.workspace_bundle_sha256,
            stage_input_manifest_sha256=runtime.stage_input_manifest_sha256,
            runtime_adapter_sha256="f" * 64,
        )
        self.assertNotEqual(
            tuple(item.target_evidence_sha256 for item in plan.targets),
            tuple(item.target_evidence_sha256 for item in repinned.targets),
        )

        self.assertEqual(len(table_cells), 4)
        self.assertEqual(
            tuple(item.source_text for item in table_cells),
            tuple(f"table-1-cell-{index}" for index in range(4)),
        )
        self.assertEqual(
            tuple(item.source_handle.rsplit(":", 1)[-1] for item in table_cells),
            ("0", "1", "2", "3"),
        )
        scope_ids = {
            item.logical_object_id
            for item in compiled.final_compilation.proposal.logical_objects
            if item.origin is D2V2LogicalObjectOrigin.REGION_LOGICAL_SCOPE
        }
        self.assertEqual(
            {
                item.logical_object_id
                for item in plan.targets
                if item.logical_object_id.startswith("d2-table-logical-scope-")
            },
            scope_ids,
        )
        self.assertTrue(
            all(
                item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
                for item in plan.targets
                if item.logical_object_id in scope_ids
            )
        )

    def test_table_frame_prose_and_base_context_remain_exact(self) -> None:
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

        compiled, _, runtime = self._runtime(candidate)
        section = next(
            item
            for item in compiled.final_compilation.proposal.logical_objects
            if item.object_kind is D2LogicalObjectKind.SECTION
        )
        section_segments = tuple(
            item
            for item in runtime.source_segments
            if item.logical_object_id == section.logical_object_id
        )

        self.assertEqual(
            section.origin,
            D2V2LogicalObjectOrigin.RETAINED_BASE_WITH_REGION_CONTENT,
        )
        self.assertTrue(any(item.handle_kind == "SOURCE_NODE" for item in section_segments))
        self.assertEqual(
            len(tuple(item for item in section_segments if item.handle_kind == "TABLE_CELL")),
            4,
        )
        self.assertTrue(
            any(item.handle_kind == "TABLE_NODE_RAW_TEXT" for item in section_segments)
        )
        self.assertTrue(any(item.handle_kind == "TABLE_NOTE" for item in section_segments))

    def test_cross_table_scope_preserves_unified_source_position(self) -> None:
        candidate = self._candidate(
            regions=(
                _scope_region("page-one", self.table_ids[0], 0, 4, "scope-a"),
                _scope_region("page-two", self.table_ids[1], 0, 4, "scope-a"),
            ),
            scopes=(_scope("scope-a", ("page-one", "page-two")),),
        )

        compiled, stage_input, runtime = self._runtime(candidate)
        scope_id = next(
            item.logical_object_id
            for item in runtime.logical_objects
            if item.origin is D2V2LogicalObjectOrigin.REGION_LOGICAL_SCOPE
        )
        unified = tuple(
            item
            for item in stage_input.segment_set.segments
            if item.logical_object_id == scope_id
            and item.evidence_kind is D3UnifiedEvidenceKindV3.TABLE_CELL
        )
        exact = tuple(
            item for item in runtime.source_segments if item.logical_object_id == scope_id
        )

        self.assertEqual(
            tuple(item.segment_id for item in exact),
            tuple(item.segment_id for item in unified),
        )
        self.assertEqual(
            tuple(item.source_order for item in exact),
            (1, 1, 1, 1, 2, 2, 2, 2),
        )
        self.assertTrue(scope_id.startswith("d2-table-logical-scope-"))

    def test_nested_and_visual_evidence_stay_owned_outside_exact_cover(self) -> None:
        candidate = self._candidate(
            regions=(
                _scope_region("owner", self.table_ids[0], 0, 3, "scope-a"),
                D2NestedValueTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                    region_key="nested",
                    physical_table_id=self.table_ids[0],
                    cell_spans=(_span(3, 4),),
                    nested_owner_region_key="owner",
                    reason_codes=("nested_value",),
                ),
                _false_region("layout-table", self.table_ids[1], 0, 4),
            ),
            scopes=(_scope("scope-a", ("owner",)),),
        )

        _, _, runtime = self._runtime(candidate)
        nested = runtime.nested_evidence_ledger[0]
        assignment = runtime.selected_parent_assignment_ledger[0]
        visual = tuple(
            item
            for item in runtime.evidence_ledger
            if item.evidence_kind is D3EvidenceLedgerKindV1.TABLE_SUPPLEMENTAL
            and any("visual" in value for value in item.evidence_refs)
        )

        self.assertTrue(nested.selected_parent_assignment_eligible)
        self.assertEqual(
            assignment.ultimate_owner_logical_object_id,
            nested.ultimate_owner_logical_object_id,
        )
        self.assertEqual(assignment.owner_segment_ids, nested.owner_segment_ids)
        self.assertNotIn(
            nested.evidence_id,
            runtime.d2_identity.source_segment_ids,
        )
        self.assertTrue(visual)
        self.assertTrue(
            all(item.ultimate_owner_logical_object_id is not None for item in visual)
        )
        self.assertTrue(all(item.owner_segment_ids for item in visual))

    def test_partial_stage_marks_identity_without_semantic_execution_failure(
        self,
    ) -> None:
        candidate = self._candidate(
            regions=(
                _scope_region("first", self.table_ids[0], 0, 4, "scope-a"),
                _scope_region("second", self.table_ids[1], 0, 4, "scope-b"),
            ),
            scopes=(
                _scope("scope-a", ("first",)),
                _scope("scope-b", ("second",)),
            ),
            supplemental=(),
        )

        compiled, stage_input, runtime = self._runtime(candidate)
        plan = build_parent_provision_object_batch_plan(
            d2_identity=runtime.d2_identity,
            logical_objects=runtime.logical_objects,
            source_segments=runtime.source_segments,
            stage_input_bundle_sha256=runtime.workspace_bundle_sha256,
            stage_input_manifest_sha256=runtime.stage_input_manifest_sha256,
            runtime_adapter_sha256=runtime.adapter_sha256,
        )

        self.assertEqual(runtime.d2_identity.status, ParentProvisionStatus.PARTIAL)
        self.assertTrue(stage_input.segment_set.unresolved_scopes)
        self.assertEqual(
            runtime.unresolved_scopes,
            stage_input.segment_set.unresolved_scopes,
        )
        self.assertTrue(
            all(
                item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
                for item in plan.targets
            )
        )

    def test_new_region_object_id_regex_and_manifest_tamper_fail_closed(self) -> None:
        new_object = D2UnifiedLogicalObjectDecisionCandidateV3(
            object_key="region-prose",
            object_kind=D2LogicalObjectKind.PROVISION_SCOPE,
            parent_object_key="section",
            reason_codes=("region_only_prose",),
        )
        candidate = self._candidate(
            logical_objects=(_section_object(), new_object),
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
                    target_object_key="region-prose",
                    source_region_logical_object_keys=("frame-prose-object",),
                    reason_codes=("new_region_object",),
                ),
            ),
        )

        compiled, stage_input, runtime = self._runtime(candidate)
        region_object_id = next(
            item.logical_object_id
            for item in runtime.logical_objects
            if item.logical_object_id.startswith("d2-v2-region-object-")
        )
        plan = build_parent_provision_object_batch_plan(
            d2_identity=runtime.d2_identity,
            logical_objects=runtime.logical_objects,
            source_segments=runtime.source_segments,
            stage_input_bundle_sha256=runtime.workspace_bundle_sha256,
            stage_input_manifest_sha256=runtime.stage_input_manifest_sha256,
            runtime_adapter_sha256=runtime.adapter_sha256,
        )
        self.assertIn(region_object_id, tuple(item.logical_object_id for item in plan.targets))

        with self.assertRaisesRegex(
            RegionParentRuntimeAdapterError,
            "MANIFEST_PIN_DRIFT",
        ):
            adapt_region_parent_runtime(
                proposal=compiled.final_compilation.proposal,
                validation_receipt=compiled.final_compilation.validation_receipt,
                stage_input=stage_input,
                expected_kit_identity=self.kit,
                expected_source_sha256=self.source.source_sha256,
                expected_source_ir_canonical_sha256=source_ir_canonical_sha256(
                    self.source
                ),
                expected_stage_input_manifest_sha256="f" * 64,
                expected_workspace_bundle_sha256="e" * 64,
            )

    def test_region_compiler_factory_rejects_runtime_workspace_mismatch(self) -> None:
        candidate = self._candidate(
            regions=(
                _scope_region("first", self.table_ids[0], 0, 4, "scope-a"),
                _scope_region("second", self.table_ids[1], 0, 4, "scope-b"),
            ),
            scopes=(
                _scope("scope-a", ("first",)),
                _scope("scope-b", ("second",)),
            ),
        )
        compiled, stage_input, _ = self._runtime(candidate)
        with tempfile.TemporaryDirectory() as raw:
            workspace = materialize_d3_region_stage_input_workspace(
                session_root=Path(raw),
                source_ir=self.source,
                d2_proposal=compiled.final_compilation.proposal,
                d2_validation_receipt=(
                    compiled.final_compilation.validation_receipt
                ),
                parent_input=compiled.region_parent_input.stage_input,
                parent_input_receipt=compiled.region_parent_input.receipt,
                expected_lineage=stage_input.manifest.lineage,
            )
            exact_runtime = adapt_region_parent_runtime(
                proposal=compiled.final_compilation.proposal,
                validation_receipt=compiled.final_compilation.validation_receipt,
                stage_input=stage_input,
                expected_kit_identity=self.kit,
                expected_source_sha256=self.source.source_sha256,
                expected_source_ir_canonical_sha256=source_ir_canonical_sha256(
                    self.source
                ),
                expected_stage_input_manifest_sha256=(
                    stage_input.manifest.manifest_sha256
                ),
                expected_workspace_bundle_sha256=(
                    workspace.attestation.bundle_sha256
                ),
            )
            host = CanonicalParentProvisionObjectBatchCompiler.from_region_runtime(
                runtime=exact_runtime,
                workspace_attestation=workspace.attestation,
                kit_identity=self.kit,
            )
            self.assertEqual(
                host.stage_input_manifest_sha256,
                stage_input.manifest.manifest_sha256,
            )

            mismatched_runtime = adapt_region_parent_runtime(
                proposal=compiled.final_compilation.proposal,
                validation_receipt=compiled.final_compilation.validation_receipt,
                stage_input=stage_input,
                expected_kit_identity=self.kit,
                expected_source_sha256=self.source.source_sha256,
                expected_source_ir_canonical_sha256=source_ir_canonical_sha256(
                    self.source
                ),
                expected_stage_input_manifest_sha256=(
                    stage_input.manifest.manifest_sha256
                ),
                expected_workspace_bundle_sha256="f" * 64,
            )
            with self.assertRaisesRegex(ValueError, "runtime/workspace identity drifted"):
                CanonicalParentProvisionObjectBatchCompiler.from_region_runtime(
                    runtime=mismatched_runtime,
                    workspace_attestation=workspace.attestation,
                    kit_identity=self.kit,
                )

    def _candidate(
        self,
        *,
        regions: tuple[object, ...],
        scopes: tuple[object, ...] = (),
        logical_objects: tuple[D2UnifiedLogicalObjectDecisionCandidateV3, ...] | None = None,
        object_placements: tuple[
            D2RegionLogicalObjectPlacementDecisionCandidateV3, ...
        ] = (),
        supplemental=None,
    ) -> D2LogicalStructureDecisionCandidateV3:
        if logical_objects is None:
            logical_objects = (_section_object(),)
        if supplemental is None:
            supplemental = _supplemental(self.source)
        return D2LogicalStructureDecisionCandidateV3(
            logical_objects=logical_objects,
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

    def _runtime(self, candidate):
        compiled = compile_and_validate_d2_logical_structure_candidate_v3(
            candidate,
            source_ir=self.source,
            expected_d1_artifact=self.d1,
            expected_d1_table_map_artifact=self.table_map,
            expected_kit_identity=self.kit,
        )
        lineage = build_d3_region_stage_input_lineage(
            proposal=compiled.final_compilation.proposal,
            validation_receipt=compiled.final_compilation.validation_receipt,
            parent_input=compiled.region_parent_input.stage_input,
            parent_input_receipt=compiled.region_parent_input.receipt,
        )
        stage_input = materialize_d3_region_stage_input(
            self.source,
            proposal=compiled.final_compilation.proposal,
            validation_receipt=compiled.final_compilation.validation_receipt,
            parent_input=compiled.region_parent_input.stage_input,
            parent_input_receipt=compiled.region_parent_input.receipt,
            expected_lineage=lineage,
        )
        runtime = adapt_region_parent_runtime(
            proposal=compiled.final_compilation.proposal,
            validation_receipt=compiled.final_compilation.validation_receipt,
            stage_input=stage_input,
            expected_kit_identity=self.kit,
            expected_source_sha256=self.source.source_sha256,
            expected_source_ir_canonical_sha256=source_ir_canonical_sha256(
                self.source
            ),
            expected_stage_input_manifest_sha256=stage_input.manifest.manifest_sha256,
            expected_workspace_bundle_sha256="e" * 64,
        )
        return compiled, stage_input, runtime


if __name__ == "__main__":
    unittest.main()
