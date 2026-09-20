from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.application.errors import ApplicationError
from app.application.extraction.contracts import (
    BoundaryCalibrationPreviewRequest,
)
from app.application.extraction.geometry_evidence import (
    PersistedGeometryEvidence,
    validated_pdf_page_geometry_descriptor,
)
from app.application.extraction.orchestrate_structure_agent import (
    apply_manual_structure_operations,
)
from app.application.extraction.revise_structure import (
    validate_structure_revision_chain,
)
from app.application.extraction.structure_workspace import (
    _map_structure_revision_rejection,
    _rebuild_structure_revision_context,
    _rebuild_validated_structure_revision_context,
    _serialize_structure_workbench_for_api,
    _structure_agent_job_payload,
    _validate_structure_mutation_binding,
)
from app.application.extraction.structure_presentation import (
    validated_structure_presentation_index,
)
from app.application.transport import ResponseSink
from app.domain.extraction.page_geometry import PdfPageGeometryIndexV1
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    SourceIRSnapshot,
    SourceIRStatus,
)
from app.domain.extraction.structure import (
    StructureNode,
    StructureNodeKind,
    StructureRelationKind,
    StructureSourceRefKind,
)
from app.domain.extraction.structure_revision import (
    BoundaryResegmentStructureOperationV1,
    BoundarySplitStructureOperationV1,
    EffectiveStructureSnapshotV1,
    StructureRevisionRejected,
)


BOUNDARY_CALIBRATION_PAGE_SCHEMA_VERSION = "boundary-calibration-page/v1"
BOUNDARY_CALIBRATION_PREVIEW_SCHEMA_VERSION = (
    "boundary-calibration-preview/v1"
)
_SUPPORTED_TEXT_KINDS = {
    StructureNodeKind.CLAUSE,
    StructureNodeKind.CONTENT,
    StructureNodeKind.CONTINUATION,
}


