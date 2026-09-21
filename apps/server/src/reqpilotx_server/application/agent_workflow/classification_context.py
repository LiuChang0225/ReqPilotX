"""Source-only semantic context shared by text, table and review classifiers."""

from __future__ import annotations

from typing import Any, Mapping

from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1,
    canonical_json_sha256,
)


def classification_scope_node_ids(document_map, node_ids):
    selected = set(node_ids)
    changed = True
    while changed:
        before = set(selected)
        selected.update(
            node.node_id for node in document_map.nodes if node.parent_id in selected
        )
        changed = before != selected
    return selected


def build_classification_context(
    document_map,
    *,
    node_ids,
    table_previews: Mapping[str, Any] | None = None,
    unavailable_tables=(),
):
    document_map = DocumentMapArtifactV1.model_validate(document_map)
    focus = classification_scope_node_ids(document_map, node_ids)
    previews = table_previews or {}
    # Full document text stays visible. The selected section's complete tables
    # are supplied beside it, so a sentence or row is not its semantic universe.
    value = {
        "schema_version": "source-item-semantic-context/v1",
        "source_sha256": document_map.source_sha256,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "focus_node_ids": sorted(focus),
        "document_summary": document_map.document_summary,
        "document_text": [
            {
                "node_id": node.node_id,
                "parent_id": node.parent_id,
                "label": node.label,
                "title": node.title,
                "ancestor_path": list(node.ancestor_path),
                "source_content": [
                    content.model_dump(mode="json") for content in node.source_content
                ],
                "logical_table_scope_ids": [
                    block.logical_table_scope_id
                    for block in node.content_blocks
                    if block.logical_table_scope_id
                ],
            }
            for node in document_map.nodes
        ],
        "complete_related_tables": [previews[key] for key in sorted(previews)],
        "unavailable_tables": list(unavailable_tables),
        "instruction": "Read the complete section, logical table and related source targets before classifying a frozen record with the current classification Skill/V1. This is source evidence, not a separate classifier or an expansion of the requested output scope.",
    }
    return {**value, "context_sha256": canonical_json_sha256(value)}
