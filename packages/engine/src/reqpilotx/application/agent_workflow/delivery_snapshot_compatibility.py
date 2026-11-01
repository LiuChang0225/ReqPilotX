"""Strict compatibility for immutable published Matrix snapshots.

Only known, source-proven legacy layout and missing image shape fields can
normalize. Original SHA and all source/semantic fields stay exact; published
snapshots are never rewritten.
"""

from __future__ import annotations

from copy import deepcopy
from reqpilotx.application.agent_workflow import source_requirement_baseline as core


def _normalize_cross_parent_derived_groups(snapshot):
    value = deepcopy(snapshot)
    digest = value.pop("delivery_sha256", None)
    if digest != core.canonical_json_sha256(value):
        raise ValueError("Published Matrix snapshot has an invalid delivery digest")
    base, groups, seen_ids = [], {}, set()
    in_derived_suffix = False
    previous_parent, completed_groups = None, set()
    for node in value["nodes"]:
        if node["node_id"] in seen_ids:
            raise ValueError("Matrix snapshot repeats a node identity")
        seen_ids.add(node["node_id"])
        if node.get("source_kind") != "SINGULAR_REQUIREMENT":
            if in_derived_suffix:
                raise ValueError("Legacy derived nodes are not an appended suffix")
            base.append(node)
            continue
        in_derived_suffix = True
        parent = node["parent_id"]
        if parent != previous_parent:
            if parent in completed_groups:
                raise ValueError("Legacy derived parent groups are interleaved")
            if previous_parent is not None:
                completed_groups.add(previous_parent)
        previous_parent = parent
        groups.setdefault(parent, []).append(node)
    value["nodes"] = base + [
        node for parent in sorted(groups) for node in groups[parent]
    ]
    by_id = {node["node_id"]: node for node in value["nodes"]}
    for table in value["logical_tables"]:
        old_ids, grouped = [], {}
        suffix = False
        for node_id in table.get("node_ids", []):
            node = by_id[node_id]
            if node.get("source_kind") == "SINGULAR_REQUIREMENT":
                suffix = True
                grouped.setdefault(node["parent_id"], []).append(node_id)
            else:
                if suffix:
                    raise ValueError(
                        "Legacy table derived links are not an appended suffix"
                    )
                old_ids.append(node_id)
        table["node_ids"] = old_ids + [
            node_id for parent in sorted(grouped) for node_id in grouped[parent]
        ]
    return value


def _restore_legacy_owned_image_shapes(
    snapshot, document_map, baseline, annotations=None
):
    if document_map is None or baseline is None:
        if any(node.get("figure_refs") for node in snapshot["nodes"]):
            raise ValueError(
                "Legacy image shape proof requires its actual Map and baseline"
            )
        return 0
    document_map = core.DocumentMapArtifactV1.model_validate(document_map)
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
    from reqpilotx.application.agent_workflow.source_item_delivery import (
        merge_source_item_related_images,
        source_item_image_export_refs,
    )

    records = {record.source_record_id: record for record in baseline.records}
    map_nodes = {node.node_id: node for node in document_map.nodes}
    geometry = {"kind", "page_number", "source_bbox", "location"}
    allowed = geometry | {"caption_authority"}
    count = 0
    checked_annotations = None
    for node in snapshot["nodes"]:
        refs = node.get("figure_refs", [])
        if not refs or all(geometry <= set(ref) for ref in refs):
            continue
        source_id = (node.get("source_item") or {}).get("source_item_id")
        record = records.get(source_id)
        if record is None:
            raise ValueError(
                "Legacy sparse image is not bound to an actual IMAGE SourceItem"
            )
        if record.image_bindings:
            core.validate_source_image_record_against_map(document_map, record)
            if len(refs) != len(record.image_bindings) or {
                ref.get("anchor_id") for ref in refs
            } != {binding.anchor_id for binding in record.image_bindings}:
                raise ValueError(
                    "Legacy sparse image anchors differ from the original record"
                )
            existing = source_item_image_export_refs(record)
        else:
            if annotations is None:
                raise ValueError(
                    "Legacy related image needs its exact registered annotation"
                )
            if checked_annotations is None:
                from reqpilotx.application.agent_workflow.source_item_visual import (
                    rebind_source_item_image_annotations,
                )

                checked_annotations = rebind_source_item_image_annotations(
                    document_map, baseline, annotations
                )
            related = (
                checked_annotations.get("records", {})
                .get(source_id, {})
                .get("related_images", [])
            )
            if refs != related:
                raise ValueError(
                    "Legacy related image differs from its registered source annotation"
                )
            existing = deepcopy(related)
        restored = merge_source_item_related_images(
            document_map,
            map_nodes[record.node_id],
            existing,
            refs,
            source_id,
        )
        if len(restored) != len(refs):
            raise ValueError("Legacy image normalization added a new image")
        for old, new in zip(refs, restored, strict=True):
            if set(new) - set(old) - allowed or any(
                key not in new or new[key] != value for key, value in old.items()
            ):
                raise ValueError(
                    "Legacy image normalization changed declared source or caption fields"
                )
            count += bool(set(new) - set(old))
        node["figure_refs"] = restored
    return count


