"""Application delivery of the current SourceItem graph through the mature matrix.

Reads source, reasoning, and knowledge versions from the application. Neither
browser data nor an experimental case directory can supply a replacement graph.
"""

from __future__ import annotations

from collections import Counter
from urllib.parse import quote

from reqpilotx_server import db
from reqpilotx_server.application.agent_workflow.native_structure_bridge import (
    _document_source_sha256, _no_store_headers, _require_project_document,
)
from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import _load_effective_reference
from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import (
    ANALYSIS_JOB_KIND, DECOMPOSITION_JOB_KIND, _container, _read_private, _source_context,
)
from reqpilotx_server.application.agent_workflow.source_item_delivery import build_delivery_snapshot, export_delivery_xlsx
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.transport import BinaryArtifact, ResponseSink


def get_document_agent_job(project_id: str, document_id: str, job_id: str, response: ResponseSink) -> dict:
    from reqpilotx_server.application.agent_workflow.document_map_jobs import _projection
    response.headers.update(_no_store_headers())
    _require_project_document(project_id, document_id)
    job = _container().jobs.get(job_id)
    if (not job or not job.kind.startswith("agent.") or job.aggregate_id != document_id
            or job.payload.get("project_id") != project_id):
        raise ApplicationError(status_code=404, detail="当前文档没有这个 Agent 任务。")
    return _projection(job)


def _reasoning(project_id, document_id, baseline):
    container = _container()
    filters = {"project_id": project_id, "publication_mode": "WORKING_PROPOSAL",
               "operation_namespace": "working", "baseline_sha256": baseline.artifact_sha256}
    output = {"decomposition": None, "assessments": None, "decomposition_job_id": None,
              "analysis_job_id": None, "notices": [], "active_jobs": []}
    from reqpilotx_server.application.agent_workflow.source_item_loop_jobs import load_native_loop_artifact
    for key, field, job_field in (("decomposition_artifact", "decomposition", "decomposition_job_id"),
                                  ("assessments_artifact", "assessments", "analysis_job_id")):
        value, jid = load_native_loop_artifact(container, project_id, document_id, baseline, key)
        if value is not None:
            output[field], output[job_field] = value, jid
    direct = container.jobs.get_latest_succeeded(kind="agent.source-item.direct-revision",
        aggregate_type="sor-document", aggregate_id=document_id)
    if (output["decomposition"] is None and direct and direct.payload.get("project_id") == project_id and direct.result
            and (direct.result.get("baseline_revision_artifact") or {}).get("baseline_artifact_sha256") == baseline.artifact_sha256
            and direct.result.get("decomposition_artifact")):
        output["decomposition"] = _read_private(container, direct.result["decomposition_artifact"],
            project_id=project_id, document_id=document_id, namespace="working")
    for kind, field, job_field in (
        (DECOMPOSITION_JOB_KIND, "decomposition", "decomposition_job_id"),
        (ANALYSIS_JOB_KIND, "assessments", "analysis_job_id"),
    ):
        latest = container.jobs.get_latest(kind=kind, aggregate_type="sor-document", aggregate_id=document_id)
        if latest and all(latest.payload.get(key) == value for key, value in filters.items()):
            if latest.status.value in {"PENDING", "RUNNING"}:
                output["active_jobs"].append({"job_id": latest.job_id, "kind": kind,
                    "status": latest.status.value, "message": (latest.progress or {}).get("message", "")})
        job = container.jobs.get_latest_succeeded(kind=kind, aggregate_type="sor-document",
                                                 aggregate_id=document_id, payload_filters=filters)
        if not job or not job.result or not job.result.get("artifact"):
            continue
        selected_job = container.jobs.get(output[job_field]) if output[job_field] else None
        if selected_job and selected_job.updated_at > job.updated_at:
            continue
        value = _read_private(container, job.result["artifact"], project_id=project_id,
                              document_id=document_id, namespace="working")
        if value.get("baseline_sha256") != baseline.artifact_sha256:
            raise ApplicationError(status_code=409, detail="分析工件与当前原文版本不一致。")
        if field == "assessments":
            project = db.get_project(project_id) or {}
            expected_decomp = (output["decomposition"] or {}).get("artifact_sha256", "")
            if (value.get("decomposition_sha256", "") != expected_decomp
                    or value.get("knowledge_base_version") != int(project.get("knowledge_base_version") or 0)):
                output["notices"].append("分析单元或内部资料已更新，旧分析保留为历史；当前版本需要重新分析。")
                continue
        output[field], output[job_field] = value, job.job_id
    if output["assessments"] is not None:
        project = db.get_project(project_id) or {}
        current_assessments = output["assessments"]
        if (current_assessments.get("decomposition_sha256", "") != (output["decomposition"] or {}).get("artifact_sha256", "")
                or current_assessments.get("knowledge_base_version") != int(project.get("knowledge_base_version") or 0)):
            output["notices"].append("分析单元或内部资料已更新，旧分析保留为历史；当前版本需要重新分析。")
            output["assessments"], output["analysis_job_id"] = None, None
    return output


def _current(project_id, document_id):
    document = _require_project_document(project_id, document_id)
    bundle = _load_effective_reference(project_id=project_id, document_id=document_id,
                                      source_sha256=_document_source_sha256(document))
    if bundle["baseline"].status == "EMPTY":
        raise ApplicationError(status_code=409, detail="原文条目尚未生成；请先完成原文提取。")
    return document, bundle


def _business_basis(bundle, reasoning=None):
    return (bundle['baseline'].artifact_sha256, bundle['document_map'].artifact_sha256,
        (bundle.get('image_annotations') or {}).get('annotations_sha256', ''),
        ((reasoning or {}).get('decomposition') or {}).get('artifact_sha256', ''),
        ((reasoning or {}).get('assessments') or {}).get('artifact_sha256', ''))


def _read_current_delivery_inputs(project_id, document_id):
    """Read one coherent effective source/reasoning basis, or require a retry."""
    document, bundle = _current(project_id, document_id)
    _, verified, source_path, previews = _source_context(project_id, document_id,
        tuple(r.source_record_id for r in bundle['baseline'].records))
    if _business_basis(bundle) != _business_basis(verified):
        raise ApplicationError(status_code=409, detail='读取期间原文或图注版本已更新，请重试。')
    reasoning = _reasoning(project_id, document_id, bundle['baseline'])
    _, latest = _current(project_id, document_id)
    latest_reasoning = _reasoning(project_id, document_id, latest['baseline'])
    if _business_basis(bundle, reasoning) != _business_basis(latest, latest_reasoning):
        raise ApplicationError(status_code=409, detail='读取期间原文、派生或分析版本已更新，请重试。')
    from reqpilotx_server.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
    reasoning = {**reasoning, 'business_basis_sha256': canonical_json_sha256(_business_basis(bundle, reasoning))}
    return document, bundle, source_path, previews, reasoning


def get_source_item_delivery_state(project_id: str, document_id: str, response: ResponseSink) -> dict:
    response.headers.update(_no_store_headers())
    from reqpilotx_server.adapters.persistence.source_item_manual_decisions import current_manual_decisions
    has_manual = current_manual_decisions(project_id, document_id) is not None
    if has_manual:
        _, bundle, _, previews, reasoning = _read_current_delivery_inputs(project_id, document_id)
    else:
        _, bundle = _current(project_id, document_id)
        reasoning = _reasoning(project_id, document_id, bundle['baseline'])
        previews = None  # Preserve the lightweight PDF read path until needed.
    baseline = bundle["baseline"]
    children = (reasoning["decomposition"] or {}).get("records", {})
    decisions = (reasoning["assessments"] or {}).get("records", {})
    required = [r for r in baseline.records if r.requirementness.value == "REQUIREMENT"]
    unit_count = sum(len(children[r.source_record_id]) if children.get(r.source_record_id) else 1 for r in required)
    pending = {"UNKNOWN", "PENDING_EVIDENCE", "NEEDS_CLARIFICATION"}
    from reqpilotx_server.application.agent_workflow.source_item_manual_decision_api import manual_response_view
    from reqpilotx_server.application.agent_workflow.source_item_manual_decisions import build_manual_decision_context
    from reqpilotx_server.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
    basis_sha = canonical_json_sha256(_business_basis(bundle, reasoning))
    engineer_view = None
    if has_manual:
        manual_context = build_manual_decision_context(bundle['document_map'], baseline, previews,
            decomposition=reasoning['decomposition'], annotations=bundle.get('image_annotations'))
        engineer_view = manual_response_view(project_id, document_id, manual_context)
        engineer_view['business_basis_sha256'] = basis_sha
    effective_decisions = dict(decisions)
    for uid, opinion in (engineer_view or {}).get('records', {}).items():
        if opinion['state'] == 'CURRENT': effective_decisions[uid] = opinion
    decision_counts = dict(Counter(r["decision"] for r in effective_decisions.values()))
    from copy import deepcopy
    from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import load_source_workbench_metadata
    metadata = load_source_workbench_metadata(bundle, project_id, document_id, reasoning.get('decomposition'), response)
    reasoning = deepcopy(reasoning)
    for children in (reasoning.get('decomposition') or {}).get('records', {}).values():
        for child in children:
            child.update(metadata['derived_numbers'].get(child['singular_requirement_id'], {}))
    return {"schema_version": "source-item-delivery-state/v1", "source_sha256": baseline.source_sha256,
            "baseline_sha256": baseline.artifact_sha256, "baseline_revision": baseline.revision,
            "source_item_count": len(baseline.records), "requirement_count": len(required),
            "non_requirement_count": len(baseline.records) - len(required),
            "analysis_unit_count": unit_count, "assessed_unit_count": len(effective_decisions),
            "ai_assessed_unit_count": len(decisions), "engineer_responses": engineer_view, "business_basis_sha256": basis_sha,
            "unassessed_unit_count": unit_count - len(effective_decisions),
            "awaiting_evidence_unit_count": decision_counts.get("PENDING_EVIDENCE", 0),
            "clarification_unit_count": decision_counts.get("NEEDS_CLARIFICATION", 0),
            "pending_unit_count": unit_count - sum(n for key, n in decision_counts.items() if key not in pending),
            "decision_counts": decision_counts, "analysis_policy": bundle.get("analysis_policy"), "scope_summaries": metadata["scope_summaries"], "source_findings": metadata["source_findings"], **reasoning}


def _build_current_delivery(project_id, document_id):
    document, bundle, source_path, previews, reasoning = _read_current_delivery_inputs(project_id, document_id)
    from reqpilotx_server.adapters.persistence.source_item_manual_decisions import current_manual_decisions
    from reqpilotx_server.application.agent_workflow.source_item_manual_decisions import build_manual_decision_context, validate_manual_decisions
    manual = current_manual_decisions(project_id, document_id)
    if manual is not None:
        manual_context = build_manual_decision_context(bundle['document_map'], bundle['baseline'], previews,
            decomposition=reasoning['decomposition'], annotations=bundle.get('image_annotations'))
        try:
            validate_manual_decisions(manual, manual_context)
        except ValueError as exc:
            raise ApplicationError(status_code=409, detail='工程师意见与当前业务版本尚未同步，请刷新后重试。') from exc
    snapshot = build_delivery_snapshot(bundle["document_map"], bundle["baseline"], previews,
        annotations=bundle.get("image_annotations"),
        decomposition=reasoning["decomposition"], assessments=reasoning["assessments"], manual_decisions=manual,
        analysis_policy=bundle.get("analysis_policy"), project_id=project_id, document_id=document_id)
    from reqpilotx_server.application.agent_workflow.source_item_reading import baseline_reading_projections
    from reqpilotx_server.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
    reading = baseline_reading_projections(bundle["document_map"], bundle["baseline"].records, source_path)
    records = {record.source_record_id: record for record in bundle["baseline"].records}
    for node in snapshot["nodes"]:
        item = node.get("source_item") or {}
        source_id = item.get("source_item_id")
        projections = reading.get(source_id)
        if not projections:
            continue
        node["display_text"] = "\n".join(projections[b.content_id]["display_text"] if b.content_id in projections else b.text
                                        for b in records[source_id].text_bindings)
        item["reading_projections"] = projections
    snapshot["delivery_sha256"] = canonical_json_sha256({key: value for key, value in snapshot.items() if key != "delivery_sha256"})
    return snapshot, source_path, document, reasoning


def get_source_item_delivery(project_id: str, document_id: str, response: ResponseSink) -> dict:
    response.headers.update(_no_store_headers())
    snapshot, _, _, reasoning = _build_current_delivery(project_id, document_id)
    return {"schema_version": "source-item-delivery/v1", "snapshot": snapshot,
            "decomposition_job_id": reasoning["decomposition_job_id"],
            "analysis_job_id": reasoning["analysis_job_id"], "notices": reasoning["notices"]}


def export_source_item_delivery(project_id: str, document_id: str,
                               expected_baseline_sha256: str, expected_response_revision: int | None = None,
                               expected_business_basis_sha256: str | None = None) -> BinaryArtifact:
    snapshot, source_path, document, reasoning = _build_current_delivery(project_id, document_id)
    source = snapshot["source_item_baseline"]
    if source["artifact_sha256"] != expected_baseline_sha256:
        raise ApplicationError(status_code=409, detail="原文版本已更新，请刷新清单后重新导出。")
    if expected_response_revision is not None and source.get('manual_decisions_revision', 0) != expected_response_revision:
        raise ApplicationError(status_code=409, detail='工程师工作意见已更新，请同步后重新导出。')
    if expected_business_basis_sha256 is not None and reasoning.get('business_basis_sha256') != expected_business_basis_sha256:
        raise ApplicationError(status_code=409, detail='原文、派生或AI建议已更新，请同步后重新导出。')
    project = db.get_project(project_id) or {}
    data = export_delivery_xlsx(snapshot, source_file_path=str(source_path), source_sha256=source["source_sha256"],
        project_name=project.get("name") or "ReqPilotX", customer_name=project.get("customer_name") or "",
        source_file_name=document.get("file_name") or "")
    filename = f"{document.get('file_name') or 'SourceItems'}-v{source['revision']}-Matrix-DRAFT.xlsx"
    return BinaryArtifact(content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={**_no_store_headers(), "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename),
                 "X-ReqPilotX-Baseline-SHA256": source["artifact_sha256"],
                 "X-ReqPilotX-Delivery-SHA256": snapshot["delivery_sha256"]})
