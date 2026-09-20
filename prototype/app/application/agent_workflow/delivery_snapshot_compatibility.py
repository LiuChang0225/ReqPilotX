"""Strict compatibility for immutable published Matrix snapshots.

Only cross-parent derived-group layout and source-proven missing image shape
fields can normalize. Original SHA, fields and within-parent order stay exact.
"""

from __future__ import annotations

from copy import deepcopy
from app.application.agent_workflow import source_requirement_baseline as core


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
    from app.application.agent_workflow.source_item_delivery import (
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
                from app.application.agent_workflow.source_item_visual import (
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
    if before != current:
        raise ValueError(
            "Published Matrix differs beyond verified cross-parent ordering or original image shape"
        )
    return {
        "schema_version": "reqpilot-published-snapshot-compatibility/v1",
        "status": "VERIFIED",
        "published_snapshot_sha256": expected_snapshot_sha256,
        "current_snapshot_sha256": core.canonical_json_sha256(current_snapshot),
        "restored_original_image_shape_count": restored,
        "source_and_semantic_fields_preserved": True,
        "within_parent_child_order_preserved": True,
        "published_snapshot_modified": False,
    }
