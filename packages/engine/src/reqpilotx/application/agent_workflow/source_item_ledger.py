"""The shared, pure Document Map + SourceItem projection for app and plugin.

No database, model call or semantic reinterpretation. Stable source identity and
ownership determine the visible ledger and its source order.
"""
from __future__ import annotations

from typing import Any
from reqpilotx.application.errors import ApplicationError
from reqpilotx.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1, SourceRequirementBaselineArtifactV1, Requirementness,
    canonical_json_sha256,
)

SOURCE_ITEM_LEDGER_SCHEMA_VERSION = "source-item-ledger/v1"
REQIF_MAPPING_PROFILE_ID = "reqpilotx-reqif-1.2-draft/v1"


def _invalid_reference(message: str) -> ApplicationError:
    return ApplicationError(status_code=409, detail={
        "code": "source_requirement_reference_invalid", "message": message})


def project_source_item_ledger(
    *,
    document_map: DocumentMapArtifactV1,
    artifact: SourceRequirementBaselineArtifactV1,
) -> dict[str, Any]:
    """Build the one progressive hierarchy consumed by UI, export, and ReqIF.

    The immutable Document Map and source-item artifact remain separate authorities. This
    projection only decides how their identities appear together: a one-to-one textual
    clause may share one visible row, while a table or one-to-many scope remains a
    structure row with source-item children.
    """

    nodes_by_id = {item.node_id: item for item in document_map.nodes}
    records_by_node: dict[str, list[Any]] = {}
    for record in artifact.records:
        if record.node_id not in nodes_by_id:
            raise _invalid_reference(
                "source item ledger record points to an unknown document structure node"
            )
        records_by_node.setdefault(record.node_id, []).append(record)
    structure_parent_ids = {
        item.parent_id for item in document_map.nodes if item.parent_id is not None
    }

    rows: dict[str, dict[str, Any]] = {}
    positions: dict[str, tuple[int, float, float, int, int]] = {}
    child_candidates: dict[str, list[str]] = {}
    projected_item_ids: set[str] = set()

    for node in document_map.nodes:
        node_records = sorted(
            records_by_node.get(node.node_id, []),
            key=lambda item: item.source_order,
        )
        logical_table_scope_ids = tuple(
            item.logical_table_scope_id
            for item in node.content_blocks
            if item.kind == "LOGICAL_TABLE" and item.logical_table_scope_id
        )
        fused_record = (
            node_records[0]
            if can_fuse_structure_and_source_item(
                logical_table_scope_ids=logical_table_scope_ids,
                records=node_records,
                has_structure_children=node.node_id in structure_parent_ids,
            )
            else None
        )
        rows[node.node_id] = {
            "row_id": node.node_id,
            "parent_row_id": node.parent_id,
            "child_row_ids": [],
            "row_role": "DOCUMENT_STRUCTURE",
            "structure_node_id": node.node_id,
            "source_item_id": (
                fused_record.source_record_id if fused_record is not None else None
            ),
            "logical_table_scope_ids": list(logical_table_scope_ids),
            "is_table_structure": bool(logical_table_scope_ids),
            "is_one_to_one_item": fused_record is not None,
            "processing_state": "ITEMS_READY" if node_records else "STRUCTURE_ONLY",
            "direct_source_item_count": len(node_records),
            "requirement_count": sum(
                item.requirementness is Requirementness.REQUIREMENT
                for item in node_records
            ),
            "non_requirement_count": sum(
                item.requirementness is Requirementness.NON_REQUIREMENT
                for item in node_records
            ),
        }
        positions[node.node_id] = document_node_position(node)
        if node.parent_id is not None:
            child_candidates.setdefault(node.parent_id, []).append(node.node_id)
        if fused_record is not None:
            projected_item_ids.add(fused_record.source_record_id)

    for record in artifact.records:
        if record.source_record_id in projected_item_ids:
            continue
        row_id = record.source_record_id
        if row_id in rows:
            raise _invalid_reference("source item ledger row identity collided")
        rows[row_id] = {
            "row_id": row_id,
            "parent_row_id": record.node_id,
            "child_row_ids": [],
            "row_role": "SOURCE_ITEM",
            "structure_node_id": record.node_id,
            "source_item_id": record.source_record_id,
            "logical_table_scope_ids": (
                [record.logical_table_scope_id]
                if record.logical_table_scope_id
                else []
            ),
            "is_table_structure": False,
            "is_one_to_one_item": False,
            "processing_state": "CLASSIFIED",
            "direct_source_item_count": 1,
            "requirement_count": int(
                record.requirementness is Requirementness.REQUIREMENT
            ),
            "non_requirement_count": int(
                record.requirementness is Requirementness.NON_REQUIREMENT
            ),
        }
        positions[row_id] = source_item_position(record)
        child_candidates.setdefault(record.node_id, []).append(row_id)
        projected_item_ids.add(record.source_record_id)

    for parent_id, child_ids in child_candidates.items():
        if parent_id not in rows:
            raise _invalid_reference("source item ledger parent row does not resolve")
        ordered = sorted(
            child_ids,
            key=lambda value: (*positions[value], value),
        )
        if len(ordered) != len(set(ordered)):
            raise _invalid_reference("source item ledger contains duplicate children")
        rows[parent_id]["child_row_ids"] = ordered

    expected_item_ids = {item.source_record_id for item in artifact.records}
    if projected_item_ids != expected_item_ids:
        raise _invalid_reference("source item ledger does not cover every source item")
    if any(
        row["parent_row_id"] is not None and row["parent_row_id"] not in rows
        for row in rows.values()
    ):
        raise _invalid_reference("source item ledger contains an unresolved parent")

    payload = {
        "schema_version": SOURCE_ITEM_LEDGER_SCHEMA_VERSION,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "baseline_artifact_id": artifact.artifact_id,
        "baseline_artifact_sha256": artifact.artifact_sha256,
        "root_row_ids": list(document_map.root_node_ids),
        "rows": [rows[item.node_id] for item in document_map.nodes]
        + [
            rows[item.source_record_id]
            for item in artifact.records
            if item.source_record_id in rows
            and rows[item.source_record_id]["row_role"] == "SOURCE_ITEM"
        ],
        "interchange": {
            "target": "REQIF_1_2",
            "mapping_profile": REQIF_MAPPING_PROFILE_ID,
            "mapping_status": "MODEL_READY_EXPORT_NOT_IMPLEMENTED",
            "hierarchy_mapping": "SPEC_HIERARCHY",
            "item_mapping": "SPEC_OBJECT",
        },
    }
    return {
        **payload,
        "ledger_sha256": canonical_json_sha256(payload),
    }


