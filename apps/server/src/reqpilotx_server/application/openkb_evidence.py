"""Native local-host original evidence preparation, never a live model claim.

Project files, customer artifacts and identity are selected by the application.
The browser supplies only version expectations and original IDs issued by this
module. Canonical analysis/engineer response/immutable delivery remain shared.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from io import BytesIO
import hashlib
import importlib.util
import json
import re
from threading import RLock
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from reqpilotx.application.openkb_originals import OriginalEvidenceBridge, OpenKBOriginalError
from reqpilotx.application.agent_workflow.source_item_analysis import build_source_item_analysis_packet, compile_source_item_assessments, validate_source_item_assessments
from reqpilotx.application.agent_workflow.source_requirement_baseline import canonical_json_sha256 as digest
from reqpilotx.domain.knowledge.evidence import compute_evidence_content_hash
from reqpilotx_server import db
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.knowledge_facts import build_project_knowledge_catalog
from reqpilotx_server.application.projects import _require_project
from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import _container, _read_private, _write_private

_LOCK = RLock()
SID = re.compile(r"^[0-9a-f]{32}$")
EVIDENCE_ID = re.compile(r"^original-evidence-[0-9a-f]{64}$")


def _sealed(value):
    return {**value, "record_sha256": digest(value)}


def _key(project_id, document_id, name):
    return "openkb-native/" + digest([project_id, document_id]) + "/" + name


def _put(project_id, document_id, name, value):
    raw = json.dumps(_sealed(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > 32 * 1024 * 1024:
        raise ApplicationError(status_code=413, detail='原件准备记录超出32MiB读取范围，请收窄资料选择；未覆盖已有记录。')
    return _container().object_store.put(key=_key(project_id, document_id, name), source=BytesIO(raw),
        expected_sha256=hashlib.sha256(raw).hexdigest(), media_type="application/json")


def _get(project_id, document_id, name, *, missing=False):
    try:
        with _container().object_store.open(_key(project_id, document_id, name)) as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
    except FileNotFoundError as exc:
        if missing:
            return None
        raise ApplicationError(status_code=404, detail="当前文档没有这个原件准备会话。") from exc
    if len(raw) > 32 * 1024 * 1024:
        raise ApplicationError(status_code=409, detail="准备工件超出读取范围。")
    value = json.loads(raw)
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    if value.get("record_sha256") != digest(body):
        raise ApplicationError(status_code=409, detail="原件准备工件校验失败。")
    return body


def _write_bounded_private(container, **kwargs):
    raw = json.dumps(kwargs['value'], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > 32 * 1024 * 1024:
        raise ApplicationError(status_code=413, detail='准备输入超出32MiB原生工件范围，请选择更少资料或分析单元；未发布准备结果。')
    return _write_private(container, **kwargs)


class ProjectOriginalStore:
    """Adapter over real application-registered files; no second KB database."""
    def __init__(self, project_id, document_ids=None):
        _require_project(project_id)
        self.project_id = project_id
        self.kb = "project-" + digest(project_id)[:32]
        self.document_ids = document_ids
        self._snapshot = None
        self.directory_catalog_sha256 = ""

    def snapshot(self, generation=None):
        if self._snapshot is not None:
            manifest, catalog = self._snapshot
            if generation is not None and generation != manifest["generation"]:
                raise OpenKBOriginalError("项目原件或知识版本已变更，请重新准备。")
            return manifest, catalog
        directory = build_project_knowledge_catalog(self.project_id, as_of=date.today())
        self.directory_catalog_sha256 = directory["catalog_sha256"]
        current = "generation-" + digest(directory)
        catalog = directory
        if self.document_ids is not None:
            selected = set(self.document_ids)
            available = {item["id"] for item in directory["documents"]}
            if len(selected) != len(self.document_ids) or selected - available:
                raise OpenKBOriginalError("所选原件必须是本项目当前登记的唯一资料身份。")
            catalog = {**directory, "selection": "EXPLICIT_DOCUMENTS",
                "documents": [item for item in directory["documents"] if item["id"] in selected],
                "evidence_catalog": {key: item for key, item in directory["evidence_catalog"].items() if item["document_id"] in selected},
                "gaps": [item for item in directory["gaps"] if item.get("document_id") in selected]}
            catalog["catalog_sha256"] = digest({key: item for key, item in catalog.items() if key != "catalog_sha256"})
        if generation is not None and generation != current:
            raise OpenKBOriginalError("项目原件、有效期或知识版本已变更，请重新准备。")
        docs = {item["id"]: item for item in db.list_knowledge_documents(self.project_id)}
        manifest = {"generation": current, "documents": [{"logical_name": docs[item["id"]]["file_name"],
            "record": docs[item["id"]]} for item in catalog["documents"]]}
        self._snapshot = manifest, catalog
        return manifest, catalog

    @property
    def current_generation(self):
        return self.snapshot()[0]["generation"]

    def reference_from_fact(self, fact_id, *, generation=None):
        manifest, catalog = self.snapshot(generation)
        fact = catalog["evidence_catalog"].get(fact_id)
        if fact is None:
            raise OpenKBOriginalError("原件事实不在当前项目范围内。")
        return {"schema_version": "openkb-original-reference/v1", "project_id": self.project_id,
            "kb": self.kb, "generation": manifest["generation"], "document_id": fact["document_id"],
            "source_sha256": fact["source_sha256"], "fact_id": fact_id, "source_ref": fact["source_ref"],
            "quote_start": 0, "quote_end": len(fact["text"]), "excerpt": fact["text"], "text_sha256": fact["text_sha256"]}

    def resolve_reference(self, reference):
        if not isinstance(reference, dict) or set(reference) != {"schema_version", "project_id", "kb", "generation", "document_id", "source_sha256", "fact_id", "source_ref", "quote_start", "quote_end", "excerpt", "text_sha256"}:
            raise OpenKBOriginalError("原件引用结构不完整。")
        if reference["project_id"] != self.project_id or reference["kb"] != self.kb:
            raise OpenKBOriginalError("原件引用不属于本项目。")
        expected = self.reference_from_fact(reference["fact_id"], generation=reference["generation"])
        if expected != reference:
            raise OpenKBOriginalError("原件位置、摘录或版本与实际登记文件不一致。")
        _, catalog = self.snapshot(reference["generation"])
        fact = catalog["evidence_catalog"][reference["fact_id"]]
        if not fact["eligible"]:
            raise OpenKBOriginalError("原件当前不可作为有效依据。")
        return {"evidence_id": "original-evidence-" + digest(reference), "fact_id": fact["fact_id"],
            "excerpt": fact["text"], "source_ref": fact["source_ref"], "source_sha256": fact["source_sha256"],
            "content_hash": compute_evidence_content_hash(fact["text"]), "evidence_contract_version": "evidence-v2",
            "authority": fact["authority"], "claim_role": fact["claim_role"], "applicability": fact["applicability"],
            "semantic_coverage": "NOT_ASSESSED", "human_acceptance": "NOT_CLAIMED"}


class PrepareSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_knowledge_base_version: StrictInt = Field(ge=0)
    knowledge_document_ids: list[str] | None = Field(default=None, max_length=64)
    selected_unit_ids: list[str] | None = Field(default=None, max_length=2048)


class PrepareReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_packet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)


def status(project_id):
    store = ProjectOriginalStore(project_id)
    manifest, catalog = store.snapshot()
    return {"available": True, "identity_mode": "LOCAL_SINGLE_USER", "project_id": project_id,
        "knowledge_base_version": catalog["knowledge_base_version"], "catalog_sha256": catalog["catalog_sha256"],
        "generation": manifest["generation"], "document_count": len(catalog["documents"]),
        "registered_fact_count": len(catalog["evidence_catalog"]), "gaps": catalog["gaps"],
        "originals_registered": bool(catalog['documents']), "facts_prepared": bool(catalog['evidence_catalog']),
        "runtime_available": all(importlib.util.find_spec(name) is not None for name in ("openkb", "agents", "litellm")),
        "live_openkb": "NOT_RUN", "index_status": "NOT_REQUESTED", "human_acceptance": "NOT_CLAIMED"}


def _context(project_id, document_id, document_ids, selected_unit_ids):
    from reqpilotx_server.application.agent_workflow.source_item_delivery_bridge import _read_current_delivery_inputs
    document, bundle, source, previews, reasoning = _read_current_delivery_inputs(project_id, document_id)
    if reasoning.get("active_jobs"):
        raise ApplicationError(status_code=409, detail="分析任务正在更新，请完成并同步后准备原件依据。")
    store = ProjectOriginalStore(project_id, document_ids)
    manifest, catalog = store.snapshot()
    packet = build_source_item_analysis_packet(bundle["document_map"], bundle["baseline"], previews, catalog,
        decomposition=reasoning.get("decomposition"), selected_unit_ids=selected_unit_ids, image_annotations=bundle.get("image_annotations"))
    return store, manifest, packet, {**reasoning, "input_annotations_sha256": digest(bundle.get("image_annotations") or {})}


def prepare_session(project_id, document_id, payload: PrepareSession):
    with _LOCK:
        store, manifest, packet, reasoning = _context(project_id, document_id, payload.knowledge_document_ids, payload.selected_unit_ids)
        if packet["source_sha256"] != payload.expected_source_sha256 or packet["baseline_sha256"] != payload.expected_baseline_sha256 or packet["knowledge_base_version"] != payload.expected_knowledge_base_version:
            raise ApplicationError(status_code=409, detail="原文、依据或分析版本已更新，请同步后重试。")
        sid = uuid4().hex
        args = {"project_id": project_id, "document_id": document_id, "namespace": "working"}
        pointer = _write_bounded_private(_container(), **args, stage="openkb-preparation-input", value=packet)
        value = {"session_id": sid, "project_id": project_id, "document_id": document_id,
            "generation": manifest["generation"], "packet_sha256": packet["packet_sha256"], "packet": pointer,
            "source_sha256": packet["source_sha256"], "baseline_sha256": packet["baseline_sha256"],
            "knowledge_base_version": packet["knowledge_base_version"], "catalog_sha256": packet["catalog_sha256"],
            "directory_catalog_sha256": store.directory_catalog_sha256,
            "knowledge_document_ids": payload.knowledge_document_ids, "unit_ids": packet["selected_unit_ids"]}
        value["annotations_sha256"] = reasoning["input_annotations_sha256"]
        _put(project_id, document_id, "sessions/" + sid + "/binding.json", value)
        _put(project_id, document_id, "sessions/" + sid + "/issued.json", {"references": {}})
        return {key: item for key, item in value.items() if key != "packet"} | {
            "catalog_gaps": packet["catalog_gaps"], "human_acceptance": "NOT_CLAIMED", "live_openkb": "NOT_RUN"}


def _session(project_id, document_id, sid):
    if not SID.fullmatch(str(sid)):
        raise ApplicationError(status_code=404, detail="当前文档没有这个准备会话。")
    value = _get(project_id, document_id, "sessions/" + sid + "/binding.json")
    if value["project_id"] != project_id or value["document_id"] != document_id:
        raise ApplicationError(status_code=404, detail="准备会话不属于当前项目文档。")
    store, manifest, current, reasoning = _context(project_id, document_id, value["knowledge_document_ids"], value["unit_ids"])
    if current["packet_sha256"] != value["packet_sha256"] or manifest["generation"] != value["generation"] or reasoning["input_annotations_sha256"] != value["annotations_sha256"]:
        raise ApplicationError(status_code=409, detail="原文、关联条件或知识依据已变化，请重新准备。")
    packet = _read_private(_container(), value["packet"], project_id=project_id, document_id=document_id, namespace="working")
    if packet != current:
        raise ApplicationError(status_code=409, detail="冻结分析输入与当前实际来源不一致。")
    return value, store, packet, reasoning


def read_original(project_id, document_id, sid, knowledge_document_id):
    with _LOCK:
        value, store, _, _ = _session(project_id, document_id, sid)
        bridge = OriginalEvidenceBridge(store, value["generation"])
        try:
            result = bridge.read_registered_original(knowledge_document_id)
        except OpenKBOriginalError as exc:
            raise ApplicationError(status_code=409, detail=str(exc)) from exc
        issued = _get(project_id, document_id, "sessions/" + sid + "/issued.json")
        issued["references"].update(bridge.issued)
        _session(project_id, document_id, sid)
        _put(project_id, document_id, "sessions/" + sid + "/issued.json", issued)
        return result


def prepare_review(project_id, document_id, sid, payload: PrepareReview):
    with _LOCK:
        value, store, packet, reasoning = _session(project_id, document_id, sid)
        if payload.expected_packet_sha256 != packet["packet_sha256"] or len(set(payload.evidence_ids)) != len(payload.evidence_ids):
            raise ApplicationError(status_code=409, detail="准备输入已变化或引用重复。")
        request_name = "sessions/" + sid + "/prepared/" + digest(payload.model_dump(mode="json")) + ".json"
        replay = _get(project_id, document_id, request_name, missing=True)
        if replay is not None:
            return replay
        issued = _get(project_id, document_id, "sessions/" + sid + "/issued.json")["references"]
        if any(not EVIDENCE_ID.fullmatch(eid) or eid not in issued for eid in payload.evidence_ids):
            raise ApplicationError(status_code=409, detail="只能使用本会话实际读取的登记原件引用。")
        facts = [store.resolve_reference(issued[eid]) for eid in payload.evidence_ids]
        if any(fact['evidence_id'] != eid for fact, eid in zip(facts, payload.evidence_ids)):
            raise ApplicationError(status_code=409, detail="已登记引用ID与实际原件引用不一致。")
        citations = [{"fact_id": fact["fact_id"], "quote": fact["excerpt"]} for fact in facts]
        candidate = {"decisions": [{"unit_id": uid, "decision": "PENDING_EVIDENCE",
            "reason": "已准备登记原件候选；OpenKB模型未运行，尚未核查这些原件是否覆盖整个单元及其适用条件。",
            "evidence": citations, "clarification_question": "请核对原件权威、产品配置、全部条件及完整覆盖；需要时补充受控依据。"} for uid in value["unit_ids"]]}
        input_ref = {**value['packet'], 'producer_origin': 'HOST_ORIGINAL_PREPARATION', 'semantic_validation': 'NOT_PERFORMED', 'model_execution': 'NOT_RUN'}
        artifact = compile_source_item_assessments(packet, candidate, previous=reasoning.get("assessments"), input_packet_ref=input_ref)
        validate_source_item_assessments(packet, artifact)
        pointer = _write_bounded_private(_container(), project_id=project_id, document_id=document_id, namespace="working",
            stage="openkb-preparation", value=artifact)
        receipt = {"schema_version": "source-item-openkb-preparation/v1", "project_id": project_id, "document_id": document_id,
            "session_id": sid, "generation": value["generation"], "packet_sha256": value["packet_sha256"],
            "baseline_sha256": packet["baseline_sha256"], "source_sha256": packet["source_sha256"],
            "decomposition_sha256": packet["decomposition_sha256"], "catalog_sha256": packet["catalog_sha256"],
            "knowledge_base_version": packet["knowledge_base_version"], "artifact": pointer,
            "annotations_sha256": value["annotations_sha256"],
            "assessment_sha256": artifact["artifact_sha256"], "issued_references": [{"evidence_id": eid, "reference": issued[eid]} for eid in payload.evidence_ids],
            "prepared_at": datetime.now(timezone.utc).isoformat(), "semantic_validation": "NOT_PERFORMED",
            "live_openkb": "NOT_RUN", "model_call_count": 0, "human_acceptance": "NOT_CLAIMED"}
        _session(project_id, document_id, sid)
        _put(project_id, document_id, "head.json", receipt)
        _session(project_id, document_id, sid)
        response = {"status": "HOST_PENDING_DRAFT_SAVED", "job_id": None, "assessment_sha256": artifact["artifact_sha256"],
            "pending_unit_count": artifact["pending_unit_count"], "human_acceptance": "NOT_CLAIMED",
            "semantic_validation": "NOT_PERFORMED", "live_openkb": "NOT_RUN"}
        _put(project_id, document_id, request_name, response)
        return response


def current_preparation(project_id, document_id, baseline, decomposition_sha256, *, newer_than=None):
    """Read a host-validated current pending draft for the canonical workbench."""
    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS
    from reqpilotx_server.infrastructure.runtime_settings import DeploymentMode
    if RUNTIME_SETTINGS.deployment_mode not in {DeploymentMode.LOCAL, DeploymentMode.TEST} or getattr(_container(), "object_store", None) is None:
        return None
    head = _get(project_id, document_id, "head.json", missing=True)
    if head is None:
        return None
    if head["project_id"] != project_id or head["document_id"] != document_id:
        raise ApplicationError(status_code=409, detail="原件准备头不属于当前文档。")
    stamp = datetime.fromisoformat(head["prepared_at"])
    if stamp.tzinfo is None or (newer_than is not None and newer_than.tzinfo is None):
        raise ApplicationError(status_code=409, detail="分析版本时间缺少可信时区。")
    if newer_than is not None and stamp <= newer_than:
        return None
    project = _require_project(project_id)
    if head["baseline_sha256"] != baseline.artifact_sha256 or head["source_sha256"] != baseline.source_sha256 or head["decomposition_sha256"] != decomposition_sha256 or head["knowledge_base_version"] != int(project.get("knowledge_base_version") or 0):
        return None
    binding = _get(project_id, document_id, "sessions/" + head["session_id"] + "/binding.json")
    from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import _load_effective_reference
    bundle = _load_effective_reference(project_id=project_id, document_id=document_id, source_sha256=baseline.source_sha256)
    if digest(bundle.get("image_annotations") or {}) != head["annotations_sha256"]:
        return None
    store = ProjectOriginalStore(project_id, binding["knowledge_document_ids"])
    try:
        _, catalog = store.snapshot(head["generation"])
    except (ValueError, FileNotFoundError):
        return None
    if catalog["catalog_sha256"] != head["catalog_sha256"]:
        return None
    packet = _read_private(_container(), binding["packet"], project_id=project_id, document_id=document_id, namespace="working")
    if head['packet_sha256'] != binding['packet_sha256'] or packet['packet_sha256'] != binding['packet_sha256']:
        raise ApplicationError(status_code=409, detail="准备会话、冻结输入与引用回执版本不一致。")
    artifact = _read_private(_container(), head["artifact"], project_id=project_id, document_id=document_id, namespace="working")
    validate_source_item_assessments(packet, artifact)
    if artifact["artifact_sha256"] != head["assessment_sha256"]:
        raise ApplicationError(status_code=409, detail="原件准备结果版本已改变。")
    for item in head['issued_references']:
        resolved = store.resolve_reference(item['reference'])
        if resolved['evidence_id'] != item['evidence_id']:
            raise ApplicationError(status_code=409, detail="原件准备引用身份校验失败。")
        fact = packet['evidence_catalog'].get(resolved['fact_id'])
        if not fact or fact['source_sha256'] != item['reference']['source_sha256'] or fact['source_ref'] != item['reference']['source_ref'] or item['reference']['excerpt'] != fact['text']:
            raise ApplicationError(status_code=409, detail="原件准备引用不属于冻结输入中的实际原文事实。")
        if not any({'fact_id': resolved['fact_id'], 'quote': item['reference']['excerpt']} in decision['evidence'] for decision in artifact['records'].values()):
            raise ApplicationError(status_code=409, detail="引用回执与本版实际判断引用不一致。")
    reference_receipt = {"schema_version": "source-item-openkb-original-reference-receipt/v1",
        "project_id": project_id, "document_id": document_id, "session_id": head['session_id'],
        "generation": head['generation'], "packet_sha256": head['packet_sha256'],
        "assessment_sha256": head['assessment_sha256'], "references": head['issued_references'],
        "producer_origin": "HOST_ORIGINAL_PREPARATION", "semantic_validation": "NOT_PERFORMED", "live_openkb": "NOT_RUN", "human_acceptance": "NOT_CLAIMED"}
    reference_receipt['receipt_sha256'] = digest(reference_receipt)
    return artifact, {key: item for key, item in head.items() if key not in {"artifact", "issued_references"}} | {
        'original_reference_receipt': reference_receipt}