def build_boundary_calibration_page(
    *,
    page_number: int,
    source_ir: SourceIRSnapshot,
    effective: EffectiveStructureSnapshotV1,
    geometry_page: PdfPageGeometryIndexV1,
    presentations: Sequence[dict[str, Any]] = (),
    selected_node_id: str = "",
    editable: bool = False,
) -> dict[str, Any]:
    """Build a page-local, source-bound plan for visual boundary calibration.

    The plan contains source text only as an ephemeral projection. Every
    writable target remains a canonical SourceIR offset supplied by the
    verified PDF line index.
    """

    if page_number < 1 or geometry_page.page_number != page_number:
        raise ValueError("boundary calibration page identity is inconsistent")
    if (
        geometry_page.source_identity.source_ir_id != source_ir.source_ir_id
        or geometry_page.source_identity.source_sha256 != source_ir.source_sha256
        or effective.source_ir_id != source_ir.source_ir_id
        or effective.source_sha256 != source_ir.source_sha256
    ):
        raise ValueError("boundary calibration inputs have mixed source lineage")
    can_edit = bool(editable and source_ir.status is SourceIRStatus.COMPLETE)

    nodes_by_id = {
        node.structure_node_id: node for node in effective.nodes
    }
    children_by_parent: dict[str, list[str]] = {
        node_id: [] for node_id in nodes_by_id
    }
    for node in effective.nodes:
        parent_id = node.parent_structure_node_id
        if parent_id in children_by_parent:
            children_by_parent[parent_id].append(node.structure_node_id)
    for child_ids in children_by_parent.values():
        child_ids.sort(
            key=lambda node_id: (
                nodes_by_id[node_id].source_order,
                node_id,
            )
        )

    spans_by_node = {
        binding.structure_node_id: binding.source_spans
        for binding in effective.node_source_spans
    }
    source_nodes_by_id = {
        node.source_node_id: node for node in source_ir.nodes
    }
    presentation_by_id = {
        str(item.get("id") or item.get("structure_node_id") or ""): item
        for item in presentations
        if str(item.get("id") or item.get("structure_node_id") or "")
    }
    touching_non_parent_ids = {
        endpoint
        for relation in effective.relations
        if relation.kind is not StructureRelationKind.CONTAINS
        for endpoint in (
            relation.source_structure_node_id,
            relation.target_structure_node_id,
        )
    }
    sidecar_node_ids = {
        finding.structure_node_id
        for finding in effective.findings
        if finding.structure_node_id is not None
    }
    sidecar_node_ids.update(
        node_id
        for group in effective.bilingual_counterpart_groups
        for node_id in group.member_structure_node_ids
    )
    sidecar_node_ids.update(
        binding.table_structure_node_id
        for binding in effective.visual_table_cell_text_bindings
    )

    segment_by_node: dict[str, dict[str, Any]] = {}
    segments: list[dict[str, Any]] = []
    for node in effective.nodes:
        spans = spans_by_node.get(node.structure_node_id, ())
        page_spans = [
            span
            for span in spans
            if _source_ref_page(
                span.source_ref.kind,
                span.source_ref.source_id,
                source_nodes_by_id=source_nodes_by_id,
            )
            == page_number
        ]
        if not page_spans or children_by_parent[node.structure_node_id]:
            continue

        presentation = presentation_by_id.get(node.structure_node_id, {})
        lock_reason = _segment_lock_reason(
            node=node,
            spans=spans,
            page_number=page_number,
            source_nodes_by_id=source_nodes_by_id,
            touching_non_parent_ids=touching_non_parent_ids,
            sidecar_node_ids=sidecar_node_ids,
        )
        if (
            lock_reason is None
            and source_ir.status is not SourceIRStatus.COMPLETE
        ):
            lock_reason = "SOURCE_CAPTURE_NOT_COMPLETE"
        single_span = spans[0] if len(spans) == 1 else None
        line_regions = []
        text = ""
        source_id = ""
        start_offset: int | None = None
        end_offset: int | None = None
        if (
            single_span is not None
            and single_span.source_ref.kind
            is StructureSourceRefKind.SOURCE_NODE
        ):
            source_id = single_span.source_ref.source_id
            start_offset = single_span.start_offset
            end_offset = single_span.end_offset
            source_node = source_nodes_by_id.get(source_id)
            if source_node is not None:
                text = source_node.raw_text[start_offset:end_offset]
            line_regions = [
                _line_region(line)
                for line in geometry_page.text_lines
                if line.source_node_id == source_id
                and line.end_offset > start_offset
                and line.start_offset < end_offset
            ]
            if not line_regions and lock_reason is None:
                lock_reason = "NO_EXACT_LINE_GEOMETRY"

        fallback_bbox = _source_bbox(
            source_id,
            source_nodes_by_id=source_nodes_by_id,
            page_number=page_number,
        )
        bbox = _union_bbox(
            [
                tuple(float(value) for value in region["bbox"])
                for region in line_regions
            ]
        )
        if bbox is None:
            bbox = fallback_bbox

        path = _string_list(presentation.get("path"))
        label = str(
            presentation.get("label")
            or node.explicit_label
            or node.kind.value
        )
        segment = {
            "node_id": node.structure_node_id,
            "parent_node_id": node.parent_structure_node_id,
            "kind": node.kind.value.upper(),
            "label": label,
            "path": path or [label],
            "source_order": node.source_order,
            "source_id": source_id,
            "start_offset": start_offset,
            "end_offset": end_offset,
            "text": text,
            "bbox": list(bbox) if bbox is not None else [],
            "line_regions": line_regions,
            "editable": bool(can_edit and lock_reason is None),
            "locked_reason": lock_reason,
        }
        segments.append(segment)
        if single_span is not None:
            segment_by_node[node.structure_node_id] = segment

    segments.sort(
        key=lambda item: (
            int(item["source_order"]),
            int(item["start_offset"] or 0),
            str(item["node_id"]),
        )
    )

    boundaries: list[dict[str, Any]] = []
    for parent_id, sibling_ids in children_by_parent.items():
        for preceding_id, following_id in zip(
            sibling_ids,
            sibling_ids[1:],
        ):
            preceding = segment_by_node.get(preceding_id)
            following = segment_by_node.get(following_id)
            if preceding is None or following is None:
                continue
            if (
                preceding["source_id"] != following["source_id"]
                or not preceding["source_id"]
                or preceding["end_offset"] != following["start_offset"]
            ):
                continue
            combined_start = int(preceding["start_offset"])
            current_offset = int(preceding["end_offset"])
            combined_end = int(following["end_offset"])
            anchors = _anchors_for_range(
                geometry_page,
                source_id=str(preceding["source_id"]),
                start_offset=combined_start,
                end_offset=combined_end,
            )
            alternate_anchors = [
                anchor
                for anchor in anchors
                if int(anchor["offset"]) != current_offset
            ]
            locked_reason = (
                str(preceding["locked_reason"] or "")
                or str(following["locked_reason"] or "")
                or (None if alternate_anchors else "NO_ALTERNATE_LINE_ANCHOR")
            )
            boundaries.append(
                {
                    "boundary_id": (
                        f"resegment:{preceding_id}:{following_id}"
                    ),
                    "mode": "RESEGMENT",
                    "state": "CURRENT",
                    "parent_node_id": parent_id,
                    "preceding_node_id": preceding_id,
                    "following_node_id": following_id,
                    "target_node_id": None,
                    "source_id": preceding["source_id"],
                    "current_offset": current_offset,
                    "range_start": combined_start,
                    "range_end": combined_end,
                    "label": (
                        f"{preceding['label']} ↔ {following['label']}"
                    ),
                    "path": _shared_path(
                        preceding["path"],
                        following["path"],
                    ),
                    "anchors": anchors,
                    "editable": bool(can_edit and locked_reason is None),
                    "locked_reason": locked_reason,
                }
            )

    editable_pairs = {
        (
            str(boundary.get("preceding_node_id") or ""),
            str(boundary.get("following_node_id") or ""),
        )
        for boundary in boundaries
        if boundary.get("mode") == "RESEGMENT"
    }
    for preceding, following in zip(segments, segments[1:]):
        pair = (
            str(preceding["node_id"]),
            str(following["node_id"]),
        )
        if pair in editable_pairs:
            continue
        current_offset = preceding.get("end_offset")
        anchor = _display_boundary_anchor(
            preceding,
            page_width=geometry_page.page_width,
            page_height=geometry_page.page_height,
        )
        if (
            not isinstance(current_offset, int)
            or not str(preceding.get("source_id") or "")
            or anchor is None
        ):
            continue
        if preceding["parent_node_id"] != following["parent_node_id"]:
            locked_reason = "CROSS_PARENT_BOUNDARY"
        elif preceding["source_id"] != following["source_id"]:
            locked_reason = "CROSS_SOURCE_BOUNDARY"
        else:
            locked_reason = "NON_CONTIGUOUS_SOURCE_BOUNDARY"
        locked_reason = str(
            preceding.get("locked_reason")
            or following.get("locked_reason")
            or locked_reason
        )
        boundaries.append(
            {
                "boundary_id": (
                    f"display:{preceding['node_id']}:{following['node_id']}"
                ),
                "mode": "RESEGMENT",
                "state": "CURRENT_LOCKED",
                "parent_node_id": (
                    preceding["parent_node_id"]
                    if preceding["parent_node_id"]
                    == following["parent_node_id"]
                    else None
                ),
                "preceding_node_id": preceding["node_id"],
                "following_node_id": following["node_id"],
                "target_node_id": None,
                "source_id": preceding["source_id"],
                "current_offset": current_offset,
                "range_start": preceding["start_offset"],
                "range_end": following["end_offset"],
                "label": (
                    f"{preceding['label']} ↔ {following['label']}"
                ),
                "path": _shared_path(
                    preceding["path"],
                    following["path"],
                ),
                "anchors": [anchor],
                "editable": False,
                "locked_reason": locked_reason,
            }
        )

    selected_segment = segment_by_node.get(selected_node_id)
    if selected_segment is not None:
        split_anchors = _anchors_for_range(
            geometry_page,
            source_id=str(selected_segment["source_id"]),
            start_offset=int(selected_segment["start_offset"]),
            end_offset=int(selected_segment["end_offset"]),
        )
        if split_anchors:
            midpoint = (
                int(selected_segment["start_offset"])
                + int(selected_segment["end_offset"])
            ) / 2
            suggested = min(
                split_anchors,
                key=lambda anchor: abs(int(anchor["offset"]) - midpoint),
            )
            locked_reason = selected_segment["locked_reason"]
            boundaries.append(
                {
                    "boundary_id": f"split:{selected_node_id}",
                    "mode": "SPLIT",
                    "state": "PROPOSED",
                    "parent_node_id": selected_segment["parent_node_id"],
                    "preceding_node_id": None,
                    "following_node_id": None,
                    "target_node_id": selected_node_id,
                    "source_id": selected_segment["source_id"],
                    "current_offset": int(suggested["offset"]),
                    "range_start": selected_segment["start_offset"],
                    "range_end": selected_segment["end_offset"],
                    "label": f"在「{selected_segment['label']}」内新增边界",
                    "path": selected_segment["path"],
                    "anchors": split_anchors,
                    "editable": bool(can_edit and locked_reason is None),
                    "locked_reason": locked_reason,
                }
            )

    return {
        "schema_version": BOUNDARY_CALIBRATION_PAGE_SCHEMA_VERSION,
        "source_status": source_ir.status.value,
        "selected_node_id": selected_node_id or None,
        "binding": {
            "source_ir_id": source_ir.source_ir_id,
            "source_sha256": source_ir.source_sha256,
            "revision_number": effective.revision_number,
            "effective_snapshot_id": effective.effective_snapshot_id,
            "effective_tree_sha256": effective.effective_tree_sha256,
        },
        "page": {
            "number": page_number,
            "width": geometry_page.page_width,
            "height": geometry_page.page_height,
            "rotation_cw_degrees": geometry_page.rotation_cw_degrees,
            "coordinate_space": geometry_page.coordinate_space,
            "index_sha256": geometry_page.index_sha256,
        },
        "segments": segments,
        "boundaries": boundaries,
        "counts": {
            "segment_count": len(segments),
            "boundary_count": len(boundaries),
            "editable_boundary_count": sum(
                boundary["editable"] for boundary in boundaries
            ),
            "locked_boundary_count": sum(
                not boundary["editable"] for boundary in boundaries
            ),
        },
        "capabilities": {
            "can_split_at_exact_line": bool(
                can_edit
                and any(
                    boundary["mode"] == "SPLIT"
                    and boundary["editable"]
                    for boundary in boundaries
                )
            ),
            "can_resegment_adjacent_siblings": bool(
                can_edit
                and any(
                    boundary["mode"] == "RESEGMENT"
                    and boundary["editable"]
                    for boundary in boundaries
                )
            ),
            "supports_free_pixel_boundary": False,
            "supports_cross_page_boundary": False,
            "supports_table_boundary": False,
        },
    }


