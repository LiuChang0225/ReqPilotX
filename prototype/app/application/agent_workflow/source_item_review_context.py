"""Version-bound read projection for source search and engineering review.

Focus annotations are explicit suggestions, never requirements or classifications.
This module does not run a model, change a baseline, or infer tags from keywords.
"""
from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
from app.application.agent_workflow.source_item_visual import _visual_source_evidence
from app.application.transport import ResponseSink

FocusArea = Literal["TECHNICAL_PARAMETERS", "VERIFICATION"]
ANNOTATION_SCHEMA = "source-item-review-annotations/v1"
DECISION_SCHEMA = "source-item-review-annotation-decisions/v1"


class AttentionIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    status: Literal["OPEN", "RESOLVED"] = "OPEN"


class ReviewAnnotationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    focus_areas: tuple[FocusArea, ...] = Field(default=(), max_length=2)
    focus_reason: str = Field(default="", max_length=1000)
    context_focus_areas: tuple[FocusArea, ...] = Field(default=(), max_length=2)
    attention_issues: tuple[AttentionIssue, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def check_focus(self):
        if len(set(self.focus_areas)) != len(self.focus_areas) or len(set(self.context_focus_areas)) != len(self.context_focus_areas):
            raise ValueError("Focus areas must be unique")
        if (self.focus_areas or self.context_focus_areas) and not self.focus_reason.strip():
            raise ValueError("Focus suggestions require a source-specific reason")
        if len({issue.id for issue in self.attention_issues}) != len(self.attention_issues):
            raise ValueError("Attention issue identities must be unique")
        return self


def review_source_evidence_sha256(record):
    return _visual_source_evidence(record)


def _checked_records(document_map, baseline, value):
    if (value.get("source_sha256") != baseline.source_sha256
            or value.get("baseline_sha256") != baseline.artifact_sha256
            or value.get("document_map_sha256") != document_map.artifact_sha256
            or baseline.document_map_artifact_sha256 != document_map.artifact_sha256):
        raise ValueError("Review annotations belong to a different source, baseline or Map")
    known = {r.source_record_id: r for r in baseline.records}
    supplied = value.get("records", {})
    if not isinstance(supplied, dict) or set(supplied) - set(known):
        raise ValueError("Review annotation refers to an unknown SourceItem")
    output = {}
    for sid, raw in supplied.items():
        annotation = ReviewAnnotationEntry.model_validate(raw)
        if annotation.source_evidence_sha256 != review_source_evidence_sha256(known[sid]):
            raise ValueError("Review annotation source evidence changed")
        if known[sid].requirementness.value == "NON_REQUIREMENT" and annotation.focus_areas:
            raise ValueError("Retained non-requirements can carry context focus, not an analysis suggestion")
        output[sid] = annotation.model_dump(mode="json")
    return output


def _scope_key(project_id, document_id, baseline_sha):
    scope = canonical_json_sha256({"project_id": project_id, "document_id": document_id})
    return f"runtime/source-item-review-context/{scope}/{baseline_sha}"


def _read_json(store, key):
    import json
    with store.open(key) as stream:
        return json.load(stream)


def read_review_annotations(store, project_id, document_id, document_map, baseline):
    root = _scope_key(project_id, document_id, baseline.artifact_sha256)
    if store.stat(root + "/head.json") is None:
        return None
    head = _read_json(store, root + "/head.json")
    digest = head.get("artifact_sha256", "")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid review annotation artifact identity")
    value = _read_json(store, root + "/" + digest + ".json")
    if (value.get("schema_version") != ANNOTATION_SCHEMA
            or value.get("project_id") != project_id or value.get("document_id") != document_id
            or value.get("artifact_sha256") != digest
            or canonical_json_sha256({k:v for k,v in value.items() if k != "artifact_sha256"}) != digest):
        raise ValueError("Review annotation artifact is invalid or changed")
    _checked_records(document_map, baseline, value)
    return value


def publish_review_annotations(project_id, document_id, decisions, *, expected_head_sha256=None):
    """Controlled local-host sidecar update, independent of the baseline pointer.

    Immutable versions retain provenance. The head uses an optimistic predecessor
    check; this CLI write is not exposed as an end-user HTTP mutation.
    """
    from app.application.agent_workflow.source_item_delivery_bridge import _current, _container
    if decisions.get("schema_version") != DECISION_SCHEMA:
        raise ValueError("Unsupported review annotation decision contract")
    _, bundle = _current(project_id, document_id)
    baseline, document_map = bundle["baseline"], bundle["document_map"]
    records = _checked_records(document_map, baseline, decisions)
    provenance = decisions.get("provenance", {})
    if not all(str(provenance.get(k, "")).strip() for k in ("kind", "recorded_at", "action_record")):
        raise ValueError("Review annotation provenance is required")
    store = _container().object_store
    previous = read_review_annotations(store, project_id, document_id, document_map, baseline)
    request_sha = canonical_json_sha256(decisions)
    if previous and previous.get("request_sha256") == request_sha:
        return {"artifact_sha256": previous["artifact_sha256"], "revision": previous["revision"], "idempotent": True}
    if (previous or {}).get("artifact_sha256") != expected_head_sha256:
        raise ValueError("Review annotations changed; supply the actual predecessor SHA")
    value = {"schema_version": ANNOTATION_SCHEMA, "project_id": project_id, "document_id": document_id,
        "source_sha256": baseline.source_sha256, "baseline_sha256": baseline.artifact_sha256,
        "document_map_sha256": document_map.artifact_sha256, "records": records,
        "revision": previous["revision"] + 1 if previous else 1,
        "predecessor_sha256": (previous or {}).get("artifact_sha256"), "request_sha256": request_sha,
        "provenance": deepcopy(provenance)}
    digest = canonical_json_sha256(value)
    value["artifact_sha256"] = digest
    root = _scope_key(project_id, document_id, baseline.artifact_sha256)
    def put(key, data):
        import hashlib, json
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return store.put(key=key, source=BytesIO(raw), expected_sha256=hashlib.sha256(raw).hexdigest(), media_type="application/json")
    put(root + "/" + digest + ".json", value)
    _, fresh = _current(project_id, document_id)
    latest = read_review_annotations(store, project_id, document_id, document_map, baseline)
    if fresh["baseline"].artifact_sha256 != baseline.artifact_sha256 or (latest or {}).get("artifact_sha256") != expected_head_sha256:
        raise ValueError("Source or annotation head changed before sidecar publication")
    put(root + "/head.json", {"artifact_sha256": digest})
    return {"artifact_sha256": digest, "revision": value["revision"], "idempotent": False,
            "baseline_sha256": baseline.artifact_sha256, "source_item_count": len(records)}


def _judgment(record):
    if record is None:
        return None
    return {"requirementness": record.requirementness.value,
        "requirement_kinds": [k.value for k in record.requirement_kinds],
        "non_requirement_role": record.non_requirement_role.value if record.non_requirement_role else None,
        "decision_summary": record.decision_summary}


def project_review_context(document_map, baseline, table_previews, *, project_id, document_id,
                           annotations=None, visual_annotations=None, decomposition=None,
                           previous=None, feedbacks=(), reading_projections=None):
    """One row per current SRC; all source text is fetched from existing bindings."""
    if baseline.source_sha256 != document_map.source_sha256 or baseline.document_map_artifact_sha256 != document_map.artifact_sha256:
        raise ValueError("Review context Map does not match the current source")
    if previous and (previous.source_sha256 != baseline.source_sha256
                     or previous.document_map_artifact_sha256 != baseline.document_map_artifact_sha256
                     or previous.artifact_sha256 != baseline.predecessor_artifact_sha256):
        raise ValueError("Review history is not the actual same-source predecessor")
    explicit = _checked_records(document_map, baseline, annotations) if annotations else {}
    visual = {}
    if visual_annotations:
        from app.application.agent_workflow.source_item_visual import rebind_source_item_image_annotations
        checked = rebind_source_item_image_annotations(document_map, baseline, visual_annotations)
        visual = checked["records"]
    children = {}
    if decomposition:
        from app.application.agent_workflow.source_item_decomposition import validate_source_item_decomposition
        validate_source_item_decomposition(baseline, decomposition, table_previews=table_previews)
        children = decomposition["records"]
    cells = {}
    for scope, preview in table_previews.items():
        if preview.get("source_sha256") != baseline.source_sha256 or preview.get("logical_table_scope_id") != scope:
            raise ValueError("Review table belongs to another source or scope")
        cells.update({c["cell_id"]: c["text"] for f in preview["fragments"] for c in f["cells"]})
    content = {c.content_id: c.text for n in document_map.nodes for c in n.source_content}
    old = {r.source_record_id: r for r in previous.records} if previous else {}
    known = {r.source_record_id for r in baseline.records}
    feedback_by_record = {}
    for item in feedbacks:
        if item.get("project_id") != project_id or item.get("document_id") != document_id:
            continue
        if (item.get("source_sha256") != baseline.source_sha256
                or item.get("baseline_artifact_id") != baseline.artifact_id
                or item.get("document_map_artifact_sha256") != document_map.artifact_sha256
                or item.get("source_record_id") not in known):
            raise ValueError("Review feedback is not bound to the current source item version")
        feedback_by_record.setdefault(item["source_record_id"], {})[item["feedback_id"]] = item
    result = {}
    fallback_count = 0
    for record in baseline.records:
        sid = record.source_record_id
        annotation, image_annotation = explicit.get(sid), visual.get(sid, {})
        topics = list(image_annotation.get("topics", []))
        focus, context_focus, focus_reason = [], [], ""
        focus_status = "UNAVAILABLE"
        if annotation is not None:
            focus, context_focus, focus_reason = annotation["focus_areas"], annotation["context_focus_areas"], annotation["focus_reason"]
            focus_status = "EXPLICIT" if focus or context_focus else "EXPLICIT_NONE"
        elif "VERIFICATION" in topics and record.requirementness.value == "REQUIREMENT" and any(k.value in {"QUALITY", "CONSTRAINT", "FUNCTIONAL"} for k in record.requirement_kinds):
            focus, focus_reason = ["VERIFICATION"], "已有验证主题与产品要求关联，建议连同方法、条件及判据重点核对。"
            focus_status = "EXISTING_TOPIC"
            fallback_count += 1
        elif "VERIFICATION" in topics and record.requirementness.value == "NON_REQUIREMENT":
            context_focus, focus_reason = ["VERIFICATION"], "保留的验证参考上下文，不单列为待分析需求。"
            focus_status = "EXISTING_TOPIC"
            fallback_count += 1
        issues = [i for i in (annotation or {}).get("attention_issues", []) if i["status"] == "OPEN"]
        reasons = [i["summary"] for i in issues]
        image_reading = image_annotation.get("visual_interpretation") or {}
        if image_reading.get("unresolved"):
            reasons.append("图片释读含未解决的依据差异或清晰度问题，请结合原图核对。")
        if record.evidence_state in {"CONFLICTING", "INSUFFICIENT"}:
            reasons.append("现有来源证据标记为冲突或不足。")
        row_feedback = list(feedback_by_record.get(sid, {}).values())
        pending_count = sum(f.get("status") in {"PENDING_AGENT", "ACKNOWLEDGED"} for f in row_feedback)
        if pending_count:
            reasons.append(f"有{pending_count}条已保存反馈等待Agent处理。")
        prior = old.get(sid)
        changed, rationale_changed, summary = None, None, "前版条目不可用，变更状态未知。"
        if previous:
            if prior is None:
                changed, summary = True, "本版新增来源条目。"
            else:
                deltas = []
                if (record.requirementness, record.requirement_kinds, record.non_requirement_role) != (prior.requirementness, prior.requirement_kinds, prior.non_requirement_role):
                    deltas.append("需求/非需求判定或类别、内容角色已更新")
                if review_source_evidence_sha256(record) != review_source_evidence_sha256(prior):
                    deltas.append("原文范围或来源绑定已更新")
                if (record.label, record.source_form, record.ancestor_path) != (prior.label, prior.source_form, prior.ancestor_path):
                    deltas.append("原文标签、形式或位置已更新")
                changed, summary = bool(deltas), "；".join(deltas) if deltas else "内容判定与来源范围未变。"
                rationale_changed = prior.decision_summary != record.decision_summary
        own_cells = [c for f in record.table_fragments for c in f.cell_ids]
        if set(own_cells) - set(cells):
            raise ValueError("Review search is missing bound original table cells")
        projections = (reading_projections or {}).get(sid, {})
        texts = [record.source_display_id, record.requirement_display_id or "", prior.requirement_display_id if prior and prior.requirement_display_id else "",
                 record.label, " › ".join(record.ancestor_path)]
        texts += [projections.get(b.content_id, {}).get("display_text", b.text) for b in record.text_bindings]
        texts += [cells[c] for c in dict.fromkeys([*own_cells, *record.shared_header_cell_ids, *record.shared_table_cell_ids]) if c in cells]
        texts += [content[c] for c in record.shared_source_content_ids if c in content]
        texts += [image_reading.get("text", ""), *[c["text"] for c in children.get(sid, [])]]
        result[sid] = {"source_item_id": sid, "source_display_id": record.source_display_id,
            "requirement_display_id": record.requirement_display_id,
            "search_text": "\n".join(dict.fromkeys(t for t in texts if t)), "topics": topics,
            "focus_areas": focus, "context_focus_areas": context_focus, "focus_reason": focus_reason, "focus_status": focus_status,
            "needs_attention": bool(reasons), "attention_reasons": list(dict.fromkeys(reasons)),
            "changed_from_previous": changed, "change_summary": summary, "previous_judgment": _judgment(prior),
            "rationale_changed": rationale_changed, "derived_count": len(children.get(sid, [])),
            "has_visual_interpretation": bool(image_reading.get("text")),
            "feedback_count": len(row_feedback), "pending_feedback_count": pending_count}
    status = "BOUND" if len(explicit) == len(baseline.records) else "PARTIAL" if explicit or fallback_count else "UNAVAILABLE"
    return {"schema_version": "source-item-review-context/v1", "source_sha256": baseline.source_sha256,
        "baseline_sha256": baseline.artifact_sha256, "baseline_revision": baseline.revision,
        "document_map_sha256": document_map.artifact_sha256, "records": result,
        "history_status": "AVAILABLE" if previous else "UNKNOWN", "focus_annotation_status": status,
        "focus_annotation_sha256": (annotations or {}).get("artifact_sha256"),
        "focus_annotation_revision": (annotations or {}).get("revision"),
        "focus_annotation_provenance": deepcopy((annotations or {}).get("provenance")),
        "change_detection_scope": "SOURCE_JUDGMENT_AND_BOUNDARIES; rationale_changed is separate",
        "focus_notice": "建议重点分析，未改变需求类别或义务；无可靠标签的范围保守留空，可补充注释。"}


def get_source_item_review_context(project_id: str, document_id: str, response: ResponseSink,
                                   expected_baseline_sha256: str | None = None):
    from app.application.agent_workflow.source_item_delivery_bridge import _current, _reasoning, _container
    from app.application.agent_workflow.source_item_agent_jobs import _source_context
    from app.application.agent_workflow.source_item_reading import baseline_reading_projections
    from app.application.agent_workflow.source_requirement_jobs import _load_product_baseline_artifact
    from app.application.agent_workflow.source_requirement_reference_bridge import _read_feedbacks
    from app.application.agent_workflow.native_structure_bridge import _no_store_headers
    from app.application.errors import ApplicationError
    response.headers.update(_no_store_headers())
    _, bundle = _current(project_id, document_id)
    baseline = bundle["baseline"]
    if expected_baseline_sha256 and expected_baseline_sha256 != baseline.artifact_sha256:
        raise ApplicationError(status_code=409, detail="原文版本已更新，请同步后读取审阅索引。")
    _, verified, source_path, previews = _source_context(project_id, document_id, tuple(r.source_record_id for r in baseline.records))
    if verified["baseline"].artifact_sha256 != baseline.artifact_sha256:
        raise ApplicationError(status_code=409, detail="读取期间原文版本已更新。")
    container = _container()
    previous = None
    if bundle.get("product_predecessor_pointer") and container.object_store.stat(bundle["product_predecessor_pointer"]["object_key"]) is not None:
        try:
            previous = _load_product_baseline_artifact(container.object_store, bundle["product_predecessor_pointer"],
                project_id=project_id, document_id=document_id).baseline
        except FileNotFoundError:
            pass
        except ValueError as exc:
            raise ApplicationError(status_code=409, detail="前版工件来源或版本绑定不一致。") from exc
    document_map = bundle["document_map"]
    try:
        annotations = read_review_annotations(container.object_store, project_id, document_id, document_map, baseline)
        value = project_review_context(document_map, baseline, previews, project_id=project_id, document_id=document_id,
            annotations=annotations, visual_annotations=bundle.get("image_annotations"),
            decomposition=_reasoning(project_id, document_id, baseline)["decomposition"], previous=previous,
            feedbacks=_read_feedbacks(source_sha256=baseline.source_sha256, baseline_artifact_id=baseline.artifact_id),
            reading_projections=baseline_reading_projections(document_map, baseline.records, source_path))
    except ValueError as exc:
        raise ApplicationError(status_code=409, detail="审阅索引的来源、注释或版本绑定不一致，请同步后重试。") from exc
    configured = bool(container.settings.codex_bin and container.settings.codex_home)
    executor = getattr(container, "source_item_text_executor", None)
    text_ready = configured and executor is not None and executor.readiness_reason() is None
    prefix = f"/api/projects/{project_id}/sor-documents/{document_id}/agent-structure/requirement-baseline"
    value["capabilities"] = {
        "text_scope_revision": {"supported": True, "available": text_ready, "endpoint": prefix + "/revisions/start",
            "scope_format": "text:node_id", "unavailable_reason": "" if text_ready else "当前文字Agent执行器未就绪。"},
        "table_classification_review": {"supported": True, "available": configured, "endpoint": prefix + "/classification/review",
            "limitation": "只复核现有表格条目的需求判定/分类，不修改行组或拆合边界。",
            "unavailable_reason": "" if configured else "当前Agent执行器未配置。"},
        "table_scope_revision": {"supported": False, "available": False,
            "unavailable_reason": "当前范围修订仅支持文字；表格边界问题可保存反馈供后续处理。"}}
    if _current(project_id, document_id)[1]["baseline"].artifact_sha256 != baseline.artifact_sha256:
        raise ApplicationError(status_code=409, detail="读取期间原文版本已更新。")
    value["review_context_sha256"] = canonical_json_sha256(value)
    return value
