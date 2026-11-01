from __future__ import annotations

from reqpilotx_server.resource_paths import development_path

import hashlib
import json
import os
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
import re
import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from reqpilotx_server.application.agent_workflow.native_structure_bridge import (
    _document_source_sha256,
    _no_store_headers,
    _require_project_document,
)
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1,
    Requirementness,
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineValidationReceiptV1,
    canonical_json_sha256,
    compile_document_map_artifact,
    create_empty_source_requirement_baseline,
)
from reqpilotx_server.application.agent_workflow.source_item_ledger import (
    project_source_item_ledger as _project_source_item_ledger,
    can_fuse_structure_and_source_item as _can_fuse_structure_and_source_item,
    source_item_display_label as _source_item_display_label,
    document_node_position as _document_node_position,
    source_item_position as _source_item_position,
)

from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.transport import ResponseSink


SOURCE_REQUIREMENT_REFERENCE_MANIFEST_SCHEMA_VERSION = (
    "reqpilotx-source-requirement-reference-manifest/v1"
)
SOURCE_REQUIREMENT_FEEDBACK_REQUEST_SCHEMA_VERSION = (
    "source-requirement-feedback-request/v1"
)
SOURCE_REQUIREMENT_FEEDBACK_SCHEMA_VERSION = "source-requirement-feedback/v1"
SOURCE_ITEM_LEDGER_SCHEMA_VERSION = "source-item-ledger/v1"
REQIF_MAPPING_PROFILE_ID = "reqpilotx-reqif-1.2-draft/v1"

_PROTOTYPE_DIR = development_path('prototype')
REFERENCE_ROOT = _PROTOTYPE_DIR / "runtime-data" / "source-requirement-references"
FEEDBACK_ROOT = _PROTOTYPE_DIR / "runtime-data" / "source-requirement-feedback"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_MAX_FILE_BYTES = 32 * 1024 * 1024
_FEEDBACK_LOCK = threading.RLock()


class SourceRequirementFeedbackRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["source-requirement-feedback-request/v1"] = (
        SOURCE_REQUIREMENT_FEEDBACK_REQUEST_SCHEMA_VERSION
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_baseline_artifact_id: str = Field(
        pattern=r"^source-requirement-baseline-[0-9a-f]{32}$"
    )
    source_record_id: str = Field(pattern=r"^source-record-[0-9a-f]{32}$")
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("source-requirement feedback must be plain text")
        return normalized


def get_source_requirement_baseline_status(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    response.headers.update(_no_store_headers())
    document = _require_project_document(project_id, document_id)
    source_sha256 = _document_source_sha256(document)
    try:
        bundle = _load_effective_reference(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
    except ApplicationError as exc:
        if exc.status_code != 404:
            raise
        return {
            "schema_version": "source-requirement-baseline-status/v1",
            "status": "NOT_READY",
            "baseline_ready": False,
            "document_map_ready": False,
            "source_sha256": source_sha256,
            "message": "Agent 尚未发布原文需求清单切片。原文结构仍可正常浏览。",
        }
    artifact = bundle["baseline"]
    receipt = bundle["receipt"]
    scope_revision = _scope_revision_public_projection(bundle)
    return {
        "schema_version": "source-requirement-baseline-status/v1",
        "status": artifact.status,
        "baseline_ready": artifact.status != "EMPTY",
        "document_map_ready": True,
        "source_sha256": source_sha256,
        "document_map_artifact_id": artifact.document_map_artifact_id,
        "document_map_artifact_sha256": artifact.document_map_artifact_sha256,
        "baseline_artifact_id": artifact.artifact_id,
        "baseline_artifact_sha256": artifact.artifact_sha256,
        "covered_scope_ids": list(artifact.covered_scope_ids),
        "record_count": receipt.record_count,
        "requirement_count": receipt.requirement_count,
        "non_requirement_count": receipt.non_requirement_count,
        "authority": bundle["manifest"]["authority"],
        "legacy_parent_provision_result_role": "DIAGNOSTIC_ONLY",
        "scope_revision": scope_revision,
        "message": (
            f"Agent 已完成 {receipt.scope_count} 个代表性范围，形成 "
            f"{receipt.record_count} 条原文条目；当前结果可进入真实前端核对。"
        ),
    }


def get_source_requirement_baseline_outline(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    response.headers.update(_no_store_headers())
    document = _require_project_document(project_id, document_id)
    source_sha256 = _document_source_sha256(document)
    bundle = _load_effective_reference(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
    )
    artifact = bundle["baseline"]
    feedbacks = [item for item in _read_feedbacks(
        source_sha256=source_sha256,
        baseline_artifact_id=artifact.artifact_id,
    ) if item.get("project_id") == project_id and item.get("document_id") == document_id]
    from reqpilotx_server.application.agent_workflow.source_item_delivery_bridge import _reasoning
    reasoning = _reasoning(project_id, document_id, artifact)
    metadata = load_source_workbench_metadata(bundle, project_id, document_id, reasoning.get('decomposition'), response)
    return project_source_requirement_outline(bundle, feedbacks=feedbacks, metadata=metadata)


def load_source_workbench_metadata(bundle, project_id, document_id, decomposition, response):
    from reqpilotx_server.application.agent_workflow.source_item_workbench_metadata import project_workbench_metadata
    from reqpilotx_server.application.agent_workflow.native_structure_bridge import get_native_structure_table_preview
    annotations, artifact = bundle.get('image_annotations'), bundle['baseline']
    numbered_ids = {sid for sid, ann in (annotations or {}).get('records', {}).items()
                    if ann.get('source_number') or ann.get('source_numbers')}
    scopes = {record.logical_table_scope_id for record in artifact.records
              if record.source_record_id in numbered_ids and record.logical_table_scope_id}
    previews = {scope: get_native_structure_table_preview(project_id, document_id, scope, response) for scope in scopes}
    return project_workbench_metadata(bundle['document_map'], artifact, annotations=annotations,
        decomposition=decomposition, table_previews=previews)


def project_source_requirement_outline(bundle, *, feedbacks=(), metadata=None):
    """Shared workbench projection, independent of the artifact producer/store."""
    document_map = bundle["document_map"]
    artifact = bundle["baseline"]
    receipt = bundle["receipt"]
    source_sha256 = artifact.source_sha256
    feedbacks_by_record: dict[str, list[dict[str, Any]]] = {}
    for item in feedbacks:
        feedbacks_by_record.setdefault(str(item["source_record_id"]), []).append(item)
    records_by_node: dict[str, list[Any]] = {}
    for record in artifact.records:
        records_by_node.setdefault(record.node_id, []).append(record)
    node_by_id = {item.node_id: item for item in document_map.nodes}
    structure_parent_ids = {
        item.parent_id for item in document_map.nodes if item.parent_id is not None
    }
    nodes = []
    for node in document_map.nodes:
        node_records = records_by_node.get(node.node_id, [])
        nodes.append(
            {
                "node_id": node.node_id,
                "parent_id": node.parent_id,
                "order": node.order,
                "kind": node.kind,
                "label": node.label,
                "title": node.title,
                "pages": list(node.pages),
                "record_count": len(node_records),
                "requirement_count": sum(
                    item.requirementness is Requirementness.REQUIREMENT
                    for item in node_records
                ),
                "non_requirement_count": sum(
                    item.requirementness is Requirementness.NON_REQUIREMENT
                    for item in node_records
                ),
                "scope_status": "READY" if node_records else "NOT_IN_CURRENT_SLICE",
            }
        )
    source_item_ledger = _project_source_item_ledger(
        document_map=document_map,
        artifact=artifact,
    )
    return {
        "schema_version": "source-requirement-baseline-outline/v1",
        **({key: metadata[key] for key in ('scope_summaries', 'source_findings')} if metadata else {}),
        "source_sha256": source_sha256,
        "document_map": {
            "artifact_id": document_map.artifact_id,
            "artifact_sha256": document_map.artifact_sha256,
            "revision": document_map.revision,
            "status": document_map.status,
            "summary": document_map.document_summary,
            "root_node_ids": list(document_map.root_node_ids),
            "nodes": nodes,
        },
        "baseline": {
            "artifact_id": artifact.artifact_id,
            "artifact_sha256": artifact.artifact_sha256,
            "revision": artifact.revision,
            "status": artifact.status,
            "covered_scope_ids": list(artifact.covered_scope_ids),
            "authority": bundle["manifest"]["authority"],
        },
        "summary": {
            "record_count": receipt.record_count,
            "source_item_count": receipt.record_count,
            "requirement_count": receipt.requirement_count,
            "non_requirement_count": receipt.non_requirement_count,
            "feedback_count": len(feedbacks),
        },
        "records": [
            {
                "source_item_id": item.source_record_id,
                **(metadata['numbers'].get(item.source_record_id, {}) if metadata else {}),
                "source_record_id": item.source_record_id,
                "source_display_id": item.source_display_id,
                "requirement_display_id": item.requirement_display_id,
                "source_order": item.source_order,
                "node_id": item.node_id,
                "ancestor_path": list(item.ancestor_path),
                "logical_table_scope_id": item.logical_table_scope_id,
                "label": item.label,
                "display_label": _source_item_display_label(
                    item,
                    node=node_by_id[item.node_id],
                    has_structure_children=item.node_id in structure_parent_ids,
                ),
                "source_form": item.source_form.value,
                "has_owned_text": bool(item.text_bindings),
                **({"image_bindings": [binding.model_dump(mode="json") for binding in item.image_bindings]}
                   if item.image_bindings else {}),
                "pages": list(item.pages),
                "requirementness": item.requirementness.value,
                "requirement_kinds": [value.value for value in item.requirement_kinds],
                "non_requirement_role": (
                    item.non_requirement_role.value
                    if item.non_requirement_role is not None
                    else None
                ),
                "confidence": item.confidence,
                "evidence_state": item.evidence_state,
                "decision_summary": item.decision_summary,
                "decision_origin": item.decision_origin,
                "review_state": item.review_state,
                "feedback_count": len(feedbacks_by_record.get(item.source_record_id, [])),
            }
            for item in artifact.records
        ],
        "ledger": source_item_ledger,
        "activity": _public_run_activity(bundle["run_receipts"]),
        "scope_revision": _scope_revision_public_projection(bundle),
    }


def _scope_revision_public_projection(bundle: dict[str, Any]) -> dict[str, Any]:
    product = bundle.get("product_baseline_artifact")
    receipt = getattr(product, "scope_revision_receipt", None)
    predecessor = bundle.get("product_predecessor_pointer")
    if receipt is None:
        return {
            "available": False,
            "can_undo": False,
            "operation": "",
            "receipt_sha256": "",
            "affected_scope_ids": [],
            "comparison_scope_ids": [],
            "feedback_ids": [],
            "guidance_count": 0,
        }
    return {
        "available": True,
        "can_undo": isinstance(predecessor, dict),
        "operation": receipt.operation.value,
        "receipt_sha256": receipt.receipt_sha256,
        "affected_scope_ids": list(receipt.affected_scope_ids),
        "comparison_scope_ids": list(receipt.comparison_scope_ids),
        "feedback_ids": [
            item
            for item in receipt.feedback_ids
            if item.startswith("source-requirement-feedback-")
        ],
        "guidance_count": len(receipt.feedback_ids),
    }


def get_source_requirement_record(
    project_id: str,
    document_id: str,
    source_record_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    response.headers.update(_no_store_headers())
    document = _require_project_document(project_id, document_id)
    source_sha256 = _document_source_sha256(document)
    bundle = _load_effective_reference(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
    )
    from reqpilotx_server.application.source_runtime import resolve_source_record_path
    from reqpilotx_server.application.agent_workflow.native_structure_bridge import get_native_structure_table_preview
    feedbacks = [item for item in _read_feedbacks(source_sha256=source_sha256,
        baseline_artifact_id=bundle["baseline"].artifact_id)
        if item.get("source_record_id") == source_record_id
        and item.get("project_id") == project_id and item.get("document_id") == document_id]
    return project_source_requirement_record(bundle, source_record_id, feedbacks=feedbacks,
        source_path=resolve_source_record_path(document),
        table_preview=lambda scope: get_native_structure_table_preview(project_id, document_id, scope, response))


def project_source_requirement_record(bundle, source_record_id, *, feedbacks=(), source_path=None, table_preview):
    """Render immutable source bindings, images and context through the incumbent reader."""
    source_sha256 = bundle["baseline"].source_sha256
    document_map = bundle["document_map"]
    artifact = bundle["baseline"]
    record = next(
        (item for item in artifact.records if item.source_record_id == source_record_id),
        None,
    )
    if record is None:
        raise _not_found("source_requirement_record_not_found")
    node = next(item for item in document_map.nodes if item.node_id == record.node_id)
    structure_parent_ids = {
        item.parent_id for item in document_map.nodes if item.parent_id is not None
    }
    content_by_id = {
        content.content_id: content
        for node in document_map.nodes
        for content in node.source_content
    }
    shared_content = [
        content_by_id[item]
        for item in record.shared_source_content_ids
        if item in content_by_id
    ]
    image_reading = None
    annotation = {}
    if bundle.get("image_annotations"):
        from reqpilotx_server.application.agent_workflow.source_item_visual import rebind_source_item_image_annotations
        annotations = rebind_source_item_image_annotations(document_map, artifact, bundle["image_annotations"])
        annotation = annotations.get("records", {}).get(record.source_record_id, {})
        image_reading = annotation.get("visual_interpretation")
    related_images = []
    if record.table_fragments and any(a.anchor_kind in {"IMAGE", "FIGURE"} for a in node.source_anchors):
        from reqpilotx_server.application.agent_workflow.source_item_figure_relations import table_source_figure_relations
        preview = table_preview(record.logical_table_scope_id)
        try:
            relations = table_source_figure_relations(document_map, artifact, {record.logical_table_scope_id: preview})
        except (KeyError, ValueError) as exc:
            raise _conflict("source_item_image_scope_changed", "当前表格来源已更新，请刷新清单后重新读取。") from exc
        related_images = relations["records"].get(record.source_record_id, [])
    related_images = list({image["anchor_id"]: image for image in [*related_images, *annotation.get("related_images", [])]}.values())
    reading = {}
    if record.text_bindings and source_path:
        from reqpilotx_server.application.agent_workflow.source_item_reading import source_item_reading_projections
        reading = source_item_reading_projections(document_map, record, source_path)
    return {
        "schema_version": "source-requirement-record-detail/v1",
        "source_sha256": source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "baseline_artifact_id": artifact.artifact_id,
        "baseline_artifact_sha256": artifact.artifact_sha256,
        "record": {
            "source_item_id": record.source_record_id,
            **record.model_dump(mode="json", exclude={"reason_codes"}),
            "text_bindings": [{**binding.model_dump(mode="json"),
                **({"reading_projection": reading[binding.content_id]} if binding.content_id in reading else {})}
                for binding in record.text_bindings],
            "display_label": _source_item_display_label(
                record,
                node=node,
                has_structure_children=record.node_id in structure_parent_ids,
            ),
            **({"visual_interpretation": image_reading} if image_reading else {}),
            **({"related_images": related_images} if related_images else {}),
            "context_source_items": [
                {"source_item_id": context.source_record_id,
                 "source_display_id": context.source_display_id, "label": context.label,
                 "pages": list(context.pages),
                 "text_bindings": [binding.model_dump(mode="json") for binding in context.text_bindings]}
                for context in artifact.records if context.source_record_id in annotation.get("context_source_ids", [])
            ],
            "feedback_count": len(feedbacks),
            "internal_diagnostics_available": bool(record.reason_codes),
            "shared_source_content": [
                item.model_dump(mode="json") for item in shared_content
            ],
            "feedbacks": feedbacks,
        },
    }


def submit_source_requirement_feedback(
    project_id: str,
    document_id: str,
    payload: SourceRequirementFeedbackRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    response.headers.update(_no_store_headers())
    document = _require_project_document(project_id, document_id)
    source_sha256 = _document_source_sha256(document)
    bundle = _load_effective_reference(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
    )
    document_map = bundle["document_map"]
    artifact = bundle["baseline"]
    if (
        payload.expected_source_sha256 != source_sha256
        or payload.expected_document_map_artifact_sha256
        != document_map.artifact_sha256
        or payload.expected_baseline_artifact_id != artifact.artifact_id
    ):
        raise _conflict(
            "source_requirement_baseline_changed",
            "The source requirement baseline changed before feedback was saved.",
        )
    if not any(
        item.source_record_id == payload.source_record_id for item in artifact.records
    ):
        raise _not_found("source_requirement_record_not_found")
    record = _append_feedback(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        document_map_artifact_sha256=document_map.artifact_sha256,
        baseline_artifact_id=artifact.artifact_id,
        payload=payload,
    )
    return {
        "schema_version": "source-requirement-feedback-result/v1",
        "feedback": record,
        "delivery_message": (
            "指引已绑定当前原文条目；不会锁住其他条目或下一阶段。"
        ),
    }


def _load_effective_reference(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
) -> dict[str, Any]:
    base = _load_base_reference(project_id=project_id, document_id=document_id, source_sha256=source_sha256)
    from reqpilotx_server.application.agent_workflow.source_requirement_jobs import (
        resolve_effective_source_requirement_bundle,
    )

    try:
        return resolve_effective_source_requirement_bundle(
            project_id=project_id,
            document_id=document_id,
            base_bundle=base,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise _invalid_reference(
            "durable SourceItem baseline revision failed validation"
        ) from exc


def _load_base_reference(*, project_id: str, document_id: str, source_sha256: str) -> dict[str, Any]:
    """Select the current map first; a same-PDF historical ledger is not enough."""
    from types import SimpleNamespace
    from reqpilotx_server.application.agent_workflow.native_structure_bridge import get_native_structure_reference, _structure_loop_repository
    from reqpilotx_server.application.agent_workflow.native_source_graph import load_native_source_read_bundle
    repository = _structure_loop_repository()
    head = repository.get_working_revision_head(project_id=project_id, document_id=document_id,
        source_sha256=source_sha256) if repository is not None else None
    if head is None or head.validator_kind != "D2_HOST_COMPILER":
        native = load_native_source_read_bundle(project_id, document_id, source_sha256)
        if native is not None:
            return native
    public = get_native_structure_reference(project_id, document_id, SimpleNamespace(headers={}))
    from reqpilotx_server.application.agent_workflow.native_source_graph import load_native_source_graph, validate_native_source_graph
    graph = load_native_source_graph(project_id, document_id, source_sha256)
    is_native_graph = (graph is not None and (public.get('provenance') or {}).get('native_source_graph_sha256') == graph['artifact_sha256'])
    document_map = compile_document_map_artifact(public, revision=graph['document_map']['revision'] if is_native_graph else 1)
    if document_map.source_sha256 != source_sha256:
        raise _invalid_reference("current Document Map belongs to a different source")
    if graph is not None and graph["document_map"]["artifact_sha256"] == document_map.artifact_sha256:
        validated = validate_native_source_graph(graph)
        return {"root": None, "manifest": {"authority": "NATIVE_AGENT_SOURCE_GRAPH", "source_sha256": source_sha256,
                    "native_source_graph_sha256": graph["artifact_sha256"]},
            "document_map": validated["document_map"], "baseline": validated["baseline"], "receipt": validated["receipt"],
            "run_receipts": [graph["runtime_receipt"]], "image_annotations": graph.get("annotations"), "analysis_policy": graph.get("analysis_policy"),
            "native_publication": getattr(graph, 'publication', None)}
    try:
        historical = _load_latest_reference(source_sha256)
    except ApplicationError as exc:
        if exc.status_code != 404:
            raise
    else:
        accepted_map = historical["document_map"]
        if accepted_map.artifact_sha256 == document_map.artifact_sha256:
            return historical
        # The legacy native projection hash includes UI-only display fields.
        # A reading-view update does not replace a source map whose typed source,
        # hierarchy, cells, anchors and accepted decisions are exactly unchanged.
        excluded = {"artifact_id", "artifact_sha256", "native_projection_sha256"}
        if accepted_map.model_dump(mode="json", exclude=excluded) == document_map.model_dump(mode="json", exclude=excluded):
            return {**historical, "presentation_projection_sha256": document_map.native_projection_sha256}
    baseline, receipt = create_empty_source_requirement_baseline(document_map)
    return {"root": None, "manifest": {"authority": "CURRENT_DOCUMENT_MAP_EMPTY_SOURCE_LEDGER",
                "source_sha256": source_sha256}, "document_map": document_map,
            "baseline": baseline, "receipt": receipt, "run_receipts": []}


def _load_latest_reference(source_sha256: str) -> dict[str, Any]:
    if _SHA256_PATTERN.fullmatch(source_sha256) is None:
        raise _not_found("source_requirement_reference_not_found")
    root = REFERENCE_ROOT
    if not root.exists():
        raise _not_found("source_requirement_reference_not_found")
    if root.is_symlink() or not root.is_dir():
        raise _invalid_reference("source requirement reference root is unsafe")
    matches: list[tuple[int, str, Path]] = []
    for candidate in root.iterdir():
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        manifest_path = candidate / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        manifest = _read_json_file(manifest_path, root=candidate)
        if (
            manifest.get("schema_version")
            != SOURCE_REQUIREMENT_REFERENCE_MANIFEST_SCHEMA_VERSION
            or manifest.get("source_sha256") != source_sha256
        ):
            continue
        matches.append((manifest_path.stat().st_mtime_ns, candidate.name, candidate))
    if not matches:
        raise _not_found("source_requirement_reference_not_found")
    _, _, selected = max(matches)
    manifest_path = selected / "manifest.json"
    manifest_bytes = _resolved_safe_file(manifest_path, root=selected).read_bytes()
    return _load_reference_cached(
        str(selected),
        manifest_path.stat().st_mtime_ns,
        hashlib.sha256(manifest_bytes).hexdigest(),
    )


@lru_cache(maxsize=16)
def _load_reference_cached(
    raw_root: str,
    manifest_mtime_ns: int,
    manifest_file_sha256: str,
) -> dict[str, Any]:
    del manifest_mtime_ns, manifest_file_sha256
    root = Path(raw_root)
    manifest = _read_json_file(root / "manifest.json", root=root)
    _verify_manifest_files(root, manifest)
    document_map = DocumentMapArtifactV1.model_validate(
        _read_json_file(root / "document-map.json", root=root)
    )
    baseline = SourceRequirementBaselineArtifactV1.model_validate(
        _read_json_file(root / "source-requirement-baseline.json", root=root)
    )
    receipt = SourceRequirementBaselineValidationReceiptV1.model_validate(
        _read_json_file(root / "validation-receipt.json", root=root)
    )
    run_receipts = _read_json_array(root / "run-receipts.json", root=root)
    if (
        manifest.get("source_sha256") != document_map.source_sha256
        or baseline.source_sha256 != document_map.source_sha256
        or baseline.document_map_artifact_id != document_map.artifact_id
        or baseline.document_map_artifact_sha256 != document_map.artifact_sha256
        or receipt.artifact_id != baseline.artifact_id
        or receipt.artifact_sha256 != baseline.artifact_sha256
        or manifest.get("document_map_artifact_id") != document_map.artifact_id
        or manifest.get("document_map_artifact_sha256")
        != document_map.artifact_sha256
        or manifest.get("baseline_artifact_id") != baseline.artifact_id
        or manifest.get("baseline_artifact_sha256") != baseline.artifact_sha256
    ):
        raise _invalid_reference("source requirement reference lineage drifted")
    return {
        "root": root,
        "manifest": manifest,
        "document_map": document_map,
        "baseline": baseline,
        "receipt": receipt,
        "run_receipts": run_receipts,
    }


def _public_run_activity(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for index, value in enumerate(values, start=1):
        raw_scope_ids = value.get("scope_ids")
        scope_ids = (
            [item for item in raw_scope_ids if isinstance(item, str)]
            if isinstance(raw_scope_ids, list)
            else []
        )
        source_item_count = int(
            value.get("source_item_count") or value.get("record_count") or 0
        )
        result.append(
            {
                "sequence": index,
                "scope_id": str(value.get("logical_table_scope_id") or "")
                or ",".join(scope_ids),
                "state": "COMPLETED",
                "title": "Agent 已完成原文条目边界与需求判断",
                "summary": (
                    f"形成 {source_item_count} 条原文条目，"
                    f"使用 {str(value.get('model_id') or 'Codex Agent')} / "
                    f"{str(value.get('reasoning_effort') or 'default')}，"
                    f"耗时 {round(int(value.get('elapsed_milliseconds') or 0) / 1000)} 秒。"
                ),
                "tool_names": list(value.get("tool_names") or []),
                "elapsed_milliseconds": int(
                    value.get("elapsed_milliseconds") or 0
                ),
            }
        )
    return result


def _append_feedback(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    document_map_artifact_sha256: str,
    baseline_artifact_id: str,
    payload: SourceRequirementFeedbackRequestV1,
) -> dict[str, Any]:
    path = _feedback_path(source_sha256, baseline_artifact_id, create=True)
    identity = {
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "document_map_artifact_sha256": document_map_artifact_sha256,
        "baseline_artifact_id": baseline_artifact_id,
        "command_id": payload.command_id,
        "source_record_id": payload.source_record_id,
        "message": payload.message,
    }
    digest = canonical_json_sha256(identity)
    record = {
        "schema_version": SOURCE_REQUIREMENT_FEEDBACK_SCHEMA_VERSION,
        "feedback_id": f"source-requirement-feedback-{digest[:32]}",
        **identity,
        "status": "PENDING_AGENT",
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    encoded = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    with _FEEDBACK_LOCK:
        existing = _read_feedbacks(
            source_sha256=source_sha256,
            baseline_artifact_id=baseline_artifact_id,
        )
        same_command = [
            item for item in existing if item.get("command_id") == payload.command_id
            and item.get("project_id") == project_id and item.get("document_id") == document_id
        ]
        if same_command:
            if same_command[-1].get("feedback_id") != record["feedback_id"]:
                raise _conflict(
                    "source_requirement_feedback_command_conflict",
                    "The feedback command ID was already used with different content.",
                )
            return same_command[-1]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return record


def _read_feedbacks(
    *, source_sha256: str, baseline_artifact_id: str
) -> list[dict[str, Any]]:
    path = _feedback_path(source_sha256, baseline_artifact_id, create=False)
    if not path.exists():
        return []
    resolved = _resolved_safe_file(path, root=path.parent)
    result = []
    for line in resolved.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise _invalid_reference("source requirement feedback item is invalid")
        result.append(value)
    return result


def _carry_feedbacks_to_baseline(
    *,
    source_sha256: str,
    from_baseline_artifact_id: str,
    to_baseline_artifact_id: str,
    feedback_ids: tuple[str, ...],
    status: Literal["ACKNOWLEDGED", "CONSUMED"],
) -> None:
    """Carry selected feedback history across one immutable baseline revision."""

    selected_ids = tuple(sorted(set(feedback_ids)))
    if not selected_ids or from_baseline_artifact_id == to_baseline_artifact_id:
        raise _invalid_reference("source requirement feedback carry identity is invalid")
    with _FEEDBACK_LOCK:
        source_items = {
            str(item.get("feedback_id") or ""): item
            for item in _read_feedbacks(
                source_sha256=source_sha256,
                baseline_artifact_id=from_baseline_artifact_id,
            )
        }
        if any(item not in source_items for item in selected_ids):
            raise _invalid_reference("source requirement feedback carry source is missing")
        existing = {
            str(item.get("feedback_id") or ""): item
            for item in _read_feedbacks(
                source_sha256=source_sha256,
                baseline_artifact_id=to_baseline_artifact_id,
            )
        }
        records = []
        for feedback_id in selected_ids:
            if feedback_id in existing:
                if str(existing[feedback_id].get("status") or "") != status:
                    raise _invalid_reference(
                        "source requirement feedback carry status conflicts"
                    )
                continue
            value = dict(source_items[feedback_id])
            value.update(
                {
                    "baseline_artifact_id": to_baseline_artifact_id,
                    "status": status,
                    "carried_from_baseline_artifact_id": (
                        from_baseline_artifact_id
                    ),
                    "updated_at_utc": datetime.now(UTC).isoformat(),
                }
            )
            records.append(value)
        if not records:
            return
        path = _feedback_path(
            source_sha256,
            to_baseline_artifact_id,
            create=True,
        )
        encoded = "".join(
            json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for item in records
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _feedback_path(
    source_sha256: str, baseline_artifact_id: str, *, create: bool
) -> Path:
    if (
        _SHA256_PATTERN.fullmatch(source_sha256) is None
        or _SAFE_ID_PATTERN.fullmatch(baseline_artifact_id) is None
    ):
        raise _invalid_reference("source requirement feedback identity is invalid")
    root = FEEDBACK_ROOT
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise _invalid_reference("source requirement feedback root is unsafe")
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_root = root / source_sha256
    if source_root.exists() and (
        source_root.is_symlink() or not source_root.is_dir()
    ):
        raise _invalid_reference("source requirement feedback source root is unsafe")
    if create:
        source_root.mkdir(parents=False, exist_ok=True, mode=0o700)
    return source_root / f"{baseline_artifact_id}.jsonl"


def _verify_manifest_files(root: Path, manifest: dict[str, Any]) -> None:
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise _invalid_reference("source requirement manifest has no files")
    for item in files:
        if not isinstance(item, dict):
            raise _invalid_reference("source requirement manifest file is invalid")
        relative = Path(str(item.get("path") or ""))
        expected = str(item.get("sha256") or "")
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or _SHA256_PATTERN.fullmatch(expected) is None
        ):
            raise _invalid_reference("source requirement manifest path is invalid")
        resolved = _resolved_safe_file(root / relative, root=root)
        if hashlib.sha256(resolved.read_bytes()).hexdigest() != expected:
            raise _invalid_reference("source requirement manifest hash drifted")


def _read_json_file(path: Path, *, root: Path) -> dict[str, Any]:
    resolved = _resolved_safe_file(path, root=root)
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise _invalid_reference("source requirement JSON root is invalid")
    return value


def _read_json_array(path: Path, *, root: Path) -> list[dict[str, Any]]:
    resolved = _resolved_safe_file(path, root=root)
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _invalid_reference("source requirement JSON array is invalid")
    return value


def _resolved_safe_file(path: Path, *, root: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise _invalid_reference("source requirement reference file is unsafe")
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved.parent != resolved_root and resolved_root not in resolved.parents:
        raise _invalid_reference("source requirement reference escaped its root")
    if resolved.stat().st_size > _MAX_FILE_BYTES:
        raise _invalid_reference("source requirement reference file is too large")
    return resolved


def _not_found(code: str) -> ApplicationError:
    return ApplicationError(
        status_code=404,
        detail={"code": code, "message": "No current source requirement reference exists."},
    )


def _conflict(code: str, message: str) -> ApplicationError:
    return ApplicationError(status_code=409, detail={"code": code, "message": message})


def _invalid_reference(message: str) -> ApplicationError:
    return ApplicationError(
        status_code=409,
        detail={"code": "source_requirement_reference_invalid", "message": message},
    )


__all__ = [
    "SourceRequirementFeedbackRequestV1",
    "get_source_requirement_baseline_outline",
    "get_source_requirement_baseline_status",
    "get_source_requirement_record",
    "submit_source_requirement_feedback",
]