def get_document_boundary_calibration_page(
    project_id: str,
    document_id: str,
    page_number: int,
    response: ResponseSink,
    node_id: str = "",
) -> dict[str, Any]:
    """Return the verified geometry/Structure join for one editable PDF page."""

    from app.application.extraction.document_jobs import (
        _llm_extraction_result_matches_runtime,
    )
    from app.application.extraction.review_support import (
        _require_project_document,
    )
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    document = _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    job, payload, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=True,
    )
    if not _llm_extraction_result_matches_runtime(payload):
        raise _boundary_application_error(
            "boundary_runtime_binding_mismatch",
            "The completed result must be regenerated for this runtime.",
        )
    (
        (source_ir, base_tree, revisions),
        effective,
    ) = _rebuild_validated_structure_revision_context(
        document=document,
        agent=agent,
    )
    presentations, presentation_is_current = _boundary_presentations(
        source_ir=source_ir,
        effective=effective,
        payload=payload,
    )
    source_page_snapshot = _record(payload.get("source_page_snapshot"))
    descriptor = validated_pdf_page_geometry_descriptor(
        source_page_snapshot.get("geometry_index"),
        project_id=project_id,
        document_id=document_id,
        expected_source_sha256=source_ir.source_sha256,
    )
    if descriptor is None:
        raise _boundary_application_error(
            "boundary_geometry_unavailable",
            "Boundary calibration requires a verified PDF line geometry index.",
        )
    try:
        geometry_page = PersistedGeometryEvidence(descriptor).load_page(
            page_number
        )
    except KeyError as exc:
        raise _boundary_application_error(
            "boundary_page_not_found",
            "The requested PDF page has no verified geometry.",
            status_code=404,
        ) from exc
    except ValueError as exc:
        raise _boundary_application_error(
            "boundary_geometry_invalid",
            "The verified PDF page geometry failed contract validation.",
        ) from exc
    structure_editable = bool(
        str(job.get("status") or "").upper() == "COMPLETED"
        and agent.get("editable") is True
        and presentation_is_current
    )
    plan = build_boundary_calibration_page(
        page_number=page_number,
        source_ir=source_ir,
        effective=effective,
        geometry_page=geometry_page,
        presentations=presentations,
        selected_node_id=node_id,
        editable=structure_editable,
    )
    plan["binding"].update(
        {
            "geometry_index_id": str(
                descriptor.get("geometry_index_id") or ""
            ),
            "geometry_index_sha256": str(
                descriptor.get("geometry_index_sha256") or ""
            ),
        }
    )
    plan["extraction_status"] = {
        "source_capture": source_ir.status.value,
        "structure_revision": (
            "EDITABLE" if structure_editable else "READ_ONLY"
        ),
        "requirement_projection": str(
            _record(agent.get("rerun")).get("status") or "CURRENT"
        ).upper(),
    }
    return plan


