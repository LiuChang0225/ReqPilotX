from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from pydantic import ValidationError

from app.domain.entry_classification import (
    EntryClassificationAgentSuggestionSetV1,
    EntryClassificationAgentSuggestionV1,
    EntryClassificationEntryV1,
    EntryClassificationSnapshotV1,
    EntryClassificationStatus,
    EntryClassificationType,
    UnclassifiedEntriesError,
    accept_entry_classification_snapshot,
    build_initial_entry_classification_snapshot,
    canonical_entry_classification_payload_sha256,
    revise_entry_classification_snapshot,
)
from app.domain.extraction.requirement_tree import (
    RequirementTreeMode,
    RequirementTreeNodeV1,
    RequirementTreeSnapshotV1,
    RequirementTreeSourceIdentity,
    RequirementTreeState,
    RequirementTreeStructureIdentity,
    canonical_requirement_tree_sha256,
)


class EntryClassificationDomainTests(unittest.TestCase):
    def test_projection_keeps_only_visible_business_entries_in_tree_order(self) -> None:
        tree = _tree(
            _node(
                "heading",
                child_ids=("proxy", "hidden", "system", "table"),
                projection_role="STRUCTURE",
                kind="SECTION",
                summary="customer heading text must not enter the payload",
            ),
            _node(
                "proxy",
                parent_id="heading",
                projection_role="STRUCTURE_PROXY",
                workflow_node_id=None,
                summary="customer proxy text must not enter the payload",
            ),
            _node(
                "hidden",
                parent_id="heading",
                projection_role="DIRECT_CONTENT",
                include_in_view=False,
            ),
            _node(
                "system",
                parent_id="heading",
                projection_role="SYSTEM",
                kind="SYSTEM",
            ),
            _node(
                "table",
                parent_id="heading",
                projection_role="WHOLE_TABLE_REVIEW",
                workflow_node_id="workflow-table",
                kind="TABLE",
            ),
        )

        snapshot = build_initial_entry_classification_snapshot(tree)

        self.assertEqual(
            [entry.tree_node_id for entry in snapshot.entries],
            ["proxy", "table"],
        )
        self.assertEqual(snapshot.entries[0].workflow_node_id, "proxy")
        self.assertEqual(snapshot.entries[1].workflow_node_id, "workflow-table")
        self.assertEqual(snapshot.status, EntryClassificationStatus.WORKING)
        self.assertEqual(snapshot.revision, 0)
        self.assertEqual(snapshot.entries[0].suggestion_source.value, "NONE")
        self.assertIsNone(snapshot.entries[0].suggested_type)
        self.assertIsNone(snapshot.entries[0].effective_type)
        self.assertNotIn("customer", snapshot.model_dump_json())

    def test_initial_projection_is_deterministic_and_has_no_mechanical_mapping(self) -> None:
        tree = _tree(
            _node(
                "requirement",
                projection_role="REQUIREMENT",
                classification="TECHNICAL",
                semantic_state="REQUIREMENT",
            )
        )

        first = build_initial_entry_classification_snapshot(tree)
        second = build_initial_entry_classification_snapshot(tree)

        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.payload_sha256, second.payload_sha256)
        self.assertEqual(
            first.payload_sha256,
            canonical_entry_classification_payload_sha256(first),
        )
        self.assertIsNone(first.entries[0].suggested_type)
        self.assertIsNone(first.entries[0].effective_type)

    def test_agent_suggestions_bind_exact_tree_order_and_remain_reviewable(self) -> None:
        tree = _tree(
            _node("technical", projection_role="DIRECT_CONTENT"),
            _node("information", projection_role="DIRECT_CONTENT"),
        )
        suggestions = EntryClassificationAgentSuggestionSetV1(
            source_sha256=tree.source_identity.source_sha256,
            working_tree_sha256=canonical_requirement_tree_sha256(tree),
            effective_structure_id=tree.structure_identity.effective_snapshot_id,
            policy_version="entry-classification-policy/v1",
            agent_id="reqpilotx-requirement-baseline-agent",
            kit_bundle_sha256="c" * 64,
            entries=(
                EntryClassificationAgentSuggestionV1(
                    workflow_node_id="technical",
                    suggested_type=EntryClassificationType.TECHNICAL,
                    reason_codes=("SYSTEM_CAPABILITY_OR_CONSTRAINT",),
                    evidence_refs=("node-" + "1" * 32,),
                    confidence=0.91,
                ),
                EntryClassificationAgentSuggestionV1(
                    workflow_node_id="information",
                    suggested_type=EntryClassificationType.INFORMATION,
                    reason_codes=("DESCRIPTIVE_CONTEXT_ONLY",),
                    evidence_refs=("node-" + "2" * 32,),
                    confidence=0.84,
                ),
            ),
        )

        snapshot = build_initial_entry_classification_snapshot(
            tree,
            agent_suggestions=suggestions,
        )

        self.assertEqual(snapshot.status, EntryClassificationStatus.WORKING)
        self.assertEqual(
            [entry.suggested_type for entry in snapshot.entries],
            [EntryClassificationType.TECHNICAL, EntryClassificationType.INFORMATION],
        )
        self.assertTrue(
            all(entry.effective_type is None for entry in snapshot.entries)
        )
        self.assertTrue(
            all(
                entry.suggestion_source.value == "MODEL"
                for entry in snapshot.entries
            )
        )
        self.assertEqual(
            snapshot.entries[0].suggestion_evidence_refs,
            ("node-" + "1" * 32,),
        )

        accepted = accept_entry_classification_snapshot(
            snapshot,
            snapshot_id="accepted-agent-suggestions",
            accepted_at="2026-08-24T12:00:00+00:00",
            accepted_by="engineer-1",
        )
        self.assertEqual(
            accepted.entries[0].effective_type,
            EntryClassificationType.TECHNICAL,
        )

    def test_agent_suggestions_reject_stale_or_reordered_tree_identity(self) -> None:
        tree = _tree(
            _node("first", projection_role="DIRECT_CONTENT"),
            _node("second", projection_role="DIRECT_CONTENT"),
        )
        suggestions = EntryClassificationAgentSuggestionSetV1(
            source_sha256=tree.source_identity.source_sha256,
            working_tree_sha256=canonical_requirement_tree_sha256(tree),
            effective_structure_id=tree.structure_identity.effective_snapshot_id,
            policy_version="entry-classification-policy/v1",
            agent_id="reqpilotx-requirement-baseline-agent",
            kit_bundle_sha256="d" * 64,
            entries=(
                EntryClassificationAgentSuggestionV1(
                    workflow_node_id="second",
                    suggested_type=EntryClassificationType.INFORMATION,
                    reason_codes=("DESCRIPTIVE_CONTEXT_ONLY",),
                    evidence_refs=("node-" + "2" * 32,),
                    confidence=0.8,
                ),
                EntryClassificationAgentSuggestionV1(
                    workflow_node_id="first",
                    suggested_type=EntryClassificationType.TECHNICAL,
                    reason_codes=("SYSTEM_CAPABILITY_OR_CONSTRAINT",),
                    evidence_refs=("node-" + "1" * 32,),
                    confidence=0.9,
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "do not match the tree"):
            build_initial_entry_classification_snapshot(
                tree,
                agent_suggestions=suggestions,
            )

    def test_override_clear_accept_and_edit_after_accept_are_append_only(self) -> None:
        initial = build_initial_entry_classification_snapshot(
            _tree(_node("entry", projection_role="DIRECT_CONTENT"))
        )
        working = revise_entry_classification_snapshot(
            initial,
            overrides={"entry": EntryClassificationType.TECHNICAL},
            snapshot_id="snapshot-1",
            created_at="2026-08-07T01:00:00+00:00",
        )
        accepted = accept_entry_classification_snapshot(
            working,
            snapshot_id="snapshot-2",
            accepted_at="2026-08-07T01:01:00+00:00",
            accepted_by="engineer-1",
        )
        reopened = revise_entry_classification_snapshot(
            accepted,
            overrides={"entry": None},
            snapshot_id="snapshot-3",
            created_at="2026-08-07T01:02:00+00:00",
        )

        self.assertEqual(working.revision, 1)
        self.assertEqual(working.entries[0].effective_type, EntryClassificationType.TECHNICAL)
        self.assertEqual(accepted.revision, 2)
        self.assertEqual(accepted.status, EntryClassificationStatus.ACCEPTED)
        self.assertEqual(accepted.entries[0].effective_type, EntryClassificationType.TECHNICAL)
        self.assertEqual(reopened.revision, 3)
        self.assertEqual(reopened.status, EntryClassificationStatus.WORKING)
        self.assertIsNone(reopened.entries[0].human_override)
        self.assertIsNone(reopened.entries[0].effective_type)
        self.assertEqual(reopened.supersedes_id, accepted.snapshot_id)

    def test_accept_rejects_unclassified_entries(self) -> None:
        initial = build_initial_entry_classification_snapshot(
            _tree(_node("entry", projection_role="TABLE_RECORD", kind="TABLE_RECORD"))
        )

        with self.assertRaises(UnclassifiedEntriesError) as caught:
            accept_entry_classification_snapshot(
                initial,
                snapshot_id="snapshot-accept",
                accepted_at="2026-08-07T01:00:00+00:00",
                accepted_by="engineer-1",
            )

        self.assertEqual(caught.exception.workflow_node_ids, ("entry",))

    def test_revision_rejects_unknown_or_duplicate_workflow_identity(self) -> None:
        with self.assertRaises(ValueError):
            build_initial_entry_classification_snapshot(
                _tree(
                    _node("first", projection_role="DIRECT_CONTENT", workflow_node_id="same"),
                    _node("second", projection_role="REQUIREMENT", workflow_node_id="same"),
                )
            )

        initial = build_initial_entry_classification_snapshot(
            _tree(_node("entry", projection_role="DIRECT_CONTENT"))
        )
        with self.assertRaises(KeyError):
            revise_entry_classification_snapshot(
                initial,
                overrides={"unknown": EntryClassificationType.INFORMATION},
                snapshot_id="snapshot-1",
                created_at="2026-08-07T01:00:00+00:00",
            )

    def test_accepted_snapshot_rejects_effective_type_that_disagrees_with_source(self) -> None:
        initial = build_initial_entry_classification_snapshot(
            _tree(_node("entry", projection_role="REQUIREMENT"))
        )
        working = revise_entry_classification_snapshot(
            initial,
            overrides={"entry": EntryClassificationType.TECHNICAL},
            snapshot_id="working-1",
            created_at="2026-08-07T01:00:00+00:00",
        )
        accepted = accept_entry_classification_snapshot(
            working,
            snapshot_id="accepted-2",
            accepted_at="2026-08-07T01:01:00+00:00",
            accepted_by="engineer-1",
        )
        payload = accepted.model_dump(mode="json")
        payload["entries"][0]["effective_type"] = "INFORMATION"

        with self.assertRaises(ValidationError):
            EntryClassificationSnapshotV1.model_validate(payload)

    def test_accept_and_changed_revision_reject_self_supersede(self) -> None:
        initial = build_initial_entry_classification_snapshot(
            _tree(_node("entry", projection_role="REQUIREMENT"))
        )
        with self.assertRaises(ValueError):
            revise_entry_classification_snapshot(
                initial,
                overrides={"entry": EntryClassificationType.TECHNICAL},
                snapshot_id=initial.snapshot_id,
                created_at="2026-08-07T01:00:00+00:00",
            )
        working = revise_entry_classification_snapshot(
            initial,
            overrides={"entry": EntryClassificationType.TECHNICAL},
            snapshot_id="working-1",
            created_at="2026-08-07T01:00:00+00:00",
        )
        with self.assertRaises(ValueError):
            accept_entry_classification_snapshot(
                working,
                snapshot_id=working.snapshot_id,
                accepted_at="2026-08-07T01:01:00+00:00",
                accepted_by="engineer-1",
            )

    def test_accept_requires_working_and_revision_no_op_returns_same_snapshot(self) -> None:
        initial = build_initial_entry_classification_snapshot(
            _tree(_node("entry", projection_role="REQUIREMENT"))
        )
        self.assertIs(
            revise_entry_classification_snapshot(
                initial,
                overrides={},
                snapshot_id=initial.snapshot_id,
                created_at="2026-08-07T01:00:00+00:00",
            ),
            initial,
        )
        working = revise_entry_classification_snapshot(
            initial,
            overrides={"entry": EntryClassificationType.INFORMATION},
            snapshot_id="working-1",
            created_at="2026-08-07T01:00:00+00:00",
        )
        accepted = accept_entry_classification_snapshot(
            working,
            snapshot_id="accepted-2",
            accepted_at="2026-08-07T01:01:00+00:00",
            accepted_by="engineer-1",
        )
        self.assertIs(
            revise_entry_classification_snapshot(
                accepted,
                overrides={"entry": EntryClassificationType.INFORMATION},
                snapshot_id=accepted.snapshot_id,
                created_at="2026-08-07T01:02:00+00:00",
            ),
            accepted,
        )
        with self.assertRaises(ValueError):
            accept_entry_classification_snapshot(
                accepted,
                snapshot_id="accepted-3",
                accepted_at="2026-08-07T01:02:00+00:00",
                accepted_by="engineer-1",
            )

    def test_reason_codes_are_bounded_machine_codes(self) -> None:
        with self.assertRaises(ValidationError):
            EntryClassificationEntryV1(
                tree_node_id="entry",
                workflow_node_id="entry",
                projection_role="REQUIREMENT",
                reason_codes=("customer free form text",),
            )
        with self.assertRaises(ValidationError):
            EntryClassificationEntryV1(
                tree_node_id="entry",
                workflow_node_id="entry",
                projection_role="REQUIREMENT",
                reason_codes=tuple(f"CODE_{index}" for index in range(33)),
            )


class EntryClassificationOverlayTests(unittest.TestCase):
    def test_overlay_requires_an_accepted_snapshot(self) -> None:
        tree = _tree(_node("entry", projection_role="REQUIREMENT"))
        working = revise_entry_classification_snapshot(
            build_initial_entry_classification_snapshot(tree),
            overrides={"entry": EntryClassificationType.TECHNICAL},
            snapshot_id="working-overlay-1",
            created_at="2026-08-07T06:00:00+00:00",
        )

        with self.assertRaises(ValueError):
            self._apply_overlay(tree, working)

    def test_overlay_requires_the_exact_classifiable_tree_node_set(self) -> None:
        tree = _tree(
            _node("first", projection_role="DIRECT_CONTENT"),
            _node("second", projection_role="REQUIREMENT"),
        )
        accepted = _accepted(
            tree,
            {
                "first": EntryClassificationType.TECHNICAL,
                "second": EntryClassificationType.INFORMATION,
            },
        )
        missing_entry = accepted.model_copy(
            update={"entries": accepted.entries[:1]}
        )

        with self.assertRaises(ValueError):
            self._apply_overlay(tree, missing_entry)

    def test_overlay_changes_only_reviewable_node_classification(self) -> None:
        tree = _tree(
            _node(
                "heading",
                child_ids=("first", "second"),
                projection_role="STRUCTURE",
                kind="SECTION",
                classification="HEADING_CLASSIFICATION",
                summary="immutable heading",
            ),
            _node(
                "first",
                parent_id="heading",
                projection_role="DIRECT_CONTENT",
                classification="LEGACY_SEMANTIC_LABEL",
                summary="immutable first content",
            ),
            _node(
                "second",
                parent_id="heading",
                projection_role="REQUIREMENT",
                classification="ANOTHER_LEGACY_LABEL",
                summary="immutable second content",
            ),
        )
        accepted = _accepted(
            tree,
            {
                "first": EntryClassificationType.TECHNICAL,
                "second": EntryClassificationType.NON_TECHNICAL,
            },
        )

        overlaid = self._apply_overlay(tree, accepted)

        self.assertEqual(tree.nodes[1].classification, "LEGACY_SEMANTIC_LABEL")
        self.assertEqual(overlaid.nodes[0].classification, "HEADING_CLASSIFICATION")
        self.assertEqual(overlaid.nodes[1].classification, "TECHNICAL")
        self.assertEqual(overlaid.nodes[2].classification, "NON_TECHNICAL")
        self.assertEqual(
            _tree_without_classification(overlaid),
            _tree_without_classification(tree),
        )

    def _apply_overlay(
        self,
        tree: RequirementTreeSnapshotV1,
        snapshot: EntryClassificationSnapshotV1,
    ) -> RequirementTreeSnapshotV1:
        try:
            from app.application.extraction.entry_classification_overlay import (
                apply_accepted_entry_classification_overlay,
            )
        except ImportError:
            self.fail("entry classification overlay service is missing")
        return apply_accepted_entry_classification_overlay(tree, snapshot)


def _tree(*nodes: RequirementTreeNodeV1) -> RequirementTreeSnapshotV1:
    return RequirementTreeSnapshotV1(
        source_identity=RequirementTreeSourceIdentity(
            project_id="project-1",
            document_id="document-1",
            source_ir_id="source-ir-1",
            source_sha256="a" * 64,
        ),
        structure_identity=RequirementTreeStructureIdentity(
            base_snapshot_id="base-1",
            effective_snapshot_id="effective-1",
            effective_tree_sha256="b" * 64,
            revision=3,
        ),
        mode=RequirementTreeMode.WORKING,
        state=RequirementTreeState.READY,
        root_node_ids=tuple(node.id for node in nodes if node.parent_id is None),
        nodes=nodes,
    )


def _node(
    node_id: str,
    *,
    parent_id: str | None = None,
    child_ids: tuple[str, ...] = (),
    projection_role: str,
    workflow_node_id: str | None = None,
    include_in_view: bool = True,
    kind: str = "CLAUSE",
    summary: str = "content",
    classification: str = "NOT_CLASSIFIED",
    semantic_state: str = "NOT_CLASSIFIED",
) -> RequirementTreeNodeV1:
    return RequirementTreeNodeV1(
        id=node_id,
        parent_id=parent_id,
        child_ids=child_ids,
        kind=kind,
        depth=0 if parent_id is None else 1,
        source_order=0,
        label="label",
        summary=summary,
        workflow_node_id=workflow_node_id,
        structure_node_id=(
            parent_id if projection_role == "WHOLE_TABLE_REVIEW" else None
        ),
        projection_role=projection_role,
        semantic_state=semantic_state,
        classification=classification,
        include_in_view=include_in_view,
    )


def _accepted(
    tree: RequirementTreeSnapshotV1,
    overrides: dict[str, EntryClassificationType],
) -> EntryClassificationSnapshotV1:
    working = revise_entry_classification_snapshot(
        build_initial_entry_classification_snapshot(tree),
        overrides=overrides,
        snapshot_id="working-overlay",
        created_at="2026-08-07T06:00:00+00:00",
    )
    return accept_entry_classification_snapshot(
        working,
        snapshot_id="accepted-overlay",
        accepted_at="2026-08-07T06:01:00+00:00",
        accepted_by="LOCAL_USER",
    )


def _tree_without_classification(
    tree: RequirementTreeSnapshotV1,
) -> dict:
    payload = tree.model_dump(mode="json")
    for node in payload["nodes"]:
        node.pop("classification")
    return payload


if __name__ == "__main__":
    unittest.main()