def can_fuse_structure_and_source_item(
    *,
    logical_table_scope_ids: tuple[str, ...],
    records: list[Any],
    has_structure_children: bool,
) -> bool:
    if logical_table_scope_ids or has_structure_children or len(records) != 1:
        return False
    record = records[0]
    source_form = getattr(record.source_form, "value", str(record.source_form))
    return (
        source_form in {"NUMBERED_CLAUSE", "NATIVE_LIST", "PARAGRAPH"}
        and not record.logical_table_scope_id
        and not record.table_fragments
    )


def source_item_display_label(
    record: Any,
    *,
    node: Any,
    has_structure_children: bool,
) -> str:
    """Return a user-facing label without leaking the Agent's terse boundary key.

    Text boundary candidates may deliberately use the visible ordinal (for example
    ``5.7``) as their stable label.  That is precise for the compiler but not useful
    in the workbench.  The source hierarchy already owns the human title, so the
    public projection composes the two identities without mutating the immutable
    candidate.  Direct text below a node that also owns children or a logical table
    is called out explicitly; otherwise a 1:1 clause reads like the source heading.
    """

    label = str(getattr(record, "label", "") or "").strip()
    node_label = str(getattr(node, "label", "") or "").strip()
    node_title = str(getattr(node, "title", "") or "").strip()
    source_form = getattr(record.source_form, "value", str(record.source_form))
    if source_form not in {"NUMBERED_CLAUSE", "NATIVE_LIST", "PARAGRAPH"} or label != node_label:
        return label or node_title or node_label

    has_logical_table = any(
        getattr(block, "kind", "") == "LOGICAL_TABLE"
        for block in tuple(getattr(node, "content_blocks", ()) or ())
    )
    if has_structure_children or has_logical_table:
        return f"{node_title or node_label} · 直接正文"

    if node_title.startswith(node_label):
        return node_title
    return " ".join(value for value in (node_label, node_title) if value) or label


def document_node_position(node: Any) -> tuple[int, float, float, int, int]:
    from reqpilotx.application.agent_workflow.source_locations import original_format, source_position
    native = (*tuple(getattr(node, "source_anchors", ()) or ()), *tuple(getattr(node, "source_content", ()) or ()))
    if native and original_format(getattr(native[0], "native_locator", None)) != "pdf":
        return (*min(source_position(value) for value in native), 0, node.order)
    anchors = tuple(getattr(node, "source_anchors", ()) or ())
    if anchors:
        anchor = min(
            anchors,
            key=lambda item: (item.page, item.bbox[1], item.bbox[0]),
        )
        return (anchor.page, float(anchor.bbox[1]), float(anchor.bbox[0]), 0, node.order)
    first_page = node.pages[0] if node.pages else 1_000_000
    return (first_page, float(node.order), 0.0, 0, node.order)


def source_item_position(record: Any) -> tuple[int, float, float, int, int]:
    from reqpilotx.application.agent_workflow.source_locations import original_format, source_position
    native = (*tuple(getattr(record, "image_bindings", ()) or ()), *tuple(getattr(record, "table_fragments", ()) or ()), *tuple(getattr(record, "text_bindings", ()) or ()))
    if native and original_format(getattr(native[0], "native_locator", None)) != "pdf":
        return (*min(source_position(value, offset=getattr(value, "start_offset", 0)) for value in native), 1, record.source_order)
    images = tuple(getattr(record, "image_bindings", ()) or ())
    if images:
        image = min(images, key=lambda item: (item.page, item.bbox[1], item.bbox[0]))
        return (image.page, float(image.bbox[1]), float(image.bbox[0]), 1, record.source_order)
    fragments = tuple(getattr(record, "table_fragments", ()) or ())
    if fragments:
        fragment = min(
            fragments,
            key=lambda item: (item.page, item.bbox[1], item.bbox[0]),
        )
        return (
            fragment.page,
            float(fragment.bbox[1]),
            float(fragment.bbox[0]),
            1,
            record.source_order,
        )
    text_bindings = tuple(getattr(record, "text_bindings", ()) or ())
    if text_bindings:
        binding = min(
            text_bindings,
            key=lambda item: (item.page, item.bbox[1], item.bbox[0], item.start_offset),
        )
        return (
            binding.page,
            float(binding.bbox[1]),
            float(binding.bbox[0]),
            1,
            record.source_order,
        )
    pages = tuple(getattr(record, "pages", ()) or ())
    first_page = pages[0] if pages else 1_000_000
    return (first_page, float(record.source_order), 0.0, 1, record.source_order)