def _boundary_presentations(
    *,
    source_ir: SourceIRSnapshot,
    effective: EffectiveStructureSnapshotV1,
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    """Build compact labels/paths without serializing the full Workbench."""

    index = validated_structure_presentation_index(
        payload.get("structure_presentation_index"),
        expected_source_sha256=source_ir.source_sha256,
        expected_structure_snapshot_id=effective.effective_snapshot_id,
    )
    persisted_nodes = (
        _record(index.get("nodes")) if index is not None else {}
    )
    nodes_by_id = {
        node.structure_node_id: node for node in effective.nodes
    }
    labels = {
        node_id: str(
            _record(persisted_nodes.get(node_id)).get("label")
            or node.explicit_label
            or node.kind.value
        )
        for node_id, node in nodes_by_id.items()
    }
    paths: dict[str, list[str]] = {}

    def path_for(node_id: str, visiting: frozenset[str] = frozenset()) -> list[str]:
        cached = paths.get(node_id)
        if cached is not None:
            return cached
        node = nodes_by_id[node_id]
        label = labels[node_id]
        parent_id = node.parent_structure_node_id
        if (
            parent_id in nodes_by_id
            and parent_id not in visiting
            and parent_id != node_id
        ):
            path = [
                *path_for(parent_id, visiting | {node_id}),
                label,
            ]
        else:
            path = [label]
        paths[node_id] = path
        return path

    return (
        [
            {
                "id": node_id,
                "label": labels[node_id],
                "path": path_for(node_id),
            }
            for node_id in nodes_by_id
        ],
        index is not None,
    )


def preview_document_boundary_calibration(
    project_id: str,
    document_id: str,
    payload: BoundaryCalibrationPreviewRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    """Validate one exact-line boundary edit without persisting or rerunning."""

    from app.application.extraction.document_jobs import (
        _llm_extraction_result_matches_runtime,
    )
    from app.application.extraction.review_support import (
        _require_project_document,
    )
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    document = _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    job, result, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=True,
    )
    if not _llm_extraction_result_matches_runtime(result):
        raise _boundary_application_error(
            "boundary_runtime_binding_mismatch",
            "The completed result must be regenerated for this runtime.",
        )
    source_ir, base_tree, revisions = _rebuild_structure_revision_context(
        document=document,
        agent=agent,
    )
    latest_workbench = _serialize_structure_workbench_for_api(
        project_id=project_id,
        document_id=document_id,
        document=document,
        job=job,
        payload=result,
        agent=agent,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=revisions,
    )
    _validate_structure_mutation_binding(
        payload,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=revisions,
        latest_structure_agent=agent,
        latest_structure_workbench=latest_workbench,
    )
    descriptor = validated_pdf_page_geometry_descriptor(
        _record(result.get("source_page_snapshot")).get("geometry_index"),
        project_id=project_id,
        document_id=document_id,
        expected_source_sha256=source_ir.source_sha256,
    )
    if (
        descriptor is None
        or str(descriptor.get("geometry_index_id") or "")
        != payload.geometry_index_id
        or str(descriptor.get("geometry_index_sha256") or "")
        != payload.geometry_index_sha256
    ):
        raise _boundary_application_error(
            "boundary_geometry_conflict",
            "The PDF geometry changed after this boundary edit was prepared.",
        )
    geometry_page = PersistedGeometryEvidence(descriptor).load_page(
        payload.page_number
    )
    effective = validate_structure_revision_chain(
        source_ir,
        base_tree,
        revisions,
    )
    selected_node_id = next(
        (
            operation.structure_node_id
            for operation in payload.operations
            if isinstance(operation, BoundarySplitStructureOperationV1)
        ),
        "",
    )
    plan = build_boundary_calibration_page(
        page_number=payload.page_number,
        source_ir=source_ir,
        effective=effective,
        geometry_page=geometry_page,
        presentations=_records(
            _record(latest_workbench.get("structure")).get("nodes")
        ),
        selected_node_id=selected_node_id,
        editable=True,
    )
    _validate_operations_against_plan(
        payload.operations,
        boundaries=_records(plan.get("boundaries")),
    )
    try:
        mutation = apply_manual_structure_operations(
            source_ir,
            base_tree,
            revisions,
            payload.operations,
            expected_revision_number=payload.expected_revision_number,
            expected_effective_snapshot_id=(
                payload.expected_effective_snapshot_id
            ),
        )
    except StructureRevisionRejected as exc:
        raise _map_structure_revision_rejection(
            exc,
            latest_structure_agent=agent,
            latest_structure_workbench=latest_workbench,
        ) from exc

    revision = mutation.apply_result.revision
    patch = revision.patch
    assert mutation.proposal is not None and patch is not None
    return {
        "schema_version": BOUNDARY_CALIBRATION_PREVIEW_SCHEMA_VERSION,
        "accepted": True,
        "persisted": False,
        "rerun_queued": False,
        "proposal_id": mutation.proposal.proposal_id,
        "patch_id": patch.patch_id,
        "preview_revision": revision.revision_number,
        "preview_revision_id": revision.revision_id,
        "preview_effective_snapshot_id": (
            mutation.apply_result.effective_snapshot.effective_snapshot_id
        ),
        "impact": mutation.apply_result.impact.model_dump(mode="json"),
        "operations": [
            operation.model_dump(mode="json")
            for operation in payload.operations
        ],
        "commit": {
            "url": str(
                _record(latest_workbench.get("actions")).get("commit_url")
                or ""
            ),
            "payload": {
                **payload.model_dump(
                    mode="json",
                    exclude={
                        "page_number",
                        "geometry_index_id",
                        "geometry_index_sha256",
                    },
                ),
                "schema_version": "structure-patch-request/v1",
            },
        },
    }


def _validate_operations_against_plan(
    operations: Sequence[
        BoundarySplitStructureOperationV1
        | BoundaryResegmentStructureOperationV1
    ],
    *,
    boundaries: Sequence[dict[str, Any]],
) -> None:
    for operation in operations:
        if isinstance(operation, BoundarySplitStructureOperationV1):
            if len(operation.parts) != 2:
                raise _boundary_application_error(
                    "boundary_preview_invalid",
                    "Visual boundary split preview requires exactly two parts.",
                    status_code=422,
                )
            first = operation.parts[0].source_spans
            second = operation.parts[1].source_spans
            if (
                len(first) != 1
                or len(second) != 1
                or first[0].source_ref != second[0].source_ref
                or first[0].end_offset != second[0].start_offset
            ):
                raise _boundary_application_error(
                    "boundary_preview_invalid",
                    "Visual boundary split must use one exact contiguous source.",
                    status_code=422,
                )
            boundary = next(
                (
                    item
                    for item in boundaries
                    if item.get("mode") == "SPLIT"
                    and item.get("target_node_id")
                    == operation.structure_node_id
                ),
                None,
            )
            offset = first[0].end_offset
        else:
            boundary = next(
                (
                    item
                    for item in boundaries
                    if item.get("mode") == "RESEGMENT"
                    and item.get("preceding_node_id")
                    == operation.preceding_structure_node_id
                    and item.get("following_node_id")
                    == operation.following_structure_node_id
                ),
                None,
            )
            offset = operation.new_boundary_offset
        anchors = {
            int(anchor.get("offset") or -1)
            for anchor in _records(
                boundary.get("anchors") if boundary is not None else ()
            )
        }
        if (
            boundary is None
            or boundary.get("editable") is not True
            or offset not in anchors
        ):
            raise _boundary_application_error(
                "boundary_anchor_rejected",
                "The requested boundary is not one of the current verified line anchors.",
                status_code=422,
            )


def _segment_lock_reason(
    *,
    node: StructureNode,
    spans: Sequence[Any],
    page_number: int,
    source_nodes_by_id: dict[str, Any],
    touching_non_parent_ids: set[str],
    sidecar_node_ids: set[str],
) -> str | None:
    if node.kind not in _SUPPORTED_TEXT_KINDS:
        return "UNSUPPORTED_STRUCTURE_KIND"
    if len(spans) != 1:
        return "MULTI_SOURCE_SPAN"
    span = spans[0]
    if span.source_ref.kind is not StructureSourceRefKind.SOURCE_NODE:
        return "NON_NATIVE_TEXT_SOURCE"
    source_node = source_nodes_by_id.get(span.source_ref.source_id)
    if (
        source_node is None
        or source_node.native_locator.format is not NativeDocumentFormat.PDF
        or source_node.native_locator.page_number != page_number
    ):
        return "CROSS_PAGE_OR_NON_PDF_SOURCE"
    if node.structure_node_id in touching_non_parent_ids:
        return "NON_PARENT_RELATION_BOUND"
    if node.structure_node_id in sidecar_node_ids:
        return "SOURCE_SIDECAR_BOUND"
    return None


def _anchors_for_range(
    geometry_page: PdfPageGeometryIndexV1,
    *,
    source_id: str,
    start_offset: int,
    end_offset: int,
) -> list[dict[str, Any]]:
    anchors: dict[int, dict[str, Any]] = {}
    for line in geometry_page.text_lines:
        if (
            line.source_node_id != source_id
            or line.end_offset <= start_offset
            or line.end_offset >= end_offset
        ):
            continue
        anchors[line.end_offset] = {
            "offset": line.end_offset,
            "line_id": (
                f"line:{source_id}:{line.start_offset}:{line.end_offset}"
            ),
            "bbox": [
                0.0,
                max(0.0, line.bbox[3] - 0.75),
                geometry_page.page_width,
                min(geometry_page.page_height, line.bbox[3] + 0.75),
            ],
            "source_line_bbox": list(line.bbox),
            "edge": "AFTER_LINE",
        }
    return [anchors[offset] for offset in sorted(anchors)]


def _line_region(line: Any) -> dict[str, Any]:
    return {
        "line_id": (
            f"line:{line.source_node_id}:{line.start_offset}:{line.end_offset}"
        ),
        "source_id": line.source_node_id,
        "start_offset": line.start_offset,
        "end_offset": line.end_offset,
        "bbox": list(line.bbox),
        "precision": "EXACT_LINE",
    }


def _display_boundary_anchor(
    segment: dict[str, Any],
    *,
    page_width: float,
    page_height: float,
) -> dict[str, Any] | None:
    region_boxes = [
        (region, bbox)
        for region in _records(segment.get("line_regions"))
        if (bbox := _record_bbox(region)) is not None
    ]
    if not region_boxes:
        return None
    last_region, bbox = max(
        region_boxes,
        key=lambda item: (
            int(item[0].get("end_offset") or -1),
            item[1][3],
        ),
    )
    current_offset = segment.get("end_offset")
    if not isinstance(current_offset, int):
        return None
    return {
        "offset": current_offset,
        "line_id": str(last_region.get("line_id") or ""),
        "bbox": [
            0.0,
            max(0.0, bbox[3] - 0.75),
            page_width,
            min(page_height, bbox[3] + 0.75),
        ],
        "source_line_bbox": list(bbox),
        "edge": "AFTER_SEGMENT",
    }


def _record_bbox(
    value: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    raw = value.get("bbox")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        bbox = tuple(float(item) for item in raw)
    except (TypeError, ValueError):
        return None
    if (
        not all(item == item and abs(item) != float("inf") for item in bbox)
        or bbox[2] <= bbox[0]
        or bbox[3] <= bbox[1]
    ):
        return None
    return bbox


def _source_ref_page(
    kind: StructureSourceRefKind,
    source_id: str,
    *,
    source_nodes_by_id: dict[str, Any],
) -> int | None:
    if kind is not StructureSourceRefKind.SOURCE_NODE:
        return None
    source_node = source_nodes_by_id.get(source_id)
    if source_node is None:
        return None
    return source_node.native_locator.page_number


def _source_bbox(
    source_id: str,
    *,
    source_nodes_by_id: dict[str, Any],
    page_number: int,
) -> tuple[float, float, float, float] | None:
    source_node = source_nodes_by_id.get(source_id)
    if (
        source_node is None
        or source_node.native_locator.page_number != page_number
    ):
        return None
    return source_node.native_locator.bbox


def _union_bbox(
    boxes: Sequence[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _shared_path(left: Any, right: Any) -> list[str]:
    shared: list[str] = []
    for left_item, right_item in zip(
        _string_list(left),
        _string_list(right),
    ):
        if left_item != right_item:
            break
        shared.append(left_item)
    return shared


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item).strip()]


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _boundary_application_error(
    code: str,
    message: str,
    *,
    status_code: int = 409,
) -> ApplicationError:
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    return ApplicationError(
        status_code=status_code,
        detail={"code": code, "message": message},
        headers=_ASSET_RESPONSE_HEADERS,
    )


__all__ = [
    "BOUNDARY_CALIBRATION_PAGE_SCHEMA_VERSION",
    "BOUNDARY_CALIBRATION_PREVIEW_SCHEMA_VERSION",
    "build_boundary_calibration_page",
    "get_document_boundary_calibration_page",
    "preview_document_boundary_calibration",
]
