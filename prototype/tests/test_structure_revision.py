from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from datetime import UTC, datetime, timedelta

from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.orchestrate_structure_agent import (
    apply_manual_structure_operations,
)
from app.application.extraction.revise_structure import (
    apply_structure_patch,
    create_structure_patch_proposal,
    get_base_effective_structure,
    undo_structure_revision,
    validate_structure_patch_proposal,
    validate_structure_revision_chain,
)
from app.application.extraction.structure_workspace import (
    _updated_result_after_structure_mutation,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
)
from app.domain.extraction.structure import (
    StructureNode,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.domain.extraction.structure_revision import (
    AddStructureRelationOperationV1,
    BoundaryResegmentStructureOperationV1,
    BoundarySplitPartV1,
    BoundarySplitStructureOperationV1,
    ContinuationMergeStructureOperationV1,
    LogicalReorderStructureOperationV1,
    RemoveStructureRelationOperationV1,
    ReparentStructureOperationV1,
    StructureApplyResultV1,
    StructurePatchOperationV1,
    StructurePatchProposalV1,
    StructureRevisionAction,
    StructureRevisionRejected,
    StructureRevisionRejectionCode,
    StructureRevisionV1,
    StructureSourceSpanV1,
    canonical_model_sha256,
)


_SOURCE_SHA256 = "7" * 64
_ADAPTER_ID = "synthetic-structure-revision"
_ADAPTER_VERSION = "1.0.0"
_CREATED_AT = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
_TEXTS = (
    "1 Scope",
    "1.1 Alpha",
    "Alpha one. Alpha two.",
    "Second continuation.",
    "1.2 Beta",
    "Beta content.",
    "2 Other",
    "Other content.",
)


class StructureRevisionCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_ir = _source_ir_fixture()
        self.base_tree = build_structure_tree(self.source_ir)
        self.base_effective = get_base_effective_structure(
            self.source_ir,
            self.base_tree,
        )
        self.source_hash_before = canonical_model_sha256(self.source_ir)
        self.base_tree_hash_before = canonical_model_sha256(self.base_tree)
        self.nodes_by_text = {
            source_node.raw_text: _structure_node_for_source(
                self.base_tree.nodes,
                source_node.source_node_id,
            )
            for source_node in self.source_ir.nodes
        }
        self.sections_by_label = {
            node.explicit_label: node
            for node in self.base_tree.nodes
            if node.explicit_label is not None
        }

    def test_reparent_preserves_node_id_and_appends_revision(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        result, revisions = self._accept_and_apply(
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=new_parent.structure_node_id,
                    new_sibling_index=0,
                ),
            )
        )

        effective_target = _node_by_id(
            result.effective_snapshot.nodes,
            target.structure_node_id,
        )
        self.assertEqual(effective_target.structure_node_id, target.structure_node_id)
        self.assertEqual(
            effective_target.parent_structure_node_id,
            new_parent.structure_node_id,
        )
        self.assertEqual(result.effective_snapshot.revision_number, 1)
        self.assertEqual(len(revisions), 1)
        self.assertIn(
            target.structure_node_id,
            result.impact.direct_structure_node_ids,
        )
        self._assert_base_and_source_unchanged()

    def test_logical_reorder_preserves_all_node_ids(self) -> None:
        root = next(
            node
            for node in self.base_tree.nodes
            if node.parent_structure_node_id is None
        )
        section_one = self.sections_by_label["1"]
        section_two = self.sections_by_label["2"]
        result, _ = self._accept_and_apply(
            (
                LogicalReorderStructureOperationV1(
                    parent_structure_node_id=root.structure_node_id,
                    ordered_child_structure_node_ids=(
                        section_two.structure_node_id,
                        section_one.structure_node_id,
                    ),
                ),
            )
        )

        effective_ids = {
            node.structure_node_id for node in result.effective_snapshot.nodes
        }
        self.assertEqual(
            effective_ids,
            {node.structure_node_id for node in self.base_tree.nodes},
        )
        root_children = _children(
            result.effective_snapshot.nodes,
            root.structure_node_id,
        )
        self.assertEqual(
            tuple(node.structure_node_id for node in root_children),
            (section_two.structure_node_id, section_one.structure_node_id),
        )
        self._assert_base_and_source_unchanged()

    def test_boundary_split_creates_only_source_span_bound_ids(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        text = "Alpha one. Alpha two."
        split_at = text.index("Alpha two")
        result, revisions = self._accept_and_apply(
            (self._split_operation(target, ((0, split_at), (split_at, len(text)))),)
        )

        effective_ids = {
            node.structure_node_id for node in result.effective_snapshot.nodes
        }
        base_ids = {node.structure_node_id for node in self.base_tree.nodes}
        created_ids = effective_ids - base_ids
        self.assertEqual(len(created_ids), 2)
        self.assertNotIn(target.structure_node_id, effective_ids)
        created_nodes = tuple(
            node
            for node in result.effective_snapshot.nodes
            if node.structure_node_id in created_ids
        )
        self.assertTrue(
            all(node.identity_key.startswith("model:") for node in created_nodes)
        )
        replayed = validate_structure_revision_chain(
            self.source_ir,
            self.base_tree,
            revisions,
        )
        self.assertEqual(replayed, result.effective_snapshot)
        self.assertEqual(
            {
                node.structure_node_id
                for node in replayed.nodes
                if node.identity_key.startswith("model:")
            },
            created_ids,
        )
        self._assert_base_and_source_unchanged()

    def test_boundary_resegment_replaces_both_nodes_and_replays_and_undoes(
        self,
    ) -> None:
        split_result, pair, revisions = self._split_alpha_pair()
        preceding, following = pair
        text = "Alpha one. Alpha two."
        old_boundary = text.index("Alpha two")
        new_boundary = old_boundary + 2

        result, revisions = self._accept_and_apply(
            (
                BoundaryResegmentStructureOperationV1(
                    preceding_structure_node_id=preceding.structure_node_id,
                    following_structure_node_id=following.structure_node_id,
                    new_boundary_offset=new_boundary,
                ),
            ),
            revisions,
        )

        effective_ids = {
            node.structure_node_id for node in result.effective_snapshot.nodes
        }
        self.assertNotIn(preceding.structure_node_id, effective_ids)
        self.assertNotIn(following.structure_node_id, effective_ids)
        replacement_nodes = tuple(
            node
            for node in result.effective_snapshot.nodes
            if node.identity_key.startswith("model:")
            and node.source_refs == preceding.source_refs
            and node.parent_structure_node_id
            == preceding.parent_structure_node_id
        )
        replacement_spans = {
            binding.structure_node_id: binding.source_spans
            for binding in result.effective_snapshot.node_source_spans
        }
        replacement_nodes = tuple(
            sorted(
                replacement_nodes,
                key=lambda node: replacement_spans[node.structure_node_id][
                    0
                ].start_offset,
            )
        )
        self.assertEqual(len(replacement_nodes), 2)
        self.assertEqual(
            tuple(node.kind for node in replacement_nodes),
            (preceding.kind, following.kind),
        )
        self.assertEqual(
            tuple(
                (
                    replacement_spans[node.structure_node_id][0].start_offset,
                    replacement_spans[node.structure_node_id][0].end_offset,
                )
                for node in replacement_nodes
            ),
            ((0, new_boundary), (new_boundary, len(text))),
        )
        self.assertEqual(result.effective_snapshot.revision_number, 2)
        self.assertEqual(
            validate_structure_revision_chain(
                self.source_ir,
                self.base_tree,
                revisions,
            ),
            result.effective_snapshot,
        )

        undone = undo_structure_revision(
            self.source_ir,
            self.base_tree,
            revisions,
            expected_revision_number=result.effective_snapshot.revision_number,
            expected_effective_snapshot_id=(
                result.effective_snapshot.effective_snapshot_id
            ),
        )
        replayed_undo = validate_structure_revision_chain(
            self.source_ir,
            self.base_tree,
            (*revisions, undone.revision),
        )
        self.assertEqual(replayed_undo, undone.effective_snapshot)
        self.assertEqual(
            undone.effective_snapshot.nodes,
            split_result.effective_snapshot.nodes,
        )
        self.assertEqual(
            undone.effective_snapshot.relations,
            split_result.effective_snapshot.relations,
        )
        self.assertEqual(
            undone.effective_snapshot.node_source_spans,
            split_result.effective_snapshot.node_source_spans,
        )
        self.assertEqual(undone.effective_snapshot.revision_number, 3)
        self._assert_base_and_source_unchanged()

    def test_boundary_resegment_rejects_the_existing_boundary(self) -> None:
        _, pair, revisions = self._split_alpha_pair()
        preceding, following = pair
        current_boundary = next(
            binding.source_spans[0].end_offset
            for binding in validate_structure_revision_chain(
                self.source_ir,
                self.base_tree,
                revisions,
            ).node_source_spans
            if binding.structure_node_id == preceding.structure_node_id
        )
        proposal = self._proposal(
            (
                BoundaryResegmentStructureOperationV1(
                    preceding_structure_node_id=preceding.structure_node_id,
                    following_structure_node_id=following.structure_node_id,
                    new_boundary_offset=current_boundary,
                ),
            ),
            revisions,
        )

        with self.assertRaises(StructureRevisionRejected) as raised:
            validate_structure_patch_proposal(
                proposal,
                self.source_ir,
                self.base_tree,
                revisions,
            )
        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
        )

    def test_boundary_resegment_rejects_non_parent_relations(self) -> None:
        _, pair, revisions = self._split_alpha_pair()
        preceding, following = pair
        _, revisions = self._accept_and_apply(
            (
                AddStructureRelationOperationV1(
                    kind=StructureRelationKind.REFERENCES,
                    source_structure_node_id=preceding.structure_node_id,
                    target_structure_node_id=following.structure_node_id,
                    source_refs=preceding.source_refs,
                ),
            ),
            revisions,
        )
        proposal = self._proposal(
            (
                BoundaryResegmentStructureOperationV1(
                    preceding_structure_node_id=preceding.structure_node_id,
                    following_structure_node_id=following.structure_node_id,
                    new_boundary_offset=(
                        len("Alpha one. Alpha two.") - len("Alpha two") + 1
                    ),
                ),
            ),
            revisions,
        )

        with self.assertRaises(StructureRevisionRejected) as raised:
            validate_structure_patch_proposal(
                proposal,
                self.source_ir,
                self.base_tree,
                revisions,
            )
        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.INVALID_OPERATION,
        )

    def test_manual_mutation_persists_effective_node_source_spans(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        mutation = apply_manual_structure_operations(
            self.source_ir,
            self.base_tree,
            (),
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=new_parent.structure_node_id,
                    new_sibling_index=0,
                ),
            ),
            expected_revision_number=self.base_effective.revision_number,
            expected_effective_snapshot_id=(
                self.base_effective.effective_snapshot_id
            ),
        )

        _, updated_agent = _updated_result_after_structure_mutation(
            result={"items": []},
            agent={
                "effective_structure_identity": {
                    "snapshot_id": self.base_effective.effective_snapshot_id,
                },
                "stages": [],
            },
            source_ir=self.source_ir,
            mutation=mutation,
            action="PATCH",
        )

        self.assertEqual(
            updated_agent["effective_node_source_spans"],
            [
                binding.model_dump(mode="json")
                for binding in mutation.apply_result.effective_snapshot.node_source_spans
            ],
        )
        self.assertEqual(
            {
                item["structure_node_id"]
                for item in updated_agent["effective_node_source_spans"]
            },
            {
                node.structure_node_id
                for node in mutation.apply_result.effective_snapshot.nodes
            },
        )
        self._assert_base_and_source_unchanged()

    def test_continuation_merge_keeps_primary_id_and_combines_grounding(self) -> None:
        primary = self.nodes_by_text["Alpha one. Alpha two."]
        continuation = self.nodes_by_text["Second continuation."]
        add_result, revisions = self._accept_and_apply(
            (
                AddStructureRelationOperationV1(
                    kind=StructureRelationKind.CONTINUES,
                    source_structure_node_id=primary.structure_node_id,
                    target_structure_node_id=continuation.structure_node_id,
                    source_refs=continuation.source_refs,
                ),
            )
        )
        self.assertTrue(add_result.appended)

        merge_result, revisions = self._accept_and_apply(
            (
                ContinuationMergeStructureOperationV1(
                    primary_structure_node_id=primary.structure_node_id,
                    continuation_structure_node_id=continuation.structure_node_id,
                ),
            ),
            revisions,
        )
        effective_ids = {
            node.structure_node_id for node in merge_result.effective_snapshot.nodes
        }
        self.assertIn(primary.structure_node_id, effective_ids)
        self.assertNotIn(continuation.structure_node_id, effective_ids)
        merged = _node_by_id(
            merge_result.effective_snapshot.nodes,
            primary.structure_node_id,
        )
        self.assertEqual(
            merged.source_refs,
            (*primary.source_refs, *continuation.source_refs),
        )
        self.assertEqual(len(revisions), 2)
        self._assert_base_and_source_unchanged()

    def test_add_relation_preserves_endpoint_node_ids(self) -> None:
        source = self.nodes_by_text["Alpha one. Alpha two."]
        target = self.nodes_by_text["Beta content."]
        result, _ = self._accept_and_apply(
            (
                AddStructureRelationOperationV1(
                    kind=StructureRelationKind.REFERENCES,
                    source_structure_node_id=source.structure_node_id,
                    target_structure_node_id=target.structure_node_id,
                    source_refs=target.source_refs,
                ),
            )
        )

        added = tuple(
            relation
            for relation in result.effective_snapshot.relations
            if relation.kind is StructureRelationKind.REFERENCES
        )
        self.assertEqual(len(added), 1)
        self.assertEqual(
            (
                added[0].source_structure_node_id,
                added[0].target_structure_node_id,
            ),
            (source.structure_node_id, target.structure_node_id),
        )
        self.assertEqual(
            {node.structure_node_id for node in result.effective_snapshot.nodes},
            {node.structure_node_id for node in self.base_tree.nodes},
        )
        self._assert_base_and_source_unchanged()

    def test_remove_relation_appends_a_second_revision(self) -> None:
        source = self.nodes_by_text["Alpha one. Alpha two."]
        target = self.nodes_by_text["Beta content."]
        add_result, revisions = self._accept_and_apply(
            (
                AddStructureRelationOperationV1(
                    kind=StructureRelationKind.REFERENCES,
                    source_structure_node_id=source.structure_node_id,
                    target_structure_node_id=target.structure_node_id,
                    source_refs=target.source_refs,
                ),
            )
        )
        relation = next(
            relation
            for relation in add_result.effective_snapshot.relations
            if relation.kind is StructureRelationKind.REFERENCES
        )
        remove_result, revisions = self._accept_and_apply(
            (RemoveStructureRelationOperationV1(relation_id=relation.relation_id),),
            revisions,
        )

        self.assertFalse(
            any(
                candidate.relation_id == relation.relation_id
                for candidate in remove_result.effective_snapshot.relations
            )
        )
        self.assertEqual(remove_result.effective_snapshot.revision_number, 2)
        self.assertEqual(len(revisions), 2)
        self._assert_base_and_source_unchanged()

    def test_invalid_source_ref_is_rejected(self) -> None:
        source = self.nodes_by_text["Alpha one. Alpha two."]
        target = self.nodes_by_text["Beta content."]
        invalid_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=f"node-{'0' * 32}",
        )
        proposal = self._proposal(
            (
                AddStructureRelationOperationV1(
                    kind=StructureRelationKind.REFERENCES,
                    source_structure_node_id=source.structure_node_id,
                    target_structure_node_id=target.structure_node_id,
                    source_refs=(invalid_ref,),
                ),
            )
        )

        with self.assertRaises(StructureRevisionRejected) as raised:
            validate_structure_patch_proposal(
                proposal,
                self.source_ir,
                self.base_tree,
                (),
            )
        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.INVALID_SOURCE_REF,
        )
        self._assert_base_and_source_unchanged()

    def test_split_out_of_bounds_is_rejected(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        text_length = len("Alpha one. Alpha two.")
        proposal = self._proposal(
            (self._split_operation(target, ((0, 10), (10, text_length + 1))),)
        )

        with self.assertRaises(StructureRevisionRejected) as raised:
            validate_structure_patch_proposal(
                proposal,
                self.source_ir,
                self.base_tree,
                (),
            )
        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
        )

    def test_split_overlap_is_rejected(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        text_length = len("Alpha one. Alpha two.")
        proposal = self._proposal(
            (self._split_operation(target, ((0, 12), (11, text_length))),)
        )

        with self.assertRaises(StructureRevisionRejected) as raised:
            validate_structure_patch_proposal(
                proposal,
                self.source_ir,
                self.base_tree,
                (),
            )
        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
        )

    def test_split_missing_coverage_is_rejected(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        text_length = len("Alpha one. Alpha two.")
        proposal = self._proposal(
            (self._split_operation(target, ((0, 10), (11, text_length))),)
        )

        with self.assertRaises(StructureRevisionRejected) as raised:
            validate_structure_patch_proposal(
                proposal,
                self.source_ir,
                self.base_tree,
                (),
            )
        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.INVALID_SOURCE_SPAN,
        )

    def test_reparent_cycle_is_rejected(self) -> None:
        section_one = self.sections_by_label["1"]
        child_section = self.sections_by_label["1.1"]
        proposal = self._proposal(
            (
                ReparentStructureOperationV1(
                    structure_node_id=section_one.structure_node_id,
                    new_parent_structure_node_id=child_section.structure_node_id,
                    new_sibling_index=0,
                ),
            )
        )

        with self.assertRaises(StructureRevisionRejected) as raised:
            validate_structure_patch_proposal(
                proposal,
                self.source_ir,
                self.base_tree,
                (),
            )
        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.GRAPH_CYCLE,
        )

    def test_reorder_outside_one_sibling_scope_is_rejected(self) -> None:
        section_alpha = self.sections_by_label["1.1"]
        alpha = self.nodes_by_text["Alpha one. Alpha two."]
        beta = self.nodes_by_text["Beta content."]
        proposal = self._proposal(
            (
                LogicalReorderStructureOperationV1(
                    parent_structure_node_id=section_alpha.structure_node_id,
                    ordered_child_structure_node_ids=(
                        alpha.structure_node_id,
                        beta.structure_node_id,
                    ),
                ),
            )
        )

        with self.assertRaises(StructureRevisionRejected) as raised:
            validate_structure_patch_proposal(
                proposal,
                self.source_ir,
                self.base_tree,
                (),
            )
        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.INVALID_OPERATION,
        )

    def test_relation_missing_endpoint_is_rejected(self) -> None:
        source = self.nodes_by_text["Alpha one. Alpha two."]
        proposal = self._proposal(
            (
                AddStructureRelationOperationV1(
                    kind=StructureRelationKind.REFERENCES,
                    source_structure_node_id=source.structure_node_id,
                    target_structure_node_id=f"structure-node-{'0' * 32}",
                    source_refs=source.source_refs,
                ),
            )
        )

        with self.assertRaises(StructureRevisionRejected) as raised:
            validate_structure_patch_proposal(
                proposal,
                self.source_ir,
                self.base_tree,
                (),
            )
        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.DANGLING_ENDPOINT,
        )

    def test_stale_base_revision_fails_closed(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        stale_proposal = self._proposal(
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=new_parent.structure_node_id,
                    new_sibling_index=0,
                ),
            )
        )
        stale_patch = validate_structure_patch_proposal(
            stale_proposal,
            self.source_ir,
            self.base_tree,
            (),
        )
        root = next(
            node
            for node in self.base_tree.nodes
            if node.parent_structure_node_id is None
        )
        first_result, revisions = self._accept_and_apply(
            (
                LogicalReorderStructureOperationV1(
                    parent_structure_node_id=root.structure_node_id,
                    ordered_child_structure_node_ids=(
                        self.sections_by_label["2"].structure_node_id,
                        self.sections_by_label["1"].structure_node_id,
                    ),
                ),
            )
        )
        self.assertEqual(first_result.effective_snapshot.revision_number, 1)

        with self.assertRaises(StructureRevisionRejected) as raised:
            apply_structure_patch(
                stale_patch,
                self.source_ir,
                self.base_tree,
                revisions,
            )
        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.STALE_BASE_REVISION,
        )

    def test_duplicate_patch_submission_is_idempotent(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        proposal = self._proposal(
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=new_parent.structure_node_id,
                    new_sibling_index=0,
                ),
            )
        )
        patch = validate_structure_patch_proposal(
            proposal,
            self.source_ir,
            self.base_tree,
            (),
        )
        first = apply_structure_patch(
            patch,
            self.source_ir,
            self.base_tree,
            (),
        )
        revisions = (first.revision,)
        duplicate = apply_structure_patch(
            patch,
            self.source_ir,
            self.base_tree,
            revisions,
        )

        self.assertTrue(first.appended)
        self.assertFalse(duplicate.appended)
        self.assertEqual(duplicate.revision, first.revision)
        self.assertEqual(duplicate.effective_snapshot, first.effective_snapshot)
        self.assertEqual(len(revisions), 1)

    def test_proposal_has_no_apply_authority(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        proposal = self._proposal(
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=self.sections_by_label[
                        "1.2"
                    ].structure_node_id,
                    new_sibling_index=0,
                ),
            )
        )

        with self.assertRaisesRegex(TypeError, "proposals have no write access"):
            apply_structure_patch(  # type: ignore[arg-type]
                proposal,
                self.source_ir,
                self.base_tree,
                (),
            )

    def test_undo_restores_tree_but_preserves_append_only_history(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        apply_result, revisions = self._accept_and_apply(
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=self.sections_by_label[
                        "1.2"
                    ].structure_node_id,
                    new_sibling_index=0,
                ),
            )
        )
        undo_result = undo_structure_revision(
            self.source_ir,
            self.base_tree,
            revisions,
            expected_revision_number=apply_result.effective_snapshot.revision_number,
            expected_effective_snapshot_id=(
                apply_result.effective_snapshot.effective_snapshot_id
            ),
        )
        history = (*revisions, undo_result.revision)

        self.assertEqual(
            undo_result.revision.action,
            StructureRevisionAction.UNDO,
        )
        self.assertEqual(len(history), 2)
        self.assertEqual(
            undo_result.effective_snapshot.effective_tree_sha256,
            self.base_effective.effective_tree_sha256,
        )
        self.assertNotEqual(
            undo_result.effective_snapshot.effective_snapshot_id,
            self.base_effective.effective_snapshot_id,
        )
        self.assertEqual(
            undo_result.revision.undo_target_revision_id,
            apply_result.revision.revision_id,
        )
        replayed = validate_structure_revision_chain(
            self.source_ir,
            self.base_tree,
            history,
        )
        self.assertEqual(replayed, undo_result.effective_snapshot)
        self._assert_base_and_source_unchanged()

    def test_revision_replay_and_effective_identity_are_stable(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        result, revisions = self._accept_and_apply(
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=self.sections_by_label[
                        "1.2"
                    ].structure_node_id,
                    new_sibling_index=0,
                ),
            )
        )

        first_replay = validate_structure_revision_chain(
            self.source_ir,
            self.base_tree,
            revisions,
        )
        second_replay = validate_structure_revision_chain(
            self.source_ir,
            self.base_tree,
            revisions,
        )
        self.assertEqual(first_replay, second_replay)
        self.assertEqual(
            first_replay.effective_snapshot_id,
            result.effective_snapshot.effective_snapshot_id,
        )
        self.assertEqual(
            first_replay.effective_tree_sha256,
            result.effective_snapshot.effective_tree_sha256,
        )
        self._assert_base_and_source_unchanged()

    def test_revision_replays_after_same_source_is_rebuilt_later(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        result, revisions = self._accept_and_apply(
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=self.sections_by_label[
                        "1.2"
                    ].structure_node_id,
                    new_sibling_index=0,
                ),
            )
        )
        rebuilt_source_ir = self.source_ir.model_copy(
            update={"created_at": self.source_ir.created_at + timedelta(seconds=30)}
        )

        self.assertNotEqual(
            canonical_model_sha256(rebuilt_source_ir),
            canonical_model_sha256(self.source_ir),
        )
        replayed = validate_structure_revision_chain(
            rebuilt_source_ir,
            self.base_tree,
            revisions,
        )

        self.assertEqual(replayed, result.effective_snapshot)

    def test_revision_replay_rejects_changed_source_facts(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        _, revisions = self._accept_and_apply(
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=self.sections_by_label[
                        "1.2"
                    ].structure_node_id,
                    new_sibling_index=0,
                ),
            )
        )
        changed_source_ir = self.source_ir.model_copy(
            update={"adapter_version": "1.0.1"}
        )

        with self.assertRaises(StructureRevisionRejected) as raised:
            validate_structure_revision_chain(
                changed_source_ir,
                self.base_tree,
                revisions,
            )

        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.REVISION_CHAIN_INVALID,
        )

    def _proposal(
        self,
        operations: tuple[StructurePatchOperationV1, ...],
        revisions: tuple[StructureRevisionV1, ...] = (),
    ) -> StructurePatchProposalV1:
        return create_structure_patch_proposal(
            self.source_ir,
            self.base_tree,
            revisions,
            operations,
        )

    def _accept_and_apply(
        self,
        operations: tuple[StructurePatchOperationV1, ...],
        revisions: tuple[StructureRevisionV1, ...] = (),
    ) -> tuple[StructureApplyResultV1, tuple[StructureRevisionV1, ...]]:
        proposal = self._proposal(operations, revisions)
        patch = validate_structure_patch_proposal(
            proposal,
            self.source_ir,
            self.base_tree,
            revisions,
        )
        result = apply_structure_patch(
            patch,
            self.source_ir,
            self.base_tree,
            revisions,
        )
        self.assertTrue(result.appended)
        return result, (*revisions, result.revision)

    def _split_operation(
        self,
        target: StructureNode,
        ranges: tuple[tuple[int, int], ...],
    ) -> BoundarySplitStructureOperationV1:
        source_ref = target.source_refs[0]
        return BoundarySplitStructureOperationV1(
            structure_node_id=target.structure_node_id,
            parts=tuple(
                BoundarySplitPartV1(
                    source_spans=(
                        StructureSourceSpanV1(
                            source_ref=source_ref,
                            start_offset=start,
                            end_offset=end,
                        ),
                    )
                )
                for start, end in ranges
            ),
        )

    def _split_alpha_pair(
        self,
    ) -> tuple[
        StructureApplyResultV1,
        tuple[StructureNode, StructureNode],
        tuple[StructureRevisionV1, ...],
    ]:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        text = "Alpha one. Alpha two."
        split_at = text.index("Alpha two")
        result, revisions = self._accept_and_apply(
            (self._split_operation(target, ((0, split_at), (split_at, len(text)))),)
        )
        spans_by_id = {
            binding.structure_node_id: binding.source_spans
            for binding in result.effective_snapshot.node_source_spans
        }
        pair = tuple(
            sorted(
                (
                    node
                    for node in result.effective_snapshot.nodes
                    if node.identity_key.startswith("model:")
                    and node.source_refs == target.source_refs
                    and node.parent_structure_node_id
                    == target.parent_structure_node_id
                ),
                key=lambda node: spans_by_id[node.structure_node_id][0].start_offset,
            )
        )
        self.assertEqual(len(pair), 2)
        return result, (pair[0], pair[1]), revisions

    def _assert_base_and_source_unchanged(self) -> None:
        self.assertEqual(
            canonical_model_sha256(self.source_ir),
            self.source_hash_before,
        )
        self.assertEqual(
            canonical_model_sha256(self.base_tree),
            self.base_tree_hash_before,
        )


