from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from app.application.extraction.processing_pipeline import (
    LOCAL_STRUCTURE_PROJECTION_MODE,
)
from app.application.extraction.resolve_structure import (
    superseded_native_text_source_ids,
    visual_text_authority_pages,
)
from app.application.extraction.revise_structure import (
    validate_structure_revision_chain,
)
from app.application.extraction.requirement_tree import (
    REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION,
    attach_toc_navigation_entries,
    build_requirement_tree_snapshot,
)
from app.application.extraction.source_page_facts import (
    public_source_page_snapshot,
)
from app.application.extraction.structure_presentation import (
    STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION as _STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION,
    build_structure_bilingual_projection_sidecar,
    physical_source_reading_order_index,
    validated_structure_presentation_index,
)
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.extraction.requirement_tree import RequirementTreeState
from app.domain.extraction.structure import (
    StructureFinding,
    StructureTreeSnapshot,
)
from app.domain.extraction.structure_revision import (
    StructureNodeSourceSpansV1,
    StructureRevisionV1,
)


STRUCTURE_WORKBENCH_SCHEMA_VERSION = "structure-workbench/v0"
STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION = (
    _STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION
)


def serialize_structure_recovery_workbench(
    *,
    project_id: str,
    document_id: str,
    document: dict[str, Any],
    job: dict[str, Any] | None,
    state: RequirementTreeState,
    detail_code: str,
) -> dict[str, Any]:
    """Return the same tree contract when no trusted Structure can be projected.

    A completed but incompatible lineage is not an empty workspace.  The
    recovery snapshot keeps the user in the formal tree surface and exposes one
    truthful next step without inventing nodes from a flat requirement list.
    """

    job = job if isinstance(job, dict) else {}
    source = {
        "project_id": project_id,
        "document_id": document_id,
        "source_ir_id": "",
        "source_sha256": str(document.get("source_sha256") or ""),
        "preview_generation_id": str(document.get("preview_generation_id") or ""),
        "file_name": str(document.get("file_name") or document_id),
        "page_count": max(int(document.get("page_count") or 0), 0),
    }
    structure = {
        "base_snapshot_id": "",
        "base_canonical_sha256": "",
        "resolver_version": "",
        "revision": 0,
        "base_revision": 0,
        "effective_snapshot_id": "",
        "effective_tree_sha256": "",
        "revision_chain_sha256": "",
        "head_revision_id": None,
        "state": state.value,
        "editable": False,
        "nodes": [],
        "bilingual_projection": _serialize_bilingual_projection_sidecar(None),
    }
    workbench: dict[str, Any] = {
        "schema_version": STRUCTURE_WORKBENCH_SCHEMA_VERSION,
        "source": source,
        "runtime": {
            "preview_schema_version": "",
            "source_ir_adapter_version": "",
            "structure_resolver_version": "",
            "requirement_tree_projection_runtime_version": (
                REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
            ),
            "job_id": str(job.get("job_id") or "") or None,
        },
        "agent": {
            "status": str(job.get("status") or "IDLE").upper(),
            "document_health": "REQUIRES_ACTION",
            "business_phase": "recovery",
            "current_chapter": "",
            "current_page": 1,
            "elapsed_seconds": 0,
            "started_at_utc": str(job.get("started_at_utc") or ""),
            "finished_at_utc": str(job.get("finished_at_utc") or ""),
            "last_progress": "",
            "last_progress_en": "",
            "eta_min_seconds": 0,
            "eta_max_seconds": 0,
            "issue_count": 1,
            "rerunnable": True,
            "editable": False,
            "result_state": state.value,
            "stages": [],
            "job_progress": {},
        },
        "structure": structure,
        "requirements": [],
        "relations": [],
        "proposals": [],
        "revisions": [],
        "capabilities": {
            "can_edit_structure": False,
            "can_calibrate_structure_boundaries": False,
            "can_undo_revision": False,
            "can_optimize_structure": False,
            "can_atomize": False,
            "can_refine": False,
            "can_preview_publication": False,
            "can_publish_draft": False,
            "fixture_transport": False,
        },
        "workflow": {},
        "readiness": {
            "blocking_issue_codes": [
                "STRUCTURE_RESULT_REGENERATION_REQUIRED",
            ],
        },
        "issues": [
            {
                "code": "STRUCTURE_RESULT_REGENERATION_REQUIRED",
                "severity": "BLOCKING",
                "message": (
                    "The source file is still available, but the stored "
                    "structure result is unavailable or incompatible with "
                    "the current result contract. Regenerate the extraction "
                    "result with the current processing profile."
                ),
                "retry_action": "REGENERATE_EXTRACTION_RESULT",
            },
        ],
        "structure_inspection": _unavailable_structure_inspection(),
        "actions": {
            "reextract_url": (
                f"/api/projects/{quote(project_id, safe='')}/sor-documents/"
                f"{quote(document_id, safe='')}/extraction/start"
            ),
        },
        "recovery": {
            "reason_code": "RESULT_REGENERATION_REQUIRED",
            "detail_code": detail_code,
            "recommended_action": "REGENERATE_EXTRACTION_RESULT",
        },
        "result": {},
        "source_pages": {
            "schema_version": "source-page-fact-snapshot/v1",
            "state": "UNAVAILABLE",
            "page_count": 0,
        },
    }
    workbench["requirement_tree"] = build_requirement_tree_snapshot(
        workbench=workbench,
        payload={},
        forced_state=state,
    ).model_dump(mode="json")
    return workbench


