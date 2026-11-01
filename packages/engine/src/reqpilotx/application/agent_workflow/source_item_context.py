"""Expand declared customer-source context without creating new obligations.

All references resolve inside the supplied same-source Map/baseline. A visited
graph preserves indirect conditions and cycles once; no inferred relationship,
external document lookup, old response adoption or silent truncation occurs.
"""
from __future__ import annotations

from copy import deepcopy

from .source_requirement_baseline import canonical_json_sha256, validate_source_image_record_against_map


class SourceItemContextError(ValueError):
    pass


def build_analysis_context_index(document_map, baseline, table_previews, annotations, table_figures):
    if baseline.source_sha256 != document_map.source_sha256 or baseline.document_map_artifact_sha256 != document_map.artifact_sha256:
        raise SourceItemContextError("Customer context requires the same original Map and baseline")
    nodes = {node.node_id: node for node in document_map.nodes}
    records = {record.source_record_id: record for record in baseline.records}
    contents = {}
    for node in document_map.nodes:
        for content in node.source_content:
            if content.content_id in contents:
                raise SourceItemContextError("Ambiguous Map content identity")
            contents[content.content_id] = (node, content)
    previews = table_previews or {}
    if isinstance(previews, list):
        previews = {value["logical_table_scope_id"]: value for value in previews}
    annotation_records = (annotations or {}).get("records", {})
    if set(annotation_records) - set(records):
        raise SourceItemContextError("Customer context has an unknown or retired owner")
    if (annotations or {}).get("carry_forward", {}).get("stale_context_references"):
        raise SourceItemContextError("Retired customer context references require explicit reassessment")

    def content_fact(content_id):
        if content_id not in contents:
            raise SourceItemContextError("Shared customer content is unknown or from another document")
        node, content = contents[content_id]
        return {"role": "SHARED_CUSTOMER_SOURCE_CONTENT", "owner_node_id": node.node_id,
            "source_ref": {"kind": "TEXT", "content_id": content_id, "start_offset": 0, "end_offset": len(content.text)},
            **content.model_dump(mode="json", exclude_none=True)}

    graph, payloads = {}, {}
    for sid, record in records.items():
        refs = annotation_records.get(sid, {}).get("context_source_ids", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in records or ref == sid for ref in refs) or len(refs) != len(set(refs)):
            raise SourceItemContextError("Customer context IDs must be unique current same-document sources")
        graph[sid] = sorted(refs)
        payloads[sid] = {"shared_source_content": [content_fact(cid) for cid in record.shared_source_content_ids]}
    # Only referenced records need detailed expansion. An unrelated table may
    # legitimately use the pre-existing analysis visual/page fallback when no
    # readable table preview is available; context must not change that path.
    referenced_targets = {target for targets in graph.values() for target in targets}
    for sid, record in records.items():
        if sid not in referenced_targets:
            continue
        segments = []
        owned = {content.content_id: content for content in nodes[record.node_id].source_content}
        for binding in record.text_bindings:
            content = owned.get(binding.content_id)
            if content is None or binding.text != content.text[binding.start_offset:binding.end_offset]:
                raise SourceItemContextError("Referenced text differs from the original Map binding")
            segments.append({"kind": "TEXT", **binding.model_dump(mode="json", exclude_none=True),
                "source_ref": {"kind": "TEXT", "content_id": binding.content_id,
                    "start_offset": binding.start_offset, "end_offset": binding.end_offset}})
        shared_cells = []
        if record.table_fragments:
            preview = previews.get(record.logical_table_scope_id)
            if not preview or preview.get("source_sha256") != baseline.source_sha256:
                raise SourceItemContextError("Referenced table requires same-original previews")
            fragments = {fragment["fragment_id"]: fragment for fragment in preview["fragments"]}
            for binding in record.table_fragments:
                fragment = fragments.get(binding.fragment_id)
                if fragment is None:
                    raise SourceItemContextError("Referenced table fragment is unavailable")
                cells = {cell.get("cell_id", cell.get("id")): cell for cell in fragment["cells"]}
                if set(binding.cell_ids) - set(cells):
                    raise SourceItemContextError("Referenced original table cells are unavailable")
                for cid in binding.cell_ids:
                    cell = cells[cid]
                    segments.append({"kind": "TABLE_CELL", "page": fragment["page"],
                        "source_ref": {"kind": "TABLE_CELL", "table_id": binding.fragment_id, "cell_id": cid},
                        **deepcopy(cell)})
            wanted = set(record.shared_header_cell_ids) | set(record.shared_table_cell_ids)
            for fragment in fragments.values():
                for cell in fragment["cells"]:
                    cid = cell.get("cell_id", cell.get("id"))
                    if cid in wanted:
                        shared_cells.append({"kind": "TABLE_CELL", "page": fragment["page"],
                            "source_ref": {"kind": "TABLE_CELL", "table_id": fragment["fragment_id"], "cell_id": cid},
                            **deepcopy(cell)})
        if record.image_bindings:
            validate_source_image_record_against_map(document_map, record)
        ancestors, current = [], record.node_id
        while current:
            node = nodes[current]
            ancestors.append({"node_id": node.node_id, "label": node.label, "title": node.title,
                "source_content": [content.model_dump(mode="json") for content in node.source_content]})
            current = node.parent_id
        payloads[sid] = {"source_item_id": sid, "source_display_id": record.source_display_id,
            "source_sha256": baseline.source_sha256, "role": "REFERENCED_CUSTOMER_CONTEXT",
            "interpretation": "DECLARED_CONTEXT_RELATION_NOT_AN_ADDITIONAL_OBLIGATION",
            "source_segments": segments, "shared_table_context": shared_cells,
            "shared_source_content": [content_fact(cid) for cid in record.shared_source_content_ids],
            "image_refs": [value.model_dump(mode="json") for value in record.image_bindings] + deepcopy(table_figures.get(sid, [])),
            "visual_interpretation": deepcopy(annotation_records.get(sid, {}).get("visual_interpretation", {})),
            "document_context": list(reversed(ancestors)), "context_source_ids": graph[sid]}
    return graph, payloads


def expand_analysis_customer_context(source_item_id, context_index):
    graph, payloads = context_index
    visited, expanded, cycles = {source_item_id}, [], []
    stack = [(target, (source_item_id,)) for target in reversed(graph[source_item_id])]
    while stack:
        current, path = stack.pop()
        if current in path:
            cycles.append(list(path) + [current])
            continue
        if current in visited:
            continue
        visited.add(current)
        expanded.append(deepcopy(payloads[current]))
        stack.extend((target, (*path, current)) for target in reversed(graph[current]))
    value = {"direct_source_ids": graph[source_item_id], "source_items": expanded,
        "shared_source_content": deepcopy(payloads[source_item_id]["shared_source_content"]),
        "cycle_paths": cycles, "traversal": "FINITE_VISITED_SOURCE_GRAPH_NO_DUPLICATE_OBLIGATIONS"}
    # Avoid changing no-reference historical unit contracts merely for this fix.
    return {**value, "dependencies_sha256": canonical_json_sha256(value)} if expanded or value["shared_source_content"] else None