def _source_ir_fixture() -> SourceIRSnapshot:
    nodes: list[SourceNode] = []
    for source_order, raw_text in enumerate(_TEXTS):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=source_order,
            bbox=(80.0, 80.0 + source_order * 25.0, 520.0, 98.0 + source_order * 25.0),
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=_SOURCE_SHA256,
                    adapter_id=_ADAPTER_ID,
                    adapter_version=_ADAPTER_VERSION,
                    kind=SourceNodeKind.PARAGRAPH,
                    native_locator=locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
        )
    document_id = stable_document_id(_SOURCE_SHA256)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=_SOURCE_SHA256,
            adapter_id=_ADAPTER_ID,
            adapter_version=_ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=_SOURCE_SHA256,
        adapter_id=_ADAPTER_ID,
        adapter_version=_ADAPTER_VERSION,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=_CREATED_AT,
    )


def _structure_node_for_source(
    nodes: tuple[StructureNode, ...],
    source_node_id: str,
) -> StructureNode:
    return next(
        node for node in nodes if node.identity_key == f"source:{source_node_id}"
    )


def _node_by_id(
    nodes: tuple[StructureNode, ...],
    structure_node_id: str,
) -> StructureNode:
    return next(node for node in nodes if node.structure_node_id == structure_node_id)


def _children(
    nodes: tuple[StructureNode, ...],
    parent_structure_node_id: str,
) -> tuple[StructureNode, ...]:
    return tuple(
        node
        for node in nodes
        if node.parent_structure_node_id == parent_structure_node_id
    )


if __name__ == "__main__":
    unittest.main()