def serialize_structure_workbench(
    *,
    project_id: str,
    document_id: str,
    document: dict[str, Any],
    job: dict[str, Any],
    payload: dict[str, Any],
    structure_agent: dict[str, Any],
    source_ir: SourceIRSnapshot | None = None,
    base_tree: StructureTreeSnapshot | None = None,
    revisions: tuple[StructureRevisionV1, ...] = (),
    runtime_source_ir_adapter_version: str = "",
    runtime_structure_resolver_version: str = "",
    enrichment_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one source-bound Agent result into the UI Workbench contract.

    This presenter never accepts or applies mutations.  The authoritative
    optimistic identity remains the source/base/effective lineage already
    validated by the Structure API.
    """

    base_identity = _dict(structure_agent.get("base_structure_identity"))
    effective_identity = _dict(structure_agent.get("effective_structure_identity"))
    raw_nodes = _dict_items(structure_agent.get("effective_nodes"))
    raw_relations = _dict_items(structure_agent.get("effective_relations"))
    raw_revisions = _dict_items(structure_agent.get("revision_chain"))
    if not revisions:
        revisions = tuple(
            StructureRevisionV1.model_validate(item) for item in raw_revisions
        )

    effective_snapshot = _effective_snapshot(
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=revisions,
    )
    source_index, page_count = _source_index(
        source_ir,
        structure=effective_snapshot,
    )
    durable_presentations: dict[str, dict[str, Any]] = {}
    persisted_bilingual_projection: dict[str, Any] | None = None
    if source_ir is None:
        durable_presentations, source_index, page_count = (
            _persisted_structure_presentation(
                payload,
                structure_agent=structure_agent,
            )
        )
        if (
            str(job.get("status") or "").upper() == "COMPLETED"
            and not durable_presentations
        ):
            return serialize_structure_recovery_workbench(
                project_id=project_id,
                document_id=document_id,
                document=document,
                job=job,
                state=RequirementTreeState.REQUIRES_REEXTRACTION,
                detail_code="PRESENTATION_INCOMPATIBLE",
            )
        persisted_bilingual_projection = _persisted_structure_bilingual_projection(
            payload,
            structure_agent=structure_agent,
        )
    item_index = _presentation_index(payload)
    requirement_ids_by_node = _requirement_ids_by_node(payload)
    findings = effective_snapshot.findings if effective_snapshot is not None else ()
    persisted_source_spans_by_node = _persisted_source_spans_by_node(
        structure_agent.get("effective_node_source_spans")
    )
    source_spans_by_node = (
        {
            binding.structure_node_id: tuple(
                span.model_dump(mode="json") for span in binding.source_spans
            )
            for binding in effective_snapshot.node_source_spans
        }
        if effective_snapshot is not None
        else persisted_source_spans_by_node
    )
    health_codes_by_node = _health_codes_by_node(findings)

    nodes_by_id = {
        str(node.get("structure_node_id") or ""): node
        for node in raw_nodes
        if str(node.get("structure_node_id") or "")
    }
    depths = _node_depths(nodes_by_id)
    labels = {
        node_id: _node_label(
            node,
            document=document,
            source_index=source_index,
            presentation=item_index.get(node_id),
            durable_presentation=durable_presentations.get(node_id),
        )
        for node_id, node in nodes_by_id.items()
    }
    paths = {
        node_id: _node_path(
            node_id,
            nodes_by_id=nodes_by_id,
            labels=labels,
        )
        for node_id in nodes_by_id
    }
    nodes = [
        _serialize_node(
            node,
            depth=depths[node_id],
            label=labels[node_id],
            path=paths[node_id],
            source_index=source_index,
            presentation=item_index.get(node_id),
            durable_presentation=durable_presentations.get(node_id),
            requirement_ids=requirement_ids_by_node.get(node_id, ()),
            health_codes=health_codes_by_node.get(node_id, ()),
            source_spans=source_spans_by_node.get(node_id, ()),
        )
        for node_id, node in nodes_by_id.items()
    ]
    nodes.sort(key=lambda item: (item["order"], item["id"]))
    nodes = attach_toc_navigation_entries(nodes, source_index=source_index)

    job_status = str(job.get("status") or "IDLE").upper()
    editable = bool(
        job_status == "COMPLETED" and structure_agent.get("editable") is True
    )
    serving_previous_result = bool(
        job_status == "COMPLETED"
        and structure_agent.get("editable") is False
        and structure_agent.get("read_only_reason")
        in {
            "structure_derivation_rerun_in_progress",
            "structure_derivation_rerun_not_complete",
        }
    )
    structure_state = _structure_state(
        job_status,
        editable=editable,
        serving_previous_result=serving_previous_result,
    )
    proposals = _serialize_proposals(
        structure_agent,
        revisions=revisions,
        nodes=nodes,
    )
    serialized_revisions = _serialize_revisions(
        revisions,
        source_ir=source_ir,
        base_tree=base_tree,
        nodes=nodes,
        proposal_summaries=_dict_items(structure_agent.get("proposal_summaries")),
    )
    requirements = _serialize_requirements(payload)
    effective_revision = int(effective_identity.get("revision_number") or 0)
    structure_inspection = _workbench_structure_inspection(
        payload,
        structure_agent=structure_agent,
        effective_revision=effective_revision,
        expected_source_sha256=str(
            base_identity.get("source_sha256")
            or payload.get("source_sha256")
            or document.get("source_sha256")
            or ""
        ),
        expected_source_ir_id=str(base_identity.get("source_ir_id") or ""),
        expected_structure_snapshot_id=str(base_identity.get("snapshot_id") or ""),
        expected_resolver_id=str(base_identity.get("resolver_id") or ""),
        expected_resolver_version=str(
            base_identity.get("resolver_version")
            or runtime_structure_resolver_version
            or ""
        ),
    )
    inspection_issue_count = int(
        _dict(structure_inspection.get("counts")).get(
            "structural_actionable_candidate_count"
        )
        or 0
    )
    issue_count = (
        sum(len(item["health_codes"]) for item in nodes)
        + sum(str(item.get("status") or "").upper() != "RESOLVED" for item in proposals)
        + inspection_issue_count
    )
    document_health = _document_health(
        job_status=job_status,
        structure_state=structure_state,
        issue_count=issue_count,
    )
    stage = _active_agent_stage(structure_agent, job)
    current_page = _current_page(job, payload, nodes)
    current_chapter = _current_chapter(payload, current_page=current_page)
    elapsed_seconds = _elapsed_seconds(job)
    progress = _job_progress(job)
    latest_progress = _last_progress(
        job_status=job_status,
        stage=stage,
        progress=progress,
    )
    base_url = (
        f"/api/projects/{quote(project_id, safe='')}/sor-documents/"
        f"{quote(document_id, safe='')}/extraction/structure"
    )
    persisted_source_ir_stage = _dict(_dict(payload.get("pipeline")).get("source_ir"))
    persisted_visual_strategy = _dict(payload.get("visual_strategy"))
    source_ir_adapter_version = str(
        (source_ir.adapter_version if source_ir is not None else "")
        or base_identity.get("source_ir_adapter_version")
        or persisted_source_ir_stage.get("version")
        or persisted_visual_strategy.get("source_ir_adapter_version")
        or ""
    )
    # Older document-requirement-agent/v0 snapshots predate the explicit
    # adapter field.  Runtime truth is still recoverable from SourceIR on final
    # results; partial snapshots remain read-only if that field is unavailable.
    if not source_ir_adapter_version:
        source_ir_adapter_version = str(
            payload.get("source_ir_adapter_version")
            or payload.get("pdf_source_ir_adapter_version")
            or runtime_source_ir_adapter_version
            or ""
        )
    structure_resolver_version = str(
        base_identity.get("resolver_version")
        or runtime_structure_resolver_version
        or ""
    )
    local_working_tree = (
        str(payload.get("mode") or "") == LOCAL_STRUCTURE_PROJECTION_MODE
    )
    enrichment = _serialize_enrichment_workflow(
        project_id=project_id,
        document_id=document_id,
        editable=editable,
        state=_dict(enrichment_state),
        review_session_required=not local_working_tree,
    )
    actions = {
        "commit_url": f"{base_url}/patch",
        "boundary_preview_url": f"{base_url}/patch/preview",
        "boundary_page_url_template": (
            f"{base_url.rsplit('/structure', 1)[0]}"
            "/boundary-calibration/pages/{page_number}"
        ),
        "undo_url": f"{base_url}/undo",
        "optimize_url": f"{base_url}/optimization",
        "optimization_status_url": f"{base_url}/optimization/status",
        **enrichment["actions"],
    }

    workbench = {
        "schema_version": STRUCTURE_WORKBENCH_SCHEMA_VERSION,
        "source": {
            "project_id": project_id,
            "document_id": document_id,
            "source_ir_id": str(base_identity.get("source_ir_id") or ""),
            "source_sha256": str(
                base_identity.get("source_sha256")
                or payload.get("source_sha256")
                or document.get("source_sha256")
                or ""
            ),
            "source_ir_adapter_version": source_ir_adapter_version,
            "preview_generation_id": str(
                payload.get("preview_generation_id")
                or document.get("preview_generation_id")
                or ""
            ),
            "file_name": str(document.get("file_name") or document_id),
            "page_count": max(
                page_count,
                max(
                    (
                        int(item.get("page_end") or item.get("page_start") or 0)
                        for item in nodes
                    ),
                    default=0,
                ),
            ),
        },
        "runtime": {
            "preview_schema_version": str(payload.get("schema_version") or ""),
            "source_ir_adapter_version": source_ir_adapter_version,
            "structure_resolver_version": structure_resolver_version,
            "requirement_tree_projection_runtime_version": (
                REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
            ),
            "job_id": str(job.get("job_id") or "") or None,
        },
        "agent": {
            "status": job_status,
            "document_health": document_health,
            "business_phase": stage,
            "current_chapter": current_chapter,
            "current_page": current_page,
            "elapsed_seconds": elapsed_seconds,
            "started_at_utc": str(job.get("started_at_utc") or ""),
            "finished_at_utc": str(job.get("finished_at_utc") or ""),
            "last_progress": latest_progress,
            "last_progress_en": latest_progress,
            "eta_min_seconds": 0,
            "eta_max_seconds": 0,
            "issue_count": issue_count,
            "rerunnable": job_status in {"COMPLETED", "FAILED", "INTERRUPTED", "IDLE"},
            "editable": editable,
            "result_state": structure_state,
            "stages": _dict_items(structure_agent.get("stages")),
            "job_progress": progress,
        },
        "structure": {
            "source_ir_id": str(base_identity.get("source_ir_id") or ""),
            "base_snapshot_id": str(base_identity.get("snapshot_id") or ""),
            "base_canonical_sha256": str(base_identity.get("canonical_sha256") or ""),
            "resolver_version": structure_resolver_version,
            "revision": int(effective_identity.get("revision_number") or 0),
            "base_revision": 0,
            "effective_snapshot_id": str(effective_identity.get("snapshot_id") or ""),
            "effective_tree_sha256": str(effective_identity.get("tree_sha256") or ""),
            "revision_chain_sha256": str(
                effective_identity.get("revision_chain_sha256") or ""
            ),
            "head_revision_id": effective_identity.get("head_revision_id"),
            "state": structure_state,
            "editable": editable,
            "nodes": nodes,
            "bilingual_projection": (
                _serialize_bilingual_projection_sidecar(effective_snapshot)
                if effective_snapshot is not None
                else persisted_bilingual_projection
                or _serialize_bilingual_projection_sidecar(None)
            ),
        },
        "requirements": requirements,
        "relations": _serialize_relations(raw_relations, source_index=source_index),
        "proposals": proposals,
        "revisions": serialized_revisions,
        "capabilities": {
            "can_edit_structure": editable,
            "can_calibrate_structure_boundaries": editable,
            "can_undo_revision": bool(editable and revisions),
            "can_optimize_structure": editable,
            "can_atomize": enrichment["readiness"]["can_atomize"],
            "can_refine": enrichment["readiness"]["can_refine"],
            "can_preview_publication": enrichment["readiness"][
                "can_preview_publication"
            ],
            "can_publish_draft": enrichment["readiness"]["can_publish_draft"],
            "fixture_transport": False,
        },
        "workflow": enrichment["workflow"],
        "readiness": enrichment["readiness"],
        "issues": [
            *enrichment["issues"],
            *_structure_inspection_issues(structure_inspection),
        ],
        "structure_inspection": structure_inspection,
        "actions": actions,
        "result": enrichment["result"],
        "source_pages": public_source_page_snapshot(
            payload,
            project_id=project_id,
            document_id=document_id,
        ),
    }
    requirement_tree = build_requirement_tree_snapshot(
        workbench=workbench,
        payload=payload,
    )
    workbench["requirement_tree"] = requirement_tree.model_dump(mode="json")
    if local_working_tree:
        publishable = requirement_tree.state is RequirementTreeState.READY
        workbench["capabilities"]["can_publish_draft"] = publishable
        workbench["readiness"]["can_publish_draft"] = publishable
        workbench["workflow"]["publication"]["state"] = (
            "READY" if publishable else "WAITING"
        )
        if publishable:
            workbench["actions"]["publish_draft_url"] = (
                f"/api/projects/{quote(project_id, safe='')}/sor-documents/"
                f"{quote(document_id, safe='')}/extraction/"
                "requirement-tree/publish-draft"
            )
        else:
            workbench["actions"].pop("publish_draft_url", None)
    if requirement_tree.state is RequirementTreeState.REQUIRES_REEXTRACTION:
        workbench["recovery"] = {
            "reason_code": "SOURCE_CAPTURE_GAP",
            "detail_code": "SOURCE_IR_INCOMPLETE",
            "recommended_action": "VISUAL_RESTORE_AND_REEXTRACT",
        }
        workbench["agent"].update(
            {
                "document_health": "REQUIRES_ACTION",
                "result_state": RequirementTreeState.REQUIRES_REEXTRACTION.value,
                "editable": False,
                "issue_count": int(workbench["agent"].get("issue_count") or 0) + 1,
            }
        )
        workbench["structure"].update(
            {
                "state": RequirementTreeState.REQUIRES_REEXTRACTION.value,
                "editable": False,
            }
        )
        workbench["capabilities"].update(
            {
                "can_edit_structure": False,
                "can_calibrate_structure_boundaries": False,
                "can_undo_revision": False,
                "can_optimize_structure": False,
                "can_atomize": False,
                "can_refine": False,
                "can_preview_publication": False,
                "can_publish_draft": False,
            }
        )
        workbench["issues"] = [
            *workbench["issues"],
            {
                "code": "SOURCE_TEXT_RESTORATION_REQUIRED",
                "severity": "BLOCKING",
                "message": (
                    "The completed run did not recover reliable source text or "
                    "any requirement leaves. Run visual source restoration."
                ),
                "retry_action": "REEXTRACT_WITH_VISUAL_RESTORATION",
            },
        ]
        blocking_issue_codes = workbench["readiness"].setdefault(
            "blocking_issue_codes",
            [],
        )
        if "SOURCE_TEXT_RESTORATION_REQUIRED" not in blocking_issue_codes:
            blocking_issue_codes.append("SOURCE_TEXT_RESTORATION_REQUIRED")
        workbench["actions"]["reextract_url"] = (
            f"/api/projects/{quote(project_id, safe='')}/sor-documents/"
            f"{quote(document_id, safe='')}/extraction/start"
        )
    return workbench


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: Any) -> list[dict[str, Any]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, (list, tuple))
        else []
    )


def _persisted_source_spans_by_node(
    value: Any,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Validate the durable effective span index before exposing it to the UI."""

    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for item in _dict_items(value):
        try:
            binding = StructureNodeSourceSpansV1.model_validate(item)
        except ValueError:
            return {}
        if binding.structure_node_id in result:
            return {}
        result[binding.structure_node_id] = tuple(
            span.model_dump(mode="json") for span in binding.source_spans
        )
    return result


def _unavailable_structure_inspection(
    *,
    reason: str = "NOT_RECORDED",
    effective_revision: int = 0,
) -> dict[str, Any]:
    return {
        "schema_version": "structure-inspection/v1",
        "state": "UNAVAILABLE",
        "unavailable_reason": reason,
        "inspection_id": None,
        "canonical_sha256": None,
        "source_ir_id": None,
        "source_sha256": None,
        "structure_snapshot_id": None,
        "resolver_id": None,
        "resolver_version": None,
        "inspection_runtime_version": None,
        "boundary_ruleset_version": None,
        "basis": "BASE_STRUCTURE",
        "basis_revision": 0,
        "effective_revision": effective_revision,
        "stale": False,
        "status": "UNAVAILABLE",
        "counts": {
            "inspected_source_node_count": 0,
            "inspected_visual_observation_count": 0,
            "visual_text_snapshot_count": 0,
            "grounded_source_node_count": 0,
            "candidate_count": 0,
            "observed_actionable_candidate_count": 0,
            "observed_structural_actionable_candidate_count": 0,
            "observed_document_capture_gap_count": 0,
            "observed_ungrounded_source_fact_count": 0,
            "actionable_candidate_count": 0,
            "structural_actionable_candidate_count": 0,
            "capture_gap_count": 0,
            "ungrounded_source_fact_count": 0,
            "suppressed_candidate_count": 0,
            "by_category_and_disposition": {},
        },
        "actionable_candidates": [],
    }


def _workbench_structure_inspection(
    payload: dict[str, Any],
    *,
    structure_agent: dict[str, Any],
    effective_revision: int,
    expected_source_sha256: str,
    expected_source_ir_id: str,
    expected_structure_snapshot_id: str,
    expected_resolver_id: str,
    expected_resolver_version: str,
) -> dict[str, Any]:
    """Expose one bounded, detect-only receipt without making it a gate."""

    top_level = (
        payload.get("structure_inspection")
        if isinstance(payload.get("structure_inspection"), dict)
        else None
    )
    agent_copy = (
        structure_agent.get("deterministic_inspection")
        if isinstance(
            structure_agent.get("deterministic_inspection"),
            dict,
        )
        else None
    )
    if (
        top_level is not None
        and agent_copy is not None
        and (
            str(top_level.get("inspection_id") or "")
            != str(agent_copy.get("inspection_id") or "")
            or str(top_level.get("canonical_sha256") or "")
            != str(agent_copy.get("canonical_sha256") or "")
        )
    ):
        return _unavailable_structure_inspection(
            reason="DUPLICATE_RECEIPT_MISMATCH",
            effective_revision=effective_revision,
        )
    raw = top_level or agent_copy
    if not isinstance(raw, dict):
        return _unavailable_structure_inspection(
            effective_revision=effective_revision,
        )

    raw_counts = _dict(raw.get("counts"))
    structural_candidates = [
        candidate
        for candidate in _dict_items(raw.get("actionable_candidates"))
        if (
            str(candidate.get("category") or "") != "SOURCE_CAPTURE_GAP"
            or isinstance(candidate.get("source_ref"), dict)
        )
    ]
    source_sha256 = str(raw.get("source_sha256") or "")
    source_ir_id = str(raw.get("source_ir_id") or "")
    structure_snapshot_id = str(raw.get("structure_snapshot_id") or "")
    resolver_id = str(raw.get("resolver_id") or "")
    resolver_version = str(raw.get("resolver_version") or "")
    identity_mismatch = bool(
        (expected_source_sha256 and source_sha256 != expected_source_sha256)
        or (expected_source_ir_id and source_ir_id != expected_source_ir_id)
        or (
            expected_structure_snapshot_id
            and structure_snapshot_id != expected_structure_snapshot_id
        )
        or (expected_resolver_id and resolver_id != expected_resolver_id)
        or (expected_resolver_version and resolver_version != expected_resolver_version)
    )
    if identity_mismatch:
        return _unavailable_structure_inspection(
            reason="LINEAGE_MISMATCH",
            effective_revision=effective_revision,
        )
    stale = bool(effective_revision > 0)
    observed_structural = _nonnegative_int(
        raw_counts.get("observed_structural_actionable_candidate_count")
    )
    if "observed_structural_actionable_candidate_count" not in raw_counts:
        observed_structural = _nonnegative_int(
            raw_counts.get("structural_actionable_candidate_count")
        )
    observed_document_capture = _nonnegative_int(
        raw_counts.get("observed_document_capture_gap_count")
    )
    if "observed_document_capture_gap_count" not in raw_counts:
        observed_document_capture = _nonnegative_int(
            raw_counts.get("capture_gap_count")
        )
    observed_ungrounded = _nonnegative_int(
        raw_counts.get("observed_ungrounded_source_fact_count")
    )
    if "observed_ungrounded_source_fact_count" not in raw_counts:
        observed_ungrounded = _nonnegative_int(
            raw_counts.get("ungrounded_source_fact_count")
        )
    observed_actionable = _nonnegative_int(
        raw_counts.get("observed_actionable_candidate_count")
    )
    if "observed_actionable_candidate_count" not in raw_counts:
        observed_actionable = observed_structural + observed_document_capture
    active_structural = 0 if stale else observed_structural
    counts = {
        "inspected_source_node_count": _nonnegative_int(
            raw_counts.get("inspected_source_node_count")
        ),
        "inspected_visual_observation_count": _nonnegative_int(
            raw_counts.get("inspected_visual_observation_count")
        ),
        "visual_text_snapshot_count": _nonnegative_int(
            raw_counts.get("visual_text_snapshot_count")
        ),
        "grounded_source_node_count": _nonnegative_int(
            raw_counts.get("grounded_source_node_count")
        ),
        "candidate_count": _nonnegative_int(raw_counts.get("candidate_count")),
        "observed_actionable_candidate_count": observed_actionable,
        "observed_structural_actionable_candidate_count": (observed_structural),
        "observed_document_capture_gap_count": (observed_document_capture),
        "observed_ungrounded_source_fact_count": observed_ungrounded,
        "actionable_candidate_count": (observed_document_capture + active_structural),
        "structural_actionable_candidate_count": active_structural,
        "capture_gap_count": observed_document_capture,
        "ungrounded_source_fact_count": (0 if stale else observed_ungrounded),
        "suppressed_candidate_count": _nonnegative_int(
            raw_counts.get("suppressed_candidate_count")
        ),
        "by_category_and_disposition": {
            str(key): _nonnegative_int(value)
            for key, value in _dict(
                raw_counts.get("by_category_and_disposition")
            ).items()
        },
    }
    status = (
        "REQUIRES_REEXTRACTION"
        if observed_document_capture
        else "STALE_AFTER_STRUCTURE_REVISION"
        if stale
        else "REVIEW_REQUIRED"
        if active_structural
        else "COMPLETE"
    )
    return {
        "schema_version": str(raw.get("schema_version") or "structure-inspection/v1"),
        "state": "READY",
        "unavailable_reason": None,
        "inspection_id": str(raw.get("inspection_id") or "") or None,
        "canonical_sha256": str(raw.get("canonical_sha256") or "") or None,
        "source_ir_id": source_ir_id or None,
        "source_sha256": source_sha256 or None,
        "structure_snapshot_id": structure_snapshot_id or None,
        "resolver_id": resolver_id or None,
        "resolver_version": resolver_version or None,
        "inspection_runtime_version": (
            str(raw.get("inspection_runtime_version") or "") or None
        ),
        "boundary_ruleset_version": (
            str(raw.get("boundary_ruleset_version") or "") or None
        ),
        "basis": str(raw.get("basis") or "BASE_STRUCTURE"),
        "basis_revision": 0,
        "effective_revision": effective_revision,
        "stale": stale,
        "status": status,
        "counts": counts,
        "actionable_candidates": [] if stale else structural_candidates,
    }


def _structure_inspection_issues(
    inspection: dict[str, Any],
) -> list[dict[str, Any]]:
    """Publish one non-blocking review issue for all structural candidates."""

    counts = _dict(inspection.get("counts"))
    structural_count = _nonnegative_int(
        counts.get("structural_actionable_candidate_count")
    )
    if inspection.get("stale") is True or structural_count == 0:
        return []
    categories = sorted(
        {
            str(candidate.get("category") or "")
            for candidate in _dict_items(inspection.get("actionable_candidates"))
            if str(candidate.get("category") or "")
            and (
                str(candidate.get("category") or "") != "SOURCE_CAPTURE_GAP"
                or isinstance(candidate.get("source_ref"), dict)
            )
        }
    )
    return [
        {
            "code": "STRUCTURE_INSPECTION_REVIEW_REQUIRED",
            "severity": "WARNING",
            "message": (
                "Deterministic source-bound checks found structure candidates "
                "that should be reviewed; the document remains usable."
            ),
            "candidate_count": structural_count,
            "categories": categories,
            "retry_action": "REVIEW_STRUCTURE_CANDIDATES",
        }
    ]


def _nonnegative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _serialize_enrichment_workflow(
    *,
    project_id: str,
    document_id: str,
    editable: bool,
    state: dict[str, Any],
    review_session_required: bool = True,
) -> dict[str, Any]:
    session_id = str(state.get("session_id") or "")
    review_revision = int(state.get("review_revision") or 0)
    provider_available = state.get("provider_available") is True
    atomization_provider_available = state.get("atomization_provider_available") is True
    coverage = _dict(state.get("coverage"))
    coverage_eligible = coverage.get("eligible") is True
    coverage_error_code = str(state.get("coverage_error_code") or "")
    eligible_direct_node_count = int(state.get("eligible_direct_node_count") or 0)
    atomization_progress = _dict(state.get("atomization_progress"))
    atomization_progress_state = str(atomization_progress.get("state") or "NOT_STARTED")
    run = _dict(state.get("atomization_run"))
    run_provider_runs = _dict_items(run.get("provider_runs"))
    local_pass_through_run = bool(
        run_provider_runs
        and all(
            str(item.get("status") or "") == "SKIPPED"
            and int(item.get("request_count") or 0) == 0
            and str(item.get("error_code") or "") == "remote_provider_not_configured"
            for item in run_provider_runs
        )
    )
    refinements = _dict_items(state.get("refinements"))
    preview = _dict(state.get("publication_preview"))
    preview_stale = state.get("publication_preview_stale") is True
    publication = _dict(state.get("publication"))
    review_nodes = _dict_items(state.get("review_nodes"))
    atomic_nodes = [
        item
        for item in _dict_items(run.get("nodes"))
        if str(item.get("kind") or "") == "ATOMIC_OBLIGATION"
        and str(item.get("atomic_node_id") or "")
    ]
    has_session = bool(session_id)
    has_run = bool(run.get("atomization_run_id"))
    has_preview = bool(preview.get("preview_id"))
    has_publication = bool(publication.get("requirement_set_id"))
    awaiting_final_review = bool(
        not has_session and (not editable or not review_session_required)
    )
    can_atomize = bool(
        editable
        and has_session
        and coverage_eligible
        and eligible_direct_node_count > 0
        and atomization_progress_state != "RUNNING"
    )
    can_refine = bool(
        editable and has_session and has_run and atomic_nodes and provider_available
    )
    can_preview = bool(editable and coverage_eligible and has_run and run.get("nodes"))
    can_publish = bool(
        editable
        and coverage_eligible
        and has_preview
        and not preview_stale
        and not has_publication
    )
    review_base_url = (
        f"/api/projects/{quote(project_id, safe='')}/sor-documents/"
        f"{quote(document_id, safe='')}/extraction/llm-reviews/"
        f"{quote(session_id, safe='')}"
        if has_session
        else ""
    )
    actions: dict[str, Any] = {}
    if can_atomize:
        actions["atomization_run_url"] = f"{review_base_url}/atomization-runs"
    if can_refine:
        actions["refinement_suggestion_url"] = (
            f"{review_base_url}/refinement-suggestions"
        )
    if editable and coverage_eligible and refinements:
        actions["refinement_decision_url_template"] = (
            f"{review_base_url}/refinement-suggestions/{{suggestion_id}}"
        )
    if can_preview:
        actions["publication_preview_url"] = f"{review_base_url}/publication-preview"
    if can_publish:
        actions["publish_draft_url"] = f"{review_base_url}/publish-draft"
    if has_session:
        actions["enrichment_state_url"] = f"{review_base_url}/enrichment"
    issues: list[dict[str, Any]] = []
    if editable and not has_session and review_session_required:
        issues.append(
            {
                "code": "REVIEW_SESSION_UNAVAILABLE",
                "severity": "BLOCKING",
                "message": "A persisted source-bound review is required.",
                "retry_action": "RUN_REQUIREMENT_EXTRACTION",
            }
        )
    if has_session and not coverage:
        issues.append(
            {
                "code": "SEMANTIC_COVERAGE_UNAVAILABLE",
                "severity": "BLOCKING",
                "message": (
                    "A current runtime-bound semantic coverage receipt is required."
                ),
                "detail_code": coverage_error_code or None,
                "retry_action": "RUN_REQUIREMENT_EXTRACTION",
            }
        )
    if coverage and not coverage_eligible:
        issues.append(
            {
                "code": "SEMANTIC_COVERAGE_INCOMPLETE",
                "severity": "BLOCKING",
                "message": (
                    "Resolve every blocked or unresolved semantic unit before "
                    "atomization."
                ),
                "total_units": int(coverage.get("total_units") or 0),
                "blocked_unit_count": int(coverage.get("blocked_unit_count") or 0),
                "unresolved_unit_count": int(
                    coverage.get("unresolved_unit_count") or 0
                ),
                "retry_action": "REVIEW_SEMANTIC_EXCEPTIONS",
            }
        )
    if has_session and coverage_eligible and not atomization_provider_available:
        issues.append(
            {
                "code": "ATOMIZATION_PROVIDER_UNAVAILABLE",
                "severity": "INFO",
                "message": (
                    "The remote atomization provider is not configured. "
                    "Atomization will use exact local one-to-one pass-through."
                ),
                "retry_action": "RUN_LOCAL_PASS_THROUGH_ATOMIZATION",
            }
        )
    if (
        has_session
        and coverage_eligible
        and atomization_provider_available
        and eligible_direct_node_count == 0
    ):
        issues.append(
            {
                "code": "NO_TECHNICAL_DIRECT_REQUIREMENTS",
                "severity": "INFO",
                "message": (
                    "No included technical DIRECT requirement is available to atomize."
                ),
                "retry_action": "REVIEW_REQUIREMENT_CLASSIFICATION",
            }
        )
    if has_session and not provider_available:
        issues.append(
            {
                "code": "REFINEMENT_PROVIDER_UNAVAILABLE",
                "severity": "BLOCKING",
                "message": "The controlled refinement provider is not configured.",
                "retry_action": "RETRY_REFINEMENT_PROVIDER",
            }
        )
    if has_run and not atomic_nodes:
        issues.append(
            {
                "code": "NO_ATOMIC_OBLIGATIONS",
                "severity": "INFO",
                "message": "No DIRECT technical obligation is available to refine.",
                "retry_action": "REVIEW_REQUIREMENT_CLASSIFICATION",
            }
        )
    if not has_run and atomization_progress_state == "PARTIAL":
        issues.append(
            {
                "code": "ATOMIZATION_PARTIAL_RESUMABLE",
                "severity": "WARNING",
                "message": (
                    "Completed atomization nodes are checkpointed; retry resumes "
                    "only the remaining technical requirements."
                ),
                "completed_direct_node_count": int(
                    atomization_progress.get("completed_direct_node_count") or 0
                ),
                "total_direct_node_count": int(
                    atomization_progress.get("total_direct_node_count") or 0
                ),
                "retry_action": "RESUME_ATOMIZATION",
            }
        )
    if has_preview and preview_stale:
        issues.append(
            {
                "code": "PUBLICATION_PREVIEW_STALE",
                "severity": "BLOCKING",
                "message": (
                    "The active Requirement Set or a refinement revision changed "
                    "after this preview was created."
                ),
                "retry_action": "CREATE_PUBLICATION_PREVIEW",
            }
        )
    readiness = {
        "schema_version": "requirement-enrichment-readiness/v1",
        "can_atomize": can_atomize,
        "can_refine": can_refine,
        "can_preview_publication": can_preview,
        "can_publish_draft": can_publish,
        "blocking_issue_codes": [
            item["code"] for item in issues if item["severity"] == "BLOCKING"
        ],
    }
    workflow = {
        "schema_version": "requirement-enrichment-workflow/v1",
        "review": {
            "state": (
                "READY"
                if has_session
                else "WAITING"
                if awaiting_final_review
                else "BLOCKED"
            ),
            "session_id": session_id or None,
            "revision": review_revision if has_session else None,
        },
        "coverage": {
            "state": (
                "READY"
                if coverage_eligible
                else "INCOMPLETE"
                if coverage
                else "WAITING"
                if awaiting_final_review
                else "BLOCKED"
            ),
            "total_units": coverage.get("total_units"),
            "blocked_unit_count": coverage.get("blocked_unit_count"),
            "unresolved_unit_count": coverage.get("unresolved_unit_count"),
            "coverage_sha256": coverage.get("coverage_sha256"),
        },
        "atomization": {
            "state": "COMPLETED"
            if has_run
            else "RUNNING"
            if atomization_progress_state == "RUNNING"
            else "QUESTION"
            if atomization_progress_state == "PARTIAL"
            else (
                "READY"
                if can_atomize
                else "WAITING"
                if awaiting_final_review
                else "BLOCKED"
            ),
            "expected_review_revision": (review_revision if has_session else None),
            "atomization_run_id": run.get("atomization_run_id"),
            "revision": run.get("revision"),
            "atomic_obligation_count": len(atomic_nodes),
            "eligible_direct_node_count": eligible_direct_node_count,
            "completed_direct_node_count": int(
                atomization_progress.get("completed_direct_node_count") or 0
            ),
            "total_direct_node_count": int(
                atomization_progress.get("total_direct_node_count")
                or eligible_direct_node_count
            ),
            "resumable": atomization_progress.get("resumable") is True,
            "execution_mode": (
                "LOCAL_PASS_THROUGH"
                if local_pass_through_run
                else "REMOTE_PROVIDER"
                if has_run or atomization_provider_available
                else "LOCAL_PASS_THROUGH"
                if has_session
                else None
            ),
        },
        "refinement": {
            "state": (
                "COMPLETED"
                if refinements
                and all(
                    str(item.get("status") or "") in {"ACCEPTED", "REJECTED"}
                    for item in refinements
                )
                else "IN_PROGRESS"
                if refinements
                else "READY"
                if can_refine
                else "WAITING"
                if awaiting_final_review
                else "BLOCKED"
            ),
            "atomization_run_id": run.get("atomization_run_id"),
            "suggestion_count": len(refinements),
            "expected_refinement_revisions": {
                str(item.get("suggestion_id") or ""): int(item.get("revision") or 0)
                for item in refinements
                if str(item.get("suggestion_id") or "")
            },
        },
        "publication": {
            "state": (
                "PUBLISHED_DRAFT"
                if has_publication
                else "PREVIEW_READY"
                if has_preview
                else "READY"
                if can_preview
                else "WAITING"
                if awaiting_final_review
                else "BLOCKED"
            ),
            "preview_id": preview.get("preview_id"),
            "publication_key": preview.get("publication_key"),
            "preview_stale": preview_stale,
            "requirement_set_id": publication.get("requirement_set_id"),
        },
    }
    return {
        "workflow": workflow,
        "readiness": readiness,
        "issues": issues,
        "actions": actions,
        "result": {
            "schema_version": "requirement-enrichment-result/v1",
            "review_nodes": review_nodes,
            "confirmed_review_node_ids": list(
                state.get("confirmed_review_node_ids")
                if isinstance(state.get("confirmed_review_node_ids"), (list, tuple))
                else ()
            ),
            "atomization_run": run or None,
            "atomization_progress": atomization_progress or None,
            "coverage": coverage or None,
            "refinements": refinements,
            "publication_preview": preview or None,
            "publication": publication or None,
        },
    }


def _source_index(
    source_ir: SourceIRSnapshot | None,
    *,
    structure: StructureTreeSnapshot | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    if source_ir is None:
        return {}, 0
    index: dict[str, dict[str, Any]] = {}
    assets_by_id = {asset.asset_id: asset for asset in source_ir.assets}
    physical_orders = physical_source_reading_order_index(source_ir)
    authority_pages = visual_text_authority_pages(source_ir)
    superseded_native_source_ids = superseded_native_text_source_ids(source_ir)
    visual_cells_by_id = {
        cell.visual_table_cell_id: cell
        for snapshot in source_ir.visual_table_snapshots
        for cell in snapshot.cells
    }
    adopted_visual_text_by_native_cell_id = {
        binding.table_cell_id: visual_cells_by_id[binding.visual_table_cell_id].raw_text
        for binding in (
            structure.visual_table_cell_text_bindings if structure is not None else ()
        )
        if binding.visual_table_cell_id in visual_cells_by_id
    }
    page_count = 0
    for node in source_ir.nodes:
        locator = node.native_locator
        visual_locator = node.visual_locator or locator
        asset = assets_by_id.get(node.asset_ref or "")
        page = locator.page_number
        if page is not None:
            page_count = max(page_count, page)
        index[node.source_node_id] = {
            "page_number": page,
            "text": (
                ""
                if node.source_node_id in superseded_native_source_ids
                or (
                    node.table_payload is not None
                    and page in authority_pages
                )
                else node.raw_text
            ),
            "kind": node.kind.value,
            "source_order": physical_orders.get(
                node.source_node_id,
                node.source_order,
            ),
            "asset_id": asset.asset_id if asset is not None else "",
            "asset_kind": asset.kind.value if asset is not None else "",
            "content_sha256": (asset.content_sha256 if asset is not None else ""),
            "media_type": asset.media_type if asset is not None else "",
            "storage_key": asset.storage_key if asset is not None else "",
            "location": (
                visual_locator.model_dump(mode="json") if asset is not None else {}
            ),
        }
        if node.table_payload is not None:
            for cell in node.table_payload.cells:
                cell_locator = cell.native_locator
                cell_page = cell_locator.page_number
                authority_page = cell_page or node.native_locator.page_number
                if cell_page is not None:
                    page_count = max(page_count, cell_page)
                index[cell.cell_id] = {
                    "page_number": cell_page,
                    "text": (
                        adopted_visual_text_by_native_cell_id.get(cell.cell_id, "")
                        if authority_page in authority_pages
                        else cell.raw_text
                    ),
                    "kind": "cell",
                    "source_order": physical_orders.get(
                        node.source_node_id,
                        node.source_order,
                    ),
                }
    for snapshot in source_ir.visual_text_snapshots:
        page_count = max(page_count, snapshot.page_number)
        for observation in snapshot.observations:
            index[observation.observation_id] = {
                "page_number": observation.page_number,
                "text": observation.raw_text,
                "kind": (
                    "visual_text"
                    if observation.content_type == "TEXT"
                    else observation.content_type.lower()
                ),
                "source_order": None,
            }
    return index, page_count


def _persisted_structure_presentation(
    payload: dict[str, Any],
    *,
    structure_agent: dict[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    int,
]:
    effective_identity = _dict(structure_agent.get("effective_structure_identity"))
    index = validated_structure_presentation_index(
        payload.get("structure_presentation_index"),
        expected_source_sha256=str(payload.get("source_sha256") or ""),
        expected_structure_snapshot_id=str(effective_identity.get("snapshot_id") or ""),
    )
    if index is None:
        return {}, {}, 0
    nodes = {
        str(node_id): dict(value)
        for node_id, value in _dict(index.get("nodes")).items()
        if isinstance(node_id, str) and isinstance(value, dict)
    }
    source_pages: dict[str, dict[str, Any]] = {}
    for node in nodes.values():
        toc_source_text_by_id = _dict(node.get("toc_source_text_by_id"))
        for ref in _dict_items(node.get("source_refs")):
            source_id = str(ref.get("source_id") or "")
            page_number = ref.get("page_number")
            if source_id:
                source_fact = {
                    "page_number": (
                        page_number
                        if isinstance(page_number, int)
                        and not isinstance(page_number, bool)
                        and page_number > 0
                        else None
                    ),
                    "kind": str(ref.get("source_kind") or ""),
                }
                for field in (
                    "asset_id",
                    "asset_kind",
                    "content_sha256",
                    "media_type",
                    "storage_key",
                    "location",
                ):
                    if ref.get(field) not in (None, "", {}):
                        source_fact[field] = ref[field]
                if source_id in toc_source_text_by_id:
                    source_fact["text"] = str(toc_source_text_by_id[source_id])
                previous_source_fact = source_pages.get(source_id)
                if (
                    isinstance(previous_source_fact, dict)
                    and "text" in previous_source_fact
                    and "text" not in source_fact
                ):
                    source_fact["text"] = previous_source_fact["text"]
                source_pages[source_id] = source_fact
    return nodes, source_pages, max(int(index.get("page_count") or 0), 0)


def _persisted_structure_bilingual_projection(
    payload: dict[str, Any],
    *,
    structure_agent: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the sidecar only after the enclosing presentation validates."""

    effective_identity = _dict(structure_agent.get("effective_structure_identity"))
    index = validated_structure_presentation_index(
        payload.get("structure_presentation_index"),
        expected_source_sha256=str(payload.get("source_sha256") or ""),
        expected_structure_snapshot_id=str(effective_identity.get("snapshot_id") or ""),
    )
    if index is None:
        return None
    sidecar = index.get("bilingual_projection")
    return dict(sidecar) if isinstance(sidecar, dict) else None


def _presentation_index(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in _dict_items(payload.get("review_groups")):
        node_id = str(item.get("boundary_structure_node_id") or "")
        if node_id:
            index[node_id] = item
    for item in _dict_items(payload.get("items")):
        target = _dict(item.get("target_structure"))
        node_id = str(target.get("id") or "")
        if node_id:
            index.setdefault(node_id, item)
    return index


def _requirement_ids_by_node(
    payload: dict[str, Any],
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for item in _dict_items(payload.get("items")):
        node_id = str(_dict(item.get("target_structure")).get("id") or "")
        item_id = str(item.get("id") or "")
        if node_id and item_id:
            grouped[node_id].append(item_id)
    return {
        node_id: tuple(dict.fromkeys(item_ids)) for node_id, item_ids in grouped.items()
    }


def _effective_snapshot(
    *,
    source_ir: SourceIRSnapshot | None,
    base_tree: StructureTreeSnapshot | None,
    revisions: tuple[StructureRevisionV1, ...],
) -> Any | None:
    if source_ir is None or base_tree is None:
        return None
    return validate_structure_revision_chain(
        source_ir,
        base_tree,
        revisions,
    )


def _health_codes_by_node(
    findings: tuple[StructureFinding, ...],
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for finding in findings:
        if finding.structure_node_id is None:
            continue
        normalized = finding.code.upper()
        if "MISPLACED_PARENT" in normalized or "PARENT" in normalized:
            code = "UNCERTAIN_PARENT"
        elif "ORDER" in normalized or "NUMBER" in normalized:
            code = "NUMBERING_INCONSISTENCY"
        elif "CONTINU" in normalized:
            code = "CONTINUATION"
        elif "ORPHAN" in normalized or "UNASSIGNED" in normalized:
            code = "ORPHAN_UNASSIGNED"
        else:
            code = normalized
        grouped[finding.structure_node_id].append(code)
    return {node_id: tuple(dict.fromkeys(codes)) for node_id, codes in grouped.items()}


def _node_depths(
    nodes_by_id: dict[str, dict[str, Any]],
) -> dict[str, int]:
    depths: dict[str, int] = {}

    def depth(node_id: str, visiting: set[str]) -> int:
        if node_id in depths:
            return depths[node_id]
        if node_id in visiting:
            return 0
        visiting.add(node_id)
        parent_id = str(nodes_by_id[node_id].get("parent_structure_node_id") or "")
        value = depth(parent_id, visiting) + 1 if parent_id in nodes_by_id else 0
        visiting.remove(node_id)
        depths[node_id] = value
        return value

    for node_id in nodes_by_id:
        depth(node_id, set())
    return depths


def _node_label(
    node: dict[str, Any],
    *,
    document: dict[str, Any],
    source_index: dict[str, dict[str, Any]],
    presentation: dict[str, Any] | None,
    durable_presentation: dict[str, Any] | None,
) -> str:
    kind = str(node.get("kind") or "").upper()
    if kind == "DOCUMENT":
        return str(document.get("file_name") or "Document")
    # Exact Structure spans are the authoritative owned text.  Apply them to
    # SECTION/CLAUSE as well as CONTENT: embedded numbered siblings often share
    # one native PDF block, so a broad source-ref label can otherwise display a
    # neighbouring clause and suppress the real inline content candidate.
    for span in _dict_items(node.get("semantic_source_spans")):
        ref = _dict(span.get("source_ref"))
        source_text = str(
            source_index.get(str(ref.get("source_id") or ""), {}).get("text") or ""
        )
        start = span.get("start_offset")
        end = span.get("end_offset")
        if (
            source_text
            and isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end <= len(source_text)
        ):
            exact = source_text[start:end].strip()
            if exact:
                return exact
    durable_owned_text = str(
        (durable_presentation or {}).get("owned_text") or ""
    ).strip()
    if durable_owned_text:
        return durable_owned_text
    if kind == "CONTENT":
        # CONTENT is an immutable source fact, not a semantic presentation.
        # A rule/LLM review title may intentionally be shorter (for example a
        # leading bullet glyph), but it must never replace the exact text that
        # the structure node owns.  Prefer a proven span, then the bound source
        # fact, before consulting any review projection.
        for ref in _dict_items(node.get("source_refs")):
            source_text = str(
                source_index.get(str(ref.get("source_id") or ""), {}).get("text") or ""
            ).strip()
            if source_text:
                return source_text
        source_kinds = {
            str(
                ref.get("source_kind")
                or source_index.get(str(ref.get("source_id") or ""), {}).get("kind")
                or ""
            ).lower()
            for ref in _dict_items((durable_presentation or {}).get("source_refs"))
            or _dict_items(node.get("source_refs"))
        }
        if source_kinds & {"image", "figure"}:
            # Structure intentionally models image placements as generic
            # CONTENT nodes. Their immutable SourceIR kind remains the media
            # authority, so an empty image fact gets an honest display label
            # instead of the structural fallback "Content".
            return "图片"
    if presentation is not None:
        value = str(presentation.get("title") or presentation.get("text") or "").strip()
        if value:
            return _compact_label(value)
    durable_label = str((durable_presentation or {}).get("label") or "").strip()
    if durable_label:
        return durable_label
    texts = [
        str(source_index.get(str(ref.get("source_id") or ""), {}).get("text") or "")
        for ref in _dict_items(node.get("source_refs"))
    ]
    text = next((value for value in texts if value.strip()), "")
    label = str(node.get("explicit_label") or "").strip()
    if text:
        compact = _compact_label(text)
        return (
            compact if not label or compact.startswith(label) else f"{label} {compact}"
        )
    if label:
        return label
    return kind.replace("_", " ").title() or str(
        node.get("structure_node_id") or "Structure node"
    )


def _compact_label(value: str, *, limit: int = 180) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}…"


def _node_path(
    node_id: str,
    *,
    nodes_by_id: dict[str, dict[str, Any]],
    labels: dict[str, str],
) -> list[str]:
    path: list[str] = []
    current_id = node_id
    visited: set[str] = set()
    while current_id in nodes_by_id and current_id not in visited:
        visited.add(current_id)
        path.append(labels[current_id])
        current_id = str(nodes_by_id[current_id].get("parent_structure_node_id") or "")
    path.reverse()
    return path


def _serialize_node(
    node: dict[str, Any],
    *,
    depth: int,
    label: str,
    path: list[str],
    source_index: dict[str, dict[str, Any]],
    presentation: dict[str, Any] | None,
    durable_presentation: dict[str, Any] | None,
    requirement_ids: tuple[str, ...],
    health_codes: tuple[str, ...],
    source_spans: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    durable_source_spans = (
        list(source_spans)
        if source_spans
        else [dict(span) for span in _dict_items(node.get("semantic_source_spans"))]
    )
    source_refs = (
        [
            _enrich_persisted_source_ref(ref, source_index=source_index)
            for ref in _dict_items((durable_presentation or {}).get("source_refs"))
        ]
        if durable_presentation is not None
        else [
            _serialize_source_ref(ref, source_index=source_index)
            for ref in _dict_items(node.get("source_refs"))
        ]
    )
    # A Structure node's page range is an intrinsic source fact. Review/item
    # presentations may intentionally span a wider semantic context, so their
    # aggregate page range must never widen the node's physical boundary.
    pages = _intrinsic_structure_node_pages(
        source_refs=source_refs,
        source_spans=durable_source_spans,
        source_index=source_index,
    )
    physical_source_order, physical_source_offset = (
        _intrinsic_structure_node_physical_order(
            source_refs=source_refs,
            source_spans=durable_source_spans,
            source_index=source_index,
            durable_presentation=durable_presentation,
        )
    )
    kind = str(node.get("kind") or "content").upper()
    return {
        "id": str(node.get("structure_node_id") or ""),
        "clause_id": str(
            (presentation or {}).get("id")
            or (presentation or {}).get("boundary_structure_node_id")
            or ""
        ),
        "parent_id": str(node.get("parent_structure_node_id") or ""),
        "order": int(node.get("source_order") or 0),
        "physical_source_order": physical_source_order,
        "physical_source_offset": physical_source_offset,
        "depth": depth,
        "kind": kind,
        "boundary_basis": str(node.get("boundary_basis") or "").upper(),
        "explicit_label": node.get("explicit_label"),
        "list_ordinal": node.get("list_ordinal"),
        "table_row_index": node.get("table_row_index"),
        "label": label,
        "path": path,
        "page_start": pages[0] if pages else None,
        "page_end": pages[-1] if pages else None,
        "allows_children": kind not in {"TABLE_ROW", "CONTINUATION", "CONTENT"},
        "requirement_ids": list(requirement_ids),
        "health_codes": list(health_codes),
        "source_refs": source_refs,
        "source_spans": durable_source_spans,
    }


def _intrinsic_structure_node_pages(
    *,
    source_refs: list[dict[str, Any]],
    source_spans: list[dict[str, Any]],
    source_index: dict[str, dict[str, Any]],
) -> list[int]:
    """Resolve only pages proven by this Structure node's own bindings."""

    pages: set[int] = set()

    def add_page(value: Any) -> None:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            pages.add(value)

    def add_binding(binding: dict[str, Any]) -> None:
        add_page(binding.get("page_number"))
        add_page(_dict(binding.get("location")).get("page_number"))
        source_id = str(binding.get("source_id") or "")
        if source_id:
            evidence = source_index.get(source_id, {})
            add_page(evidence.get("page_number"))
            add_page(_dict(evidence.get("location")).get("page_number"))

    for ref in source_refs:
        add_binding(ref)
    for span in source_spans:
        add_binding(span)
        add_binding(_dict(span.get("source_ref")))
    return sorted(pages)


def _intrinsic_structure_node_physical_order(
    *,
    source_refs: list[dict[str, Any]],
    source_spans: list[dict[str, Any]],
    source_index: dict[str, dict[str, Any]],
    durable_presentation: dict[str, Any] | None,
) -> tuple[int | None, int]:
    """Return immutable SourceIR-bound order, never editable tree order."""

    durable_order = (durable_presentation or {}).get("physical_source_order")
    durable_offset = (durable_presentation or {}).get("physical_source_offset")
    if (
        isinstance(durable_order, int)
        and not isinstance(durable_order, bool)
        and durable_order >= 0
    ):
        return (
            durable_order,
            durable_offset
            if isinstance(durable_offset, int)
            and not isinstance(durable_offset, bool)
            and durable_offset >= 0
            else 0,
        )

    candidates: list[tuple[int, int]] = []

    def add_binding(binding: dict[str, Any], *, offset: int = 0) -> None:
        source_id = str(binding.get("source_id") or "")
        source_order = source_index.get(source_id, {}).get("source_order")
        if (
            isinstance(source_order, int)
            and not isinstance(source_order, bool)
            and source_order >= 0
        ):
            candidates.append((source_order, max(offset, 0)))

    exact_span_offsets_by_source_id: dict[str, list[int]] = defaultdict(list)
    for span in source_spans:
        start_offset = span.get("start_offset")
        if not (
            isinstance(start_offset, int)
            and not isinstance(start_offset, bool)
            and start_offset >= 0
        ):
            continue
        source_id = str(
            span.get("source_id")
            or _dict(span.get("source_ref")).get("source_id")
            or ""
        )
        if source_id:
            exact_span_offsets_by_source_id[source_id].append(start_offset)

    ref_source_ids = {
        str(ref.get("source_id") or "")
        for ref in source_refs
        if str(ref.get("source_id") or "")
    }
    for ref in source_refs:
        source_id = str(ref.get("source_id") or "")
        offsets = exact_span_offsets_by_source_id.get(source_id)
        add_binding(ref, offset=min(offsets) if offsets else 0)
    for span in source_spans:
        start_offset = span.get("start_offset")
        valid_offset = (
            start_offset
            if isinstance(start_offset, int)
            and not isinstance(start_offset, bool)
            and start_offset >= 0
            else None
        )
        for binding in (span, _dict(span.get("source_ref"))):
            source_id = str(binding.get("source_id") or "")
            if valid_offset is not None:
                add_binding(binding, offset=valid_offset)
            elif (
                source_id
                and source_id not in ref_source_ids
                and source_id not in exact_span_offsets_by_source_id
            ):
                add_binding(binding)
    return min(candidates) if candidates else (None, 0)


def _serialize_source_ref(
    ref: dict[str, Any],
    *,
    source_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_id = str(ref.get("source_id") or "")
    evidence = source_index.get(source_id, {})
    result = {
        "kind": str(ref.get("kind") or ""),
        "source_id": source_id,
        "page_number": evidence.get("page_number"),
    }
    source_kind = str(evidence.get("kind") or "")
    if source_kind:
        result["source_kind"] = source_kind
    for field in (
        "asset_id",
        "asset_kind",
        "content_sha256",
        "media_type",
        "storage_key",
        "location",
    ):
        if evidence.get(field) not in (None, "", {}):
            result[field] = evidence[field]
    return result


def _enrich_persisted_source_ref(
    ref: dict[str, Any],
    *,
    source_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Add current SourceIR fact metadata without changing durable lineage."""

    result = dict(ref)
    source_id = str(result.get("source_id") or "")
    evidence = source_index.get(source_id, {})
    if result.get("page_number") is None and evidence.get("page_number") is not None:
        result["page_number"] = evidence["page_number"]
    source_kind = str(evidence.get("kind") or "")
    if source_kind:
        result["source_kind"] = source_kind
    for field in (
        "asset_id",
        "asset_kind",
        "content_sha256",
        "media_type",
        "storage_key",
        "location",
    ):
        if result.get(field) in (None, "", {}) and evidence.get(field) not in (
            None,
            "",
            {},
        ):
            result[field] = evidence[field]
    return result


def _serialize_requirements(payload: dict[str, Any]) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(_dict_items(payload.get("items"))):
        semantic_class = str(item.get("semantic_class") or "").upper()
        if (
            semantic_class != "REQUIREMENT"
            or str(item.get("decision_mode") or "").upper() == "NONE"
            or not str(item.get("text") or "").strip()
        ):
            continue
        item_id = str(item.get("id") or f"requirement-{index}")
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        target = _dict(item.get("target_structure"))
        requirements.append(
            {
                "id": item_id,
                "clause_id": item_id,
                "structure_node_id": str(target.get("id") or ""),
                "entry_type": _entry_type(item),
                "code": str(target.get("explicit_label") or ""),
                "title": str(item.get("title") or ""),
                "text": str(item.get("text") or ""),
                "page_start": item.get("page_start"),
                "atomic_count": 1,
                "refinement_state": str(item.get("refinement_state") or "NOT_RUN"),
            }
        )
    return requirements


def _entry_type(item: dict[str, Any]) -> str:
    explicit = str(item.get("entry_type") or "").upper()
    if explicit in {"TECHNICAL", "NON_TECHNICAL", "INFORMATION"}:
        return explicit
    semantic_class = str(item.get("semantic_class") or "").upper()
    if semantic_class == "REQUIREMENT":
        return "TECHNICAL"
    if semantic_class in {"SCHEDULE", "PROJECT_RISK", "VERSION_METADATA"}:
        return "NON_TECHNICAL"
    return "INFORMATION"


def _serialize_relations(
    relations: list[dict[str, Any]],
    *,
    source_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for relation in relations:
        refs = _dict_items(relation.get("source_refs"))
        pages = [
            source_index.get(str(ref.get("source_id") or ""), {}).get("page_number")
            for ref in refs
        ]
        kind = str(relation.get("kind") or "").upper()
        serialized.append(
            {
                "id": str(relation.get("relation_id") or ""),
                "kind": "PARENT_OF" if kind == "CONTAINS" else kind,
                "from_id": str(relation.get("source_structure_node_id") or ""),
                "to_id": str(relation.get("target_structure_node_id") or ""),
                "source_page": next(
                    (int(page) for page in pages if page is not None),
                    None,
                ),
                # Exact grounding is required by consumers that validate a
                # typed relation sidecar (notably bilingual counterparts).
                # ``source_page`` remains a convenience projection only.
                "source_refs": refs,
            }
        )
    return serialized


def _serialize_bilingual_projection_sidecar(
    effective_snapshot: Any | None,
) -> dict[str, Any]:
    """Expose the validated Structure bilingual sidecar without rewriting it."""

    return build_structure_bilingual_projection_sidecar(effective_snapshot)


def _serialize_proposals(
    structure_agent: dict[str, Any],
    *,
    revisions: tuple[StructureRevisionV1, ...],
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    node_pages = {item["id"]: item.get("page_start") for item in nodes}
    revisions_by_proposal = {
        revision.patch.accepted_proposal_id: revision
        for revision in revisions
        if revision.patch is not None
    }
    proposals: list[dict[str, Any]] = []
    for index, summary in enumerate(
        _dict_items(structure_agent.get("proposal_summaries"))
    ):
        proposal_id = str(
            summary.get("proposal_id")
            or summary.get("finding_id")
            or f"structure-proposal-{index}"
        )
        revision = revisions_by_proposal.get(proposal_id)
        target_id = _revision_target_node_id(revision) if revision is not None else ""
        disposition = str(summary.get("status") or "UNRESOLVED").upper()
        status = "RESOLVED" if disposition == "APPLIED" else disposition
        proposals.append(
            {
                "id": proposal_id,
                "target_node_id": target_id,
                "status": status,
                "disposition": disposition,
                "code": str(
                    summary.get("rejection_code")
                    or summary.get("finding_id")
                    or "STRUCTURE_PROPOSAL"
                ).upper(),
                "source_page": node_pages.get(target_id),
                "provider_run_id": summary.get("provider_run_id"),
                "revision_id": summary.get("revision_id"),
            }
        )
    return proposals


def _serialize_revisions(
    revisions: tuple[StructureRevisionV1, ...],
    *,
    source_ir: SourceIRSnapshot | None,
    base_tree: StructureTreeSnapshot | None,
    nodes: list[dict[str, Any]],
    proposal_summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    node_pages = {item["id"]: item.get("page_start") for item in nodes}
    origin_by_proposal = {
        str(item.get("proposal_id") or ""): str(item.get("origin") or "AI").upper()
        for item in proposal_summaries
        if item.get("proposal_id")
    }
    contexts: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if source_ir is not None and base_tree is not None:
        for index in range(len(revisions)):
            before = validate_structure_revision_chain(
                source_ir,
                base_tree,
                revisions[:index],
            )
            after = validate_structure_revision_chain(
                source_ir,
                base_tree,
                revisions[: index + 1],
            )
            contexts.append(
                (
                    {item.structure_node_id: item for item in before.nodes},
                    {item.structure_node_id: item for item in after.nodes},
                )
            )
    result: list[dict[str, Any]] = []
    for index, revision in enumerate(revisions):
        target_id = _revision_target_node_id(revision)
        before_nodes: dict[str, Any] = {}
        after_nodes: dict[str, Any] = {}
        if index < len(contexts):
            before_nodes, after_nodes = contexts[index]
        before = before_nodes.get(target_id)
        after = after_nodes.get(target_id)
        patch = revision.patch
        operation = (
            "UNDO"
            if patch is None
            else (
                patch.operations[0].operation.upper()
                if len(patch.operations) == 1
                else "PATCH"
            )
        )
        actor_type = (
            "HUMAN"
            if (
                revision.undo_target_revision_id is not None
                or (
                    patch is not None
                    and origin_by_proposal.get(patch.accepted_proposal_id) == "HUMAN"
                )
            )
            else "AI"
        )
        result.append(
            {
                "id": revision.revision_id,
                "revision": revision.revision_number,
                "actor_type": actor_type,
                "actor_label": ("User" if actor_type == "HUMAN" else "Structure Agent"),
                "operation": operation,
                "status": "APPLIED",
                "target_node_id": target_id,
                "old_parent_id": (
                    before.parent_structure_node_id if before is not None else None
                ),
                "new_parent_id": (
                    after.parent_structure_node_id if after is not None else None
                ),
                "old_order": before.source_order if before is not None else None,
                "new_order": after.source_order if after is not None else None,
                "source_page": node_pages.get(target_id),
                "affected_requirement_count": 0,
                "created_at": "",
                "undoable": index == len(revisions) - 1,
                "target_revision_id": revision.undo_target_revision_id,
                "reason_code": None,
                "effective_snapshot_id": revision.effective_snapshot_id,
            }
        )
    return result


def _revision_target_node_id(revision: StructureRevisionV1) -> str:
    if revision.patch is not None:
        operation = revision.patch.operations[0]
        for field in (
            "structure_node_id",
            "primary_structure_node_id",
            "source_structure_node_id",
            "parent_structure_node_id",
        ):
            value = getattr(operation, field, None)
            if value:
                return str(value)
    return next(iter(revision.impact.direct_structure_node_ids), "")


def _structure_state(
    job_status: str,
    *,
    editable: bool,
    serving_previous_result: bool = False,
) -> str:
    if job_status == "INTERRUPTED":
        return "INTERRUPTED"
    if job_status == "FAILED":
        return "FAILED"
    if editable or serving_previous_result:
        return "STABLE"
    return "PARTIAL"


def _document_health(
    *,
    job_status: str,
    structure_state: str,
    issue_count: int,
) -> str:
    if job_status == "RUNNING":
        return "RUNNING"
    if job_status == "INTERRUPTED":
        return "INTERRUPTED"
    if job_status == "FAILED":
        return "FAILED"
    if structure_state == "PARTIAL":
        return "PARTIAL"
    return "STABLE_WITH_ISSUES" if issue_count else "STABLE"


def _active_agent_stage(
    structure_agent: dict[str, Any],
    job: dict[str, Any],
) -> str:
    stages = _dict_items(structure_agent.get("stages"))
    for item in reversed(stages):
        if str(item.get("status") or "").upper() not in {
            "PENDING",
            "SKIPPED",
        }:
            return str(item.get("stage") or "").upper()
    return str(job.get("stage") or "STRUCTURE_REVIEW").upper()


def _job_progress(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "total_clauses",
        "total_targets",
        "total_chunks",
        "completed_chunks",
        "completed_targets",
        "successful_chunks",
        "unresolved_chunks",
        "blocked_chunks",
        "current_chunk_index",
        "current_chunk_target_count",
    )
    return {key: job.get(key, 0) for key in keys}


def _current_page(
    job: dict[str, Any],
    payload: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> int | None:
    explicit = job.get("current_page")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    pages = [
        int(item.get("page_end") or item.get("page_start") or 0)
        for item in _dict_items(payload.get("review_groups"))
    ]
    if not pages:
        pages = [
            int(item.get("page_end") or item.get("page_start") or 0) for item in nodes
        ]
    return max(pages, default=0) or None


def _current_chapter(
    payload: dict[str, Any],
    *,
    current_page: int | None,
) -> str:
    groups = _dict_items(payload.get("review_groups"))
    if not groups:
        return ""
    if current_page is not None:
        matching = [
            item
            for item in groups
            if int(item.get("page_start") or 0)
            <= current_page
            <= int(item.get("page_end") or item.get("page_start") or 0)
        ]
        if matching:
            return _compact_label(str(matching[-1].get("title") or ""))
    return _compact_label(str(groups[-1].get("title") or ""))


def _last_progress(
    *,
    job_status: str,
    stage: str,
    progress: dict[str, Any],
) -> str:
    completed = int(progress.get("completed_chunks") or 0)
    total = int(progress.get("total_chunks") or 0)
    if total:
        return f"{stage}: {completed}/{total} chunks"
    return f"{stage}: {job_status}"


def _elapsed_seconds(job: dict[str, Any]) -> int:
    started = _parse_datetime(job.get("started_at_utc"))
    if started is None:
        return 0
    finished = _parse_datetime(job.get("finished_at_utc"))
    end = finished or datetime.now(timezone.utc)
    return max(0, int((end - started).total_seconds()))


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed
