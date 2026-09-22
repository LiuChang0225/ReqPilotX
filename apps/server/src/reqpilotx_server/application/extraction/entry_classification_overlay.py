from __future__ import annotations

from reqpilotx_server.domain.entry_classification import (
    EntryClassificationSnapshotV1,
    EntryClassificationStatus,
    build_initial_entry_classification_snapshot,
)
from reqpilotx_server.domain.extraction.requirement_tree import RequirementTreeSnapshotV1


def apply_accepted_entry_classification_overlay(
    tree: RequirementTreeSnapshotV1,
    snapshot: EntryClassificationSnapshotV1,
) -> RequirementTreeSnapshotV1:
    """Overlay accepted three-class truth without changing tree evidence."""

    if snapshot.status is not EntryClassificationStatus.ACCEPTED:
        raise ValueError("entry classification snapshot must be accepted")

    expected = build_initial_entry_classification_snapshot(
        tree,
        policy_version=snapshot.policy_version,
        semantic_snapshot_id=snapshot.semantic_snapshot_id,
    )
    if (
        snapshot.project_id != expected.project_id
        or snapshot.sor_document_id != expected.sor_document_id
        or snapshot.source_sha256 != expected.source_sha256
        or snapshot.working_tree_id != expected.working_tree_id
        or snapshot.working_tree_sha256 != expected.working_tree_sha256
        or snapshot.effective_structure_id
        != expected.effective_structure_id
    ):
        raise ValueError(
            "entry classification snapshot does not match the working tree"
        )

    expected_entries = tuple(
        (
            entry.tree_node_id,
            entry.workflow_node_id,
            entry.projection_role,
        )
        for entry in expected.entries
    )
    actual_entries = tuple(
        (
            entry.tree_node_id,
            entry.workflow_node_id,
            entry.projection_role,
        )
        for entry in snapshot.entries
    )
    if actual_entries != expected_entries:
        raise ValueError(
            "entry classification snapshot does not cover the exact tree nodes"
        )

    classification_by_node_id = {
        entry.tree_node_id: entry.effective_type.value
        for entry in snapshot.entries
        if entry.effective_type is not None
    }
    if len(classification_by_node_id) != len(snapshot.entries):
        raise ValueError("accepted entry classifications must be complete")

    return tree.model_copy(
        update={
            "nodes": tuple(
                node.model_copy(
                    update={
                        "classification": classification_by_node_id[node.id]
                    }
                )
                if node.id in classification_by_node_id
                else node
                for node in tree.nodes
            )
        }
    )


def carry_entry_classifications_to_published_tree(
    working_tree: RequirementTreeSnapshotV1,
    published_tree: RequirementTreeSnapshotV1,
) -> RequirementTreeSnapshotV1:
    """Carry the accepted overlay across the immutable ID projection only."""

    working_node_ids = tuple(node.id for node in working_tree.nodes)
    published_node_ids = tuple(node.id for node in published_tree.nodes)
    if working_node_ids != published_node_ids:
        raise ValueError("published tree nodes do not match the working tree")
    working_by_id = {node.id: node for node in working_tree.nodes}
    return published_tree.model_copy(
        update={
            "nodes": tuple(
                node.model_copy(
                    update={
                        "classification": working_by_id[
                            node.id
                        ].classification
                    }
                )
                for node in published_tree.nodes
            )
        }
    )


__all__ = [
    "apply_accepted_entry_classification_overlay",
    "carry_entry_classifications_to_published_tree",
]