def _legacy_pdf_structure_layout(current, document_map, baseline):
    """Reproduce only the pre-text-geometry PDF layout from its actual sources.

    This is an expected legacy value, not normalization of arbitrary published
    order. Full equality still checks every field after the exact old projection
    is reproduced. Office locations and unmeasured nodes retain their behavior.
    """
    if document_map is None or baseline is None:
        return None
    document_map = core.DocumentMapArtifactV1.model_validate(document_map)
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if document_map.source_format is not None:
        return None
    from reqpilotx.application.agent_workflow.source_locations import original_format

    # A missing source_format is a legacy implicit PDF declaration, not enough
    # on its own to grant the PDF-only layout exception. Prove the actual Map
    # and every baseline binding use PDF locators, including explicit locators.
    locations = [value for node in document_map.nodes
                 for value in (*node.source_content, *node.source_anchors, *node.evidence_anchors)]
    locations.extend(fragment for table in document_map.logical_tables for fragment in table.fragments)
    locations.extend(binding for record in baseline.records
                     for binding in (*record.table_fragments, *record.text_bindings, *record.image_bindings))
    if any(original_format(value.native_locator) != "pdf" for value in locations):
        return None
    source = current.get("source_item_baseline", {})
    if (source.get("artifact_sha256") != baseline.artifact_sha256
            or source.get("document_map_artifact_sha256") != document_map.artifact_sha256
            or baseline.document_map_artifact_sha256 != document_map.artifact_sha256
            or baseline.source_sha256 != document_map.source_sha256):
        raise ValueError("Legacy PDF order proof requires the exact Map and baseline")
    from reqpilotx.application.agent_workflow.source_item_ledger import (
        project_source_item_ledger, source_item_position,
    )

    ledger = project_source_item_ledger(document_map=document_map, artifact=baseline)
    rows = {row["row_id"]: row for row in ledger["rows"]}
    map_nodes = {node.node_id: node for node in document_map.nodes}
    records = {record.source_record_id: record for record in baseline.records}

    def legacy_position(row_id):
        row = rows[row_id]
        if row["row_role"] == "SOURCE_ITEM":
            return source_item_position(records[row["source_item_id"]])
        node = map_nodes[row["structure_node_id"]]
        if node.source_anchors:
            anchor = min(node.source_anchors,
                         key=lambda item: (item.page, item.bbox[1], item.bbox[0]))
            return (anchor.page, float(anchor.bbox[1]), float(anchor.bbox[0]), 0, node.order)
        page = node.pages[0] if node.pages else 1_000_000
        return (page, float(node.order), 0.0, 0, node.order)

    for row in rows.values():
        row["child_row_ids"] = sorted(
            row["child_row_ids"], key=lambda row_id: (*legacy_position(row_id), row_id)
        )
    ordered_ids = []

    def walk(row_id):
        ordered_ids.append(row_id)
        for child_id in rows[row_id]["child_row_ids"]:
            walk(child_id)

    for row_id in ledger["root_row_ids"]:
        walk(row_id)
    expected = deepcopy(current)
    nodes = {node["node_id"]: node for node in expected["nodes"]}
    base_ids = {node_id for node_id, node in nodes.items()
                if node.get("source_kind") != "SINGULAR_REQUIREMENT"}
    if base_ids != set(ordered_ids):
        raise ValueError("Legacy PDF order proof does not cover the exact Matrix nodes")
    derived = [node for node in expected["nodes"]
               if node.get("source_kind") == "SINGULAR_REQUIREMENT"]
    for ordinal, row_id in enumerate(ordered_ids):
        node = nodes[row_id]
        children = [child_id for child_id in node["child_ids"]
                    if nodes[child_id].get("source_kind") == "SINGULAR_REQUIREMENT"]
        node["child_ids"] = rows[row_id]["child_row_ids"] + children
        node["source_order"] = ordinal * 1000
        for index, child_id in enumerate(children, start=1):
            nodes[child_id]["source_order"] = node["source_order"] + index
    expected["nodes"] = [nodes[row_id] for row_id in ordered_ids] + derived
    return expected


def verify_published_delivery_snapshot(
    *,
    document_map,
    baseline,
    published_snapshot,
    current_snapshot,
    expected_snapshot_sha256,
    annotations=None,
):
    if core.canonical_json_sha256(published_snapshot) != expected_snapshot_sha256:
        raise ValueError(
            "Published Matrix snapshot SHA differs from the immutable graph"
        )
    before = _normalize_cross_parent_derived_groups(published_snapshot)
    current = _normalize_cross_parent_derived_groups(current_snapshot)
    restored = _restore_legacy_owned_image_shapes(
        before, document_map, baseline, annotations
    )
    legacy_pdf_order = False
    if before != current:
        legacy = _legacy_pdf_structure_layout(current, document_map, baseline)
        if legacy is None or before != legacy:
            raise ValueError(
                "Published Matrix differs beyond verified legacy ordering or original image shape"
            )
        legacy_pdf_order = True
    return {
        "schema_version": "reqpilot-published-snapshot-compatibility/v1",
        "status": "VERIFIED",
        "published_snapshot_sha256": expected_snapshot_sha256,
        "current_snapshot_sha256": core.canonical_json_sha256(current_snapshot),
        "restored_original_image_shape_count": restored,
        "source_and_semantic_fields_preserved": True,
        "within_parent_child_order_preserved": not legacy_pdf_order,
        "verified_legacy_pdf_structure_order": legacy_pdf_order,
        "published_snapshot_modified": False,
    }
