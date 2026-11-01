"""An autonomous source-only entry using the same frozen ReqPilotX tools.

The host copies an uploaded original, exposes tools, and verifies the Agent's
completed graph. It does not capture/map/classify through a scheduled stage DAG.
"""

from __future__ import annotations
from reqpilotx_server.saas.context import owned_task_identity

import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from reqpilotx_server.adapters.agents.codex_app_server.native_loop_driver import (
    LocalCodexAppServerNativeLoopDriver,
    _write_json,
)
from reqpilotx_server.adapters.agents.provider_tool_loop import (
    build_provider_tool_loop_driver,
    provider_loop_ready,
)
from reqpilotx_server.infrastructure.runtime_settings import AgentRuntimeBackend
from reqpilotx_server.application.agent_workflow.native_loop_tools import NativeLoopToolHost
from reqpilotx_server.application.agent_workflow.native_loop_progress import native_tool_activity
from reqpilotx_server.application.agent_workflow.native_loop_workspace import (
    export_registered_knowledge,
)
from reqpilotx_server.application.agent_workflow.native_source_graph import (
    EXTRACTION_JOB_KIND,
    load_native_source_graph,
    seal_native_source_graph,
)
from reqpilotx_server.application.agent_workflow.native_structure_bridge import (
    _document_source_sha256,
    _no_store_headers,
    _require_project_document,
)
from reqpilotx_server.application.agent_workflow.source_item_loop_jobs import (
    ACTIVE,
    ENTRYPOINT,
    LoopControlRequestV1,
    LoopResumeRequestV1,
    LoopSteerRequestV1,
    _existing,
    _locked,
    _portable,
    _read,
    _task_root,
    get_source_item_loop_events,
)
from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import (
    _container,
    _source_context,
    _write_private,
)
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    canonical_json_sha256,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.source_runtime import resolve_source_record_path
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.ports.job_queue import NonRetryableJobError


SCHEMA = "source-document-loop-task/v1"


class SourceDocumentLoopStartV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=160)
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    goal: str = Field(min_length=1, max_length=12000)
    origin: Literal["USER_REQUEST", "AGENT_SELF_REVIEW"] = "USER_REQUEST"
    analysis_mode: Literal["AUTO", "SKIP"] = "AUTO"
    require_map_confirmation: bool = False
    model_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$", exclude_if=lambda value: value is None)
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(default=None, exclude_if=lambda value: value is None)


def _raw_existing(project_id, document_id, task_id):
    container, root, meta = _existing(project_id, document_id, task_id)
    if meta.get("job_kind") != EXTRACTION_JOB_KIND:
        raise ApplicationError(
            status_code=404, detail="当前文档没有这个原文件提取任务。"
        )
    return container, root, meta


def _head(root):
    path = root / "workspace/review/workspace-manifest.json"
    return _read(path) if path.exists() else None


def _public(container, root, meta):
    job = (
        container.jobs.get(meta["latest_job_id"]) if meta.get("latest_job_id") else None
    )
    runtime_path = root / "runtime/run-state.json"
    runtime = _read(runtime_path) if runtime_path.exists() else {}
    head = _head(root)
    status = job.status.value if job else "PREPARING"
    non_cancellable = bool(job and job.progress.get("non_cancellable"))
    from reqpilotx_server.application.agent_workflow.source_document_stages import stage_inventory, public_stage
    inventory = stage_inventory(root)
    stages = inventory['current']
    confirmed = _read(root / "map-confirmation.json") if (root / "map-confirmation.json").exists() else {}
    return {
        "schema_version": SCHEMA,
        "stages": [public_stage(item) for item in stages.values()],
        "retained_stages": [public_stage(item) for item in inventory['retained']],
        "analysis_policy": meta.get("analysis_policy"),
        "requested_model_id": meta.get("request", {}).get("model_id"),
        "requested_reasoning_effort": meta.get("request", {}).get("reasoning_effort"),
        "map_confirmation_required": bool(meta.get("request", {}).get("require_map_confirmation") and stages.get("MAP")
            and confirmed.get("map_sha256") != stages["MAP"]["artifact_sha256"]),
        "task_id": meta["task_id"],
        "goal": meta.get("request", {}).get("goal", ""),
        "updated_at": job.updated_at.isoformat() if job else None,
        "job_id": job.job_id if job else None,
        "job_status": status,
        "runtime_status": runtime.get("status", "NOT_STARTED"),
        "thread_id": runtime.get("thread_id"),
        "turn_id": runtime.get("turn_id"),
        "source_sha256": meta["source_sha256"],
        "origin": meta["origin"],
        "working_baseline_sha256": head["current"]["baseline_sha256"] if head else None,
        "review_sha256": head["review_sha256"] if head else None,
        "progress": job.progress if job else {},
        "result": job.result if job and status == "SUCCEEDED" else None,
        "business_status": (job.result or {}).get("status")
        if job and status == "SUCCEEDED"
        else None,
        "error_code": job.error_code if job else "",
        "error_message": job.error_message if job else "",
        "can_steer": not (status in ACTIVE and non_cancellable),
        "can_interrupt": status in ACTIVE and not non_cancellable,
        "can_resume": status not in ACTIVE,
        "is_human_accepted": False,
        "prior_answers_provided": False,
        "plugin_bundle_sha256": meta["plugin_bundle_sha256"],
        "configured_plugin_bundle_sha256": meta.get("active_plugin_bundle_sha256")
        or meta["plugin_bundle_sha256"],
        "active_capability_epoch_id": (runtime.get("capability_epoch") or {}).get(
            "epoch_id"
        ),
        "configured_capability_epoch_id": (meta.get("capability_epoch") or {}).get(
            "epoch_id"
        ),
    }


def _enqueue(container, root, meta, *, command_id, goal, origin):
    from reqpilotx_server.application.agent_workflow.provider_continuation import saved_predecessor
    identity = canonical_json_sha256(
        {
            "task_id": meta["task_id"],
            "command_id": command_id,
            "goal": goal,
            "origin": origin,
        }
    )
    existing = container.jobs.get_by_idempotency(
        kind=EXTRACTION_JOB_KIND, idempotency_key=identity
    )
    if existing:
        return existing
    graph = load_native_source_graph(
        meta["project_id"],
        meta["document_id"],
        meta["source_sha256"],
        container=container,
    )
    baseline_sha, map_sha, graph_predecessor = None, None, None
    if graph is not None:
        _, bundle, _, _ = _source_context(meta["project_id"], meta["document_id"], ())
        baseline_sha = bundle["baseline"].artifact_sha256
        map_sha = bundle["document_map"].artifact_sha256
        publication = getattr(graph, "publication", None)
        if not publication:
            raise ValueError(
                "Current source graph lacks its actual publication pointer"
            )
        graph_predecessor = {
            "graph_pointer": publication["graph_pointer"],
            "artifact_sha256": graph["artifact_sha256"],
            "review_sha256": graph["host_verification"]["review"]["review_sha256"],
            "baseline_sha256": baseline_sha,
            "document_map_sha256": map_sha,
        }
        try:
            _require_review_predecessor(root, graph_predecessor)
        except ValueError as exc:
            raise ApplicationError(
                status_code=409,
                detail="当前任务的 review 未包含最新应用版本；已保留现有结果，未启动 Agent。",
            ) from exc
    repository = getattr(container, "agent_structure_loop_repository", None)
    structure_head = (
        repository.get_working_revision_head(
            project_id=meta["project_id"],
            document_id=meta["document_id"],
            source_sha256=meta["source_sha256"],
        )
        if repository is not None
        else None
    )
    job = container.jobs.enqueue(
        kind=EXTRACTION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=meta["document_id"],
        idempotency_key=identity,
        priority=24,
        max_attempts=1,
        payload={
            **saved_predecessor(container, meta),
            "application_entrypoint": ENTRYPOINT,
            "project_id": meta["project_id"],
            "document_id": meta["document_id"],
            "source_sha256": meta["source_sha256"],
            "task_id": meta["task_id"],
            "command_id": command_id,
            "goal": goal,
            "origin": origin,
            "phase": "REVIEW" if graph is not None else "INITIAL_SOURCE",
            "baseline_sha256": baseline_sha,
            "document_map_artifact_sha256": map_sha,
            "native_graph_predecessor": graph_predecessor,
            "structure_head_revision_sha256": structure_head.revision_sha256
            if structure_head
            else None,
            "operation_namespace": "working",
            "publication_mode": "WORKING_PROPOSAL",
            "capability_epoch": meta.get("capability_epoch"),
            **(
                {
                    "plugin_bundle_sha256": meta.get(
                        "active_plugin_bundle_sha256"
                    )
                    or meta.get("plugin_bundle_sha256")
                }
                if meta.get("active_plugin_bundle_sha256")
                or meta.get("plugin_bundle_sha256")
                else {}
            ),
        },
    )
    meta["latest_job_id"] = job.job_id
    _write_json(root / "task.json", meta)
    return job


def _require_review_predecessor(root, predecessor):
    """A retained task must descend from the actual published source graph."""
    review = root / "workspace/review"
    head, seen = _head(root), set()
    graph_found, baseline_found = False, False
    while head and head["review_sha256"] not in seen:
        digest = head["review_sha256"]
        if (
            canonical_json_sha256(
                {k: v for k, v in head.items() if k != "review_sha256"}
            )
            != digest
        ):
            raise ValueError("Review history changed before publication continuation")
        seen.add(digest)
        graph_found |= digest == predecessor["review_sha256"]
        current = head["current"]
        baseline_found |= (
            current["baseline_sha256"] == predecessor["baseline_sha256"]
            and current["document_map_sha256"] == predecessor["document_map_sha256"]
        )
        previous = head.get("predecessor_review_sha256")
        head = _read(review / "reviews" / (previous + ".json")) if previous else None
    if not graph_found or not baseline_found:
        raise ValueError(
            "Task review does not contain the actual application predecessor; preserve external revisions before continuing"
        )


def _record_review_feedback(root, entries):
    head = _head(root)
    if head is None:
        return None
    pending = [entry for entry in entries if not entry.get("review_recorded")]
    if not pending:
        return None
    path = root / ("host-feedback-" + uuid4().hex + ".json")
    _write_json(
        path,
        {
            "expected_review_sha256": head["review_sha256"],
            "feedback": [
                {
                    "origin": "USER"
                    if entry["origin"] == "USER_REQUEST"
                    else "AGENT_SELF_REVIEW",
                    "text": entry["text"],
                    "source_item_ids": entry.get("source_item_ids", []),
                }
                for entry in pending
            ],
        },
    )
    result = _portable(
        root, "review-feedback", review_dir=root / "workspace/review", input=path
    )
    for entry, feedback_id in zip(pending, result["added_feedback_ids"], strict=True):
        entry.update(review_recorded=True, feedback_id=feedback_id)
    return result


def start_source_document_loop(
    project_id: str,
    document_id: str,
    payload: SourceDocumentLoopStartV1,
    response: ResponseSink,
):
    response.headers.update(_no_store_headers())
    container = _container()
    document = _require_project_document(project_id, document_id)
    if _document_source_sha256(document) != payload.expected_source_sha256:
        raise ApplicationError(status_code=409, detail="上传的原文件版本已改变。")
    provider_runtime = (
        getattr(container.settings, "agent_runtime_backend", None)
        is AgentRuntimeBackend.PROVIDER_TOOL_LOOP
    )
    if not container.settings.agent_customer_data_enabled or (
        not provider_loop_ready(container.settings)
        if provider_runtime
        else not container.settings.codex_bin or not container.settings.codex_home
    ):
        raise ApplicationError(status_code=503, detail="自主 Agent 运行环境尚未配置。")
    task_id = (
        "loop-"
        + canonical_json_sha256(
            {
                "mode": "RAW_SOURCE",
                "project_id": project_id,
                "document_id": document_id,
                **payload.model_dump(mode="json"),
            }
        )[:32]
    )
    root = _task_root(container, project_id, document_id, task_id)
    if (root / "task.json").exists():
        return _public(container, root, _read(root / "task.json"))
    if 'require_map_confirmation' not in payload.model_fields_set:
        # Replaying an old omitted-policy creation command must find its original
        # task, whose request hash was formed under the former True default.
        legacy_task_id='loop-'+canonical_json_sha256({
            'mode':'RAW_SOURCE','project_id':project_id,'document_id':document_id,
            **payload.model_dump(mode='json'),'require_map_confirmation':True})[:32]
        legacy_root=_task_root(container,project_id,document_id,legacy_task_id)
        if (legacy_root/'task.json').exists():
            return _public(container,legacy_root,_read(legacy_root/'task.json'))
    if (
        load_native_source_graph(
            project_id, document_id, payload.expected_source_sha256, container=container
        )
        is not None
    ):
        raise ApplicationError(
            status_code=409,
            detail="当前原文件已经形成工作图谱，请继续原任务或使用已有版本修订入口。",
        )
    repository = getattr(container, "agent_structure_loop_repository", None)
    if (
        repository is not None
        and repository.get_working_revision_head(
            project_id=project_id,
            document_id=document_id,
            source_sha256=payload.expected_source_sha256,
        )
        is not None
    ):
        raise ApplicationError(
            status_code=409,
            detail="当前文档已有原文结构工作版，请使用同源版本修订入口。",
        )
    latest = container.jobs.get_latest(
        kind=EXTRACTION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if (
        latest
        and latest.status.value in ACTIVE
        and latest.payload.get("task_id") != task_id
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "message": "当前原文件已有执行中的自主任务。",
                "task_id": latest.payload.get("task_id"),
            },
        )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _locked(root):
        if (root / "task.json").exists():
            return _public(container, root, _read(root / "task.json"))
        if (root / "workspace").exists() or (root / "plugin").exists():
            raise ApplicationError(
                status_code=409,
                detail="这个任务的准备过程未完成，保留现场供恢复；请勿重复覆盖。",
            )
        return _prepare_source_document_loop(
            container, root, task_id, project_id, document_id, document, payload
        )


def _prepare_source_document_loop(
    container,
    root: Path,
    task_id: str,
    project_id: str,
    document_id: str,
    document: dict,
    payload: SourceDocumentLoopStartV1,
):
    workspace = root / "workspace"
    (workspace / "source").mkdir(parents=True, mode=0o700)
    source = resolve_source_record_path(document).resolve(strict=True)
    source_name = Path(document.get("file_name") or source.name).name
    target = workspace / "source" / source_name
    shutil.copyfile(source, target)
    target.chmod(0o600)
    if (
        hashlib.sha256(target.read_bytes()).hexdigest()
        != payload.expected_source_sha256
    ):
        raise ValueError("Uploaded source changed while creating the native task")
    from reqpilotx_server.application.agent_workflow.native_loop_capabilities import (
        prepare_native_loop_capability,
    )

    try:
        capability = prepare_native_loop_capability(root, container.settings)
    except (OSError, ValueError) as exc:
        raise ApplicationError(
            status_code=503,
            detail=f"原生任务能力包准备失败：{exc}",
        ) from exc
    from reqpilotx_server.application.knowledge_facts import build_project_knowledge_catalog

    knowledge = workspace / "knowledge"
    knowledge.mkdir(mode=0o700)
    catalog = build_project_knowledge_catalog(project_id)
    from reqpilotx_server.application.agent_workflow.source_document_stages import analysis_policy
    policy = analysis_policy(catalog, payload.analysis_mode)
    _write_json(knowledge / "knowledge-catalog.json", catalog)
    export_registered_knowledge(catalog, knowledge)
    meta = owned_task_identity({
        "schema_version": SCHEMA,
        "job_kind": EXTRACTION_JOB_KIND,
        "task_id": task_id,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": payload.expected_source_sha256,
        "source_path": str(target.relative_to(workspace)),
        "origin": payload.origin,
        "plugin_bundle_sha256": capability["bundle_sha256"],
        "plugin_capability_source": capability["capability_source"],
        "engine_core_identity": (capability.get("engine_release") or {}).get(
            "core_identity"
        ),
        "request": payload.model_dump(mode="json"),
        "workflow_version": 1,
        "analysis_policy": policy,
        "latest_job_id": None,
        "resume_commands": {},
    })
    _write_json(
        workspace / "task-input.json",
        {
            "source": {"path": meta["source_path"], "sha256": meta["source_sha256"]},
            "knowledge_catalog": "knowledge/knowledge-catalog.json",
            "analysis_policy": policy,
            "require_map_confirmation": payload.require_map_confirmation,
            "prior_answers_provided": False,
            "review_directory": "review",
            "authority": "HOST_BOUND_UPLOAD_AND_REGISTERED_PROJECT_KNOWLEDGE",
        },
    )
    _write_json(root / "task.json", meta)
    _write_json(
        root / "controls.json",
        {
            "steering": [
                {
                    "command_id": payload.command_id,
                    "text": payload.goal,
                    "origin": payload.origin,
                    "source_item_ids": [],
                    "status": "INCLUDED_IN_GOAL",
                    "review_recorded": False,
                }
            ],
            "interrupt_commands": [],
        },
    )
    _enqueue(
        container,
        root,
        meta,
        command_id=payload.command_id,
        goal=payload.goal,
        origin=payload.origin,
    )
    return _public(container, root, meta)


def get_source_document_loop(
    project_id: str, document_id: str, task_id: str, response: ResponseSink
):
    response.headers.update(_no_store_headers())
    container, root, meta = _raw_existing(project_id, document_id, task_id)
    return _public(container, root, meta)


def get_latest_source_document_loop(
    project_id: str, document_id: str, response: ResponseSink
):
    response.headers.update(_no_store_headers())
    _require_project_document(project_id, document_id)
    container = _container()
    job = container.jobs.get_latest(
        kind=EXTRACTION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if not job or job.payload.get("project_id") != project_id:
        return {"schema_version": SCHEMA, "task_id": None, "job_status": "NOT_STARTED"}
    _, root, meta = _raw_existing(project_id, document_id, job.payload["task_id"])
    return _public(container, root, meta)


def get_source_document_loop_events(
    project_id: str,
    document_id: str,
    task_id: str,
    response: ResponseSink,
    after: int = 0,
    limit: int = 100,
):
    _raw_existing(project_id, document_id, task_id)
    return get_source_item_loop_events(
        project_id, document_id, task_id, response, after=after, limit=limit
    )


def steer_source_document_loop(
    project_id: str,
    document_id: str,
    task_id: str,
    payload: LoopSteerRequestV1,
    response: ResponseSink,
):
    response.headers.update(_no_store_headers())
    container, root, meta = _raw_existing(project_id, document_id, task_id)
    with _locked(root):
        job = container.jobs.get(meta["latest_job_id"])
        if job.status.value in ACTIVE and job.progress.get("non_cancellable"):
            raise ApplicationError(
                status_code=409, detail="正在保存本轮图谱，请在保存后继续反馈。"
            )
        controls = _read(root / "controls.json")
        digest = canonical_json_sha256(payload.model_dump(mode="json"))
        previous = next(
            (
                item
                for item in controls["steering"]
                if item["command_id"] == payload.command_id
            ),
            None,
        )
        if previous:
            if previous.get("request_sha256") != digest:
                raise ApplicationError(
                    status_code=409, detail="相同反馈命令不能更换内容。"
                )
            return _public(container, root, meta)
        if payload.source_item_ids and _head(root) is None:
            raise ApplicationError(
                status_code=422, detail="条目尚未形成，可先对整个原文件提出反馈。"
            )
        entry = {
            "command_id": payload.command_id,
            "text": payload.feedback,
            "origin": payload.origin,
            "source_item_ids": list(payload.source_item_ids),
            "status": "PENDING",
            "review_recorded": False,
            "request_sha256": digest,
        }
        controls["steering"].append(entry)
        _record_review_feedback(root, controls["steering"])
        _write_json(root / "controls.json", controls)
        return _public(container, root, meta)


def interrupt_source_document_loop(
    project_id: str,
    document_id: str,
    task_id: str,
    payload: LoopControlRequestV1,
    response: ResponseSink,
):
    response.headers.update(_no_store_headers())
    container, root, meta = _raw_existing(project_id, document_id, task_id)
    with _locked(root):
        controls = _read(root / "controls.json")
        if payload.command_id not in controls["interrupt_commands"]:
            container.jobs.request_cancel(meta["latest_job_id"])
            controls["interrupt_commands"].append(payload.command_id)
            _write_json(root / "controls.json", controls)
        return _public(container, root, meta)


def resume_source_document_loop(
    project_id: str,
    document_id: str,
    task_id: str,
    payload: LoopResumeRequestV1,
    response: ResponseSink,
):
    response.headers.update(_no_store_headers())
    container, root, _ = _raw_existing(project_id, document_id, task_id)
    with _locked(root):
        meta = _read(root / "task.json")
        latest = container.jobs.get(meta["latest_job_id"])
        if latest.status.value in ACTIVE:
            if latest.payload["command_id"] == payload.command_id:
                return _public(container, root, meta)
            raise ApplicationError(
                status_code=409, detail="任务仍在执行，可补充反馈或先停止。"
            )
        document = _require_project_document(project_id, document_id)
        if _document_source_sha256(document) != meta["source_sha256"]:
            raise ApplicationError(
                status_code=409, detail="原文件已改变，不能恢复到另一份来源。"
            )
        digest = canonical_json_sha256(payload.model_dump(mode="json"))
        previous = meta["resume_commands"].get(payload.command_id)
        if previous and previous != digest:
            raise ApplicationError(status_code=409, detail="相同恢复命令不能更换目标。")
        if not previous:
            controls = _read(root / "controls.json")
            controls["steering"].append(
                {
                    "command_id": payload.command_id,
                    "text": payload.goal,
                    "origin": payload.origin,
                    "source_item_ids": list(payload.source_item_ids),
                    "status": "INCLUDED_IN_GOAL",
                    "review_recorded": False,
                }
            )
            _record_review_feedback(root, controls["steering"])
            _write_json(root / "controls.json", controls)
            meta["resume_commands"][payload.command_id] = digest
            _write_json(root / "task.json", meta)
        _enqueue(
            container,
            root,
            meta,
            command_id=payload.command_id,
            goal=payload.goal,
            origin=payload.origin,
        )
        return _public(container, root, meta)


class MapConfirmationV1(LoopControlRequestV1):
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_map_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def get_source_document_stage(project_id: str, document_id: str, task_id: str,
        stage: str, response: ResponseSink, offset: int = 0, limit: int = 100,
        artifact_sha256: str | None = None, stage_version_id: str | None = None):
    response.headers.update(_no_store_headers())
    _, root, meta = _raw_existing(project_id, document_id, task_id)
    if stage not in {"MAP", "ITEMS"} or offset < 0 or not 1 <= limit <= 100:
        raise ApplicationError(status_code=400, detail="阶段或分页参数无效。")
    from reqpilotx_server.application.agent_workflow.source_document_stages import read_stage, public_stage
    try:
        pointer, artifact = read_stage(root, stage, artifact_sha256=artifact_sha256,
            stage_version_id=stage_version_id)
    except (ValueError, OSError) as exc:
        raise ApplicationError(status_code=409, detail="阶段成果不可用或已变化，请同步任务。") from exc
    if pointer["source_sha256"] != meta["source_sha256"]:
        raise ApplicationError(status_code=409, detail="阶段来源不一致。")
    rows = artifact["nodes"] if stage == "MAP" else artifact["records"]
    return {**public_stage(pointer), "stage": stage, "source_sha256": meta["source_sha256"], "artifact_sha256": pointer["artifact_sha256"],
        "total": len(rows), "offset": offset, "rows": rows[offset:offset+limit],
        "next_offset": offset+limit if offset+limit < len(rows) else None,
        "status": "DRAFT", "human_acceptance": "NOT_CLAIMED"}


def confirm_source_document_map(project_id: str, document_id: str, task_id: str,
        payload: MapConfirmationV1, response: ResponseSink):
    response.headers.update(_no_store_headers())
    container, root, meta = _raw_existing(project_id, document_id, task_id)
    from reqpilotx_server.application.agent_workflow.source_document_stages import read_stage
    with _locked(root):
        document = _require_project_document(project_id, document_id)
        if payload.expected_source_sha256 != meta["source_sha256"] or _document_source_sha256(document) != meta["source_sha256"]:
            raise ApplicationError(status_code=409, detail="原文件已变化，请核对当前来源。")
        pointer, _ = read_stage(root, "MAP")
        if pointer["artifact_sha256"] != payload.expected_map_sha256:
            raise ApplicationError(status_code=409, detail="结构已更新，请查看新结构后再确认。")
        _write_json(root / "map-confirmation.json", {"source_sha256": meta["source_sha256"],
            "map_sha256": payload.expected_map_sha256, "command_id": payload.command_id,
            "scope": "STRUCTURE_ONLY", "origin": "USER_REQUEST"})
        latest = container.jobs.get(meta["latest_job_id"])
    goal = "用户已确认当前文档结构，继续形成完整原文条目、分类和必要拆分；按本任务内部依据分支执行，保留阶段成果并完成同版程序导出。"
    if latest.status.value in ACTIVE:
        return steer_source_document_loop(project_id, document_id, task_id,
            LoopSteerRequestV1(command_id=payload.command_id, feedback=goal), response)
    return resume_source_document_loop(project_id, document_id, task_id,
        LoopResumeRequestV1(command_id=payload.command_id, goal=goal), response)


def source_document_goal(user_goal, *, origin, phase, reading_v1, brief=None):
    """One task renderer for production and evaluation, atop the mounted mission skill."""
    from reqpilotx_server.application.agent_workflow.native_task_brief import build_task_brief, render_task_goal
    if brief is None:
        brief = build_task_brief(goal=user_goal,origin=origin,mode=phase)
    return render_task_goal(user_goal,origin=origin,brief=brief,reading_v1=reading_v1)


class SourceDocumentToolHost(NativeLoopToolHost):
    """Only adds actual producer receipts and feedback import at review creation."""

    def __init__(self, *, task_root, meta, **kwargs):
        self.task_root, self.meta = task_root, meta
        super().__init__(review_dir=kwargs["workspace"] / "review", **kwargs)

    def task_brief(self):
        if not self.task_context:
            return None
        from reqpilotx_server.application.agent_workflow.native_task_brief import build_task_brief
        from reqpilotx_server.application.agent_workflow.source_document_stages import stage_inventory
        inputs = _read(self.workspace/'task-input.json') if (self.workspace/'task-input.json').is_file() else {}
        confirmation = _read(self.task_root/'map-confirmation.json') if (self.task_root/'map-confirmation.json').is_file() else {}
        manifest = _read(self.review_dir/'workspace-manifest.json') if (self.review_dir/'workspace-manifest.json').is_file() else {}
        context = {**self.task_context,
            'source':inputs.get('source') or {'path':self.meta.get('source_path'),'sha256':self.meta.get('source_sha256')},
            'initial_goal':self.meta.get('request',{}).get('goal'),
            'analysis_policy':self.meta.get('analysis_policy') or inputs.get('analysis_policy'),
            'project_id':self.meta.get('project_id'),'document_id':self.meta.get('document_id')}
        inventory = stage_inventory(self.task_root)
        return build_task_brief(**context,stages=inventory['current'],retained_stages=inventory['retained'],review=manifest.get('current'),
            require_map_confirmation=self.meta.get('request',{}).get('require_map_confirmation',inputs.get('require_map_confirmation')),
            confirmed_map_sha256=confirmation.get('map_sha256'))

    def invoke(self, name, args):
        from reqpilotx_server.application.agent_workflow.source_document_stages import stage_index, publish_stage
        if name == "reqpilot_run" and self.meta.get("workflow_version") == 1:
            operation = args.get("operation")
            if operation in {"analysis-packet", "compile-assessments"} and self.meta.get("analysis_policy", {}).get("status") == "SKIPPED":
                return self._text({"status": "SKIPPED", "reason": self.meta["analysis_policy"]["reason"],
                    "next": "Skip automatic compliance analysis; preserve extraction, decomposition and manual response work, then export a draft."})
            if operation in {"compile-items", "compile-images", "review-open"} and self.meta.get("request", {}).get("require_map_confirmation"):
                stages = stage_index(self.task_root)
                path = self.task_root / "map-confirmation.json"
                confirmed = _read(path) if path.exists() else {}
                if not stages.get("MAP") or confirmed.get("map_sha256") != stages["MAP"]["artifact_sha256"]:
                    return self._text({"status": "WAITING_FOR_MAP_CONFIRMATION", "next": "The structure is available in the workbench. End this turn with a concise handoff; do not poll or manufacture source items before confirmation."})
            if operation in {"compile-items", "compile-images"} and self.meta.get("request", {}).get("require_map_confirmation"):
                field, filename = ("map_dir", "document-map.json") if operation == "compile-items" else ("baseline_dir", "source-requirement-baseline.json")
                try:
                    input_artifact = _read(self._path(args["arguments"][field]) / filename)
                    actual_map = input_artifact["artifact_sha256"] if operation == "compile-items" else input_artifact["document_map_artifact_sha256"]
                    if actual_map != confirmed.get("map_sha256"):
                        return self._text({"status": "WAITING_FOR_MAP_CONFIRMATION", "next": "Use the confirmed Map, or publish the changed Map for review."})
                except (OSError, KeyError, ValueError) as exc:
                    return {"success": False, "content_items": [{"type": "inputText", "text": str(exc)}]}
        started = time.monotonic()
        result = super().invoke(name, args)
        if name == "reqpilot_run" and result.get("success") is True:
            if args["operation"] in {"compile-items", "compile-images"} and self.meta.get("analysis_policy"):
                directory = self._path(args["arguments"]["output_dir"])
                _write_json(directory / "analysis-policy.json", {"source_sha256": self.meta["source_sha256"], **self.meta["analysis_policy"]})
                self._record_artifacts()
            with _locked(self.task_root):
                stage = publish_stage(self.task_root, self.workspace, args["operation"], args["arguments"], self.meta["source_sha256"])
            if stage:
                from reqpilotx_server.application.agent_workflow.source_document_stages import public_stage
                self._record_artifacts()
                value = json.loads(result["content_items"][0]["text"])
                result = self._text({**value, "published_stage": stage["stage"],
                    "stage_artifact_sha256": stage["artifact_sha256"],
                    "stage_result": public_stage(stage),
                    "next": ("The current draft is saved and available in the workbench. Continue source self-check and scoped Map refinement as needed; use the compact review when returned. Once the requested structure scope is ready for review, hand it off and wait for required Map confirmation before compiling items."
                        if stage['current'] else
                        "The output is saved as a retained stage version, not the current Items. Inspect stage_result for its Map and retention reason; continue within the authorized scope using the current Map or explicitly revisit its saved Map. Do not recompile only to avoid losing this saved output.")})
            receipt = {
                "operation": args["operation"],
                "arguments": args["arguments"],
                "success": True,
                "cli_result_sha256": canonical_json_sha256(result),
            }
            if args["operation"] == "review-open":
                with _locked(self.task_root):
                    controls = _read(self.task_root / "controls.json")
                    imported = _record_review_feedback(
                        self.task_root, controls["steering"]
                    )
                    _write_json(self.task_root / "controls.json", controls)
                self._record_artifacts()
                if imported:
                    opened = json.loads(result["content_items"][0]["text"])
                    result = self._text(
                        {
                            **opened,
                            "opened_review_sha256": opened.get("review_sha256"),
                            "review_sha256": imported["review_sha256"],
                            "host_feedback_import": imported,
                        }
                    )
            runtime_path = self.task_root / "runtime/run-state.json"
            runtime = _read(runtime_path) if runtime_path.exists() else {}
            receipt.update(
                elapsed_milliseconds=round((time.monotonic() - started) * 1000),
                result_sha256=canonical_json_sha256(result),
                attempt_id=runtime.get("attempt_id"),
                thread_id=runtime.get("thread_id"),
                turn_id=runtime.get("turn_id"),
                bundle_sha256=self.meta.get("active_plugin_bundle_sha256")
                or self.meta["plugin_bundle_sha256"],
            )
            with (self.task_root / "producer-events.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        return result


def _producers(root):
    path = root / "producer-events.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def _initial_item_receipt_from_producers(workspace, producers, initial_baseline):
    """Recover the actual text compiler receipt across image-only successors.

    Early portable image output did not copy the text receipt. Its predecessor
    and host-owned producer chain remain authoritative; never select an unrelated
    receipt merely because it exists somewhere in the workspace.
    """
    workspace = workspace.resolve(strict=True)

    def directory(value):
        path = (workspace / value).resolve(strict=True)
        if not path.is_relative_to(workspace):
            raise ValueError("Initial item producer path escaped task workspace")
        return path

    def baseline(path):
        value = _read(path / "source-requirement-baseline.json")
        if any(
            value[key] != initial_baseline[key]
            for key in ("source_sha256", "document_map_artifact_sha256")
        ):
            raise ValueError("Initial item producer source or Map binding changed")
        return value

    openings = []
    for event in producers:
        if event.get("success") is not True or event["operation"] != "review-open":
            continue
        path = directory(event["arguments"]["baseline_dir"])
        value = _read(path / "source-requirement-baseline.json")
        if value["artifact_sha256"] == initial_baseline["artifact_sha256"]:
            if value != initial_baseline:
                raise ValueError(
                    "Initial review differs from its actual opened baseline"
                )
            openings.append(path)
    if not openings:
        raise ValueError("No actual review-open produced the selected initial baseline")
    current_dir, current = openings[-1], initial_baseline
    chain, seen = [], set()
    while current_dir not in seen:
        seen.add(current_dir)
        matching = [
            event
            for event in producers
            if event.get("success") is True
            and event["operation"] in {"compile-items", "compile-images"}
            and directory(event["arguments"]["output_dir"]) == current_dir
        ]
        if not matching:
            raise ValueError("Initial baseline lost its actual item producer chain")
        event = matching[-1]
        map_dir = directory(event["arguments"]["map_dir"])
        if (
            _read(map_dir / "document-map.json")["artifact_sha256"]
            != initial_baseline["document_map_artifact_sha256"]
        ):
            raise ValueError("Initial item producer used a different Map")
        chain.append(
            {
                "operation": event["operation"],
                "output_dir": str(current_dir.relative_to(workspace)),
                "baseline_sha256": current["artifact_sha256"],
                "result_sha256": event.get("result_sha256"),
                "thread_id": event.get("thread_id"),
                "turn_id": event.get("turn_id"),
            }
        )
        if event["operation"] == "compile-items":
            receipt = _read(current_dir / "item-compilation-receipt.json")
            if (
                receipt.get("schema_version") != "reqpilot-item-compilation/v1"
                or receipt.get("source_sha256") != initial_baseline["source_sha256"]
                or receipt.get("baseline_artifact_sha256") != current["artifact_sha256"]
                or receipt.get("baseline_artifact_id") != current["artifact_id"]
            ):
                raise ValueError("Initial item compilation receipt binding mismatch")
            return receipt, list(reversed(chain))
        previous_dir = directory(event["arguments"]["baseline_dir"])
        previous = baseline(previous_dir)
        if current.get("predecessor_artifact_sha256") != previous["artifact_sha256"]:
            raise ValueError("Initial image producer predecessor does not match")
        current_dir, current = previous_dir, previous
    raise ValueError("Initial item producer chain contains a cycle")


def _source_page_return_receipts(root, source_sha256, thread_id):
    """Project successful image bytes, not file existence or model self-report."""
    workspace = (root / "workspace").resolve(strict=True)
    source_hashes, receipts = {}, []
    for path in sorted((root / "runtime/calls").glob("*.json")):
        call = _read(path)
        identity, arguments, response = (
            call.get("identity", {}),
            call.get("arguments", {}),
            call.get("response", {}),
        )
        page = arguments.get("page")
        if (
            call.get("status") != "RETURNED"
            or response.get("success") is not True
            or identity.get("name") != "reqpilot_read"
            or identity.get("thread_id") != thread_id
            or type(page) is not int
            or page < 1
            or not str(arguments.get("path", "")).lower().endswith(".pdf")
        ):
            continue
        source = (workspace / arguments["path"]).resolve(strict=True)
        if not source.is_relative_to(workspace):
            raise ValueError("Page image return source escaped task workspace")
        if source not in source_hashes:
            source_hashes[source] = hashlib.sha256(source.read_bytes()).hexdigest()
        if source_hashes[source] != source_sha256:
            continue
        if identity.get("arguments_sha256") != canonical_json_sha256(arguments):
            raise ValueError("Page image return arguments changed")
        items = response.get("contentItems", [])
        images = [item for item in items if item.get("type") == "inputImage"]
        if not images:
            continue
        metadata = [
            json.loads(item["text"])
            for item in items
            if item.get("type") == "inputText"
        ]
        if len(images) != 1 or len(metadata) != 1:
            raise ValueError("Page image return lacks exact source metadata")
        prefix, separator, encoded = images[0].get("imageUrl", "").partition(",")
        if (
            not separator
            or not prefix.startswith("data:image/")
            or not prefix.endswith(";base64")
        ):
            raise ValueError("Page image return is not an embedded source image")
        image_sha = hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
        expected_metadata = {
            "path": arguments["path"],
            "page": page,
            "image_sha256": image_sha,
        }
        rich = 'source_sha256' in metadata[0]
        if rich:
            import pymupdf
            with pymupdf.open(source) as pdf:
                box = arguments.get('bbox', list(pdf[page-1].rect))
            expected_metadata.update(source_sha256=source_sha256, bbox=box,
                scale=arguments.get('scale',2.0), annotations=arguments.get('annotations',False))
        if metadata[0] != expected_metadata:
            raise ValueError("Page image return bytes differ from source metadata")
        receipts.append(
            {
                "evidence_kind": "TOOL_IMAGE_RETURNED",
                "source_sha256": source_sha256,
                "source_path": arguments["path"],
                "page": page,
                "image_sha256": image_sha,
                "thread_id": thread_id,
                "turn_id": identity["turn_id"],
                "call_id": identity["call_id"],
                "call_receipt_path": str(path.relative_to(root)),
                "call_receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "semantic_verification": "NOT_CLAIMED",
                **({'view_kind':'CROP' if 'bbox' in arguments else 'PAGE',
                    'scale':metadata[0]['scale'], 'annotations':metadata[0]['annotations']} if rich else {}),
            }
        )
    # Native view_image reads are actual image-open events, separate from
    # dynamic tool image returns. Resolve page identity from the sealed index,
    # never from a guessed filename or the model's final answer.
    events_path = root/'runtime/events.jsonl'
    if events_path.is_file():
        pages = {}
        for manifest_path in workspace.rglob('reading-workspace.json'):
            if not manifest_path.is_file():
                continue
            manifest = _read(manifest_path)
            if manifest.get('schema_version') != 'reqpilot-reading-workspace/v1' or manifest.get('source_sha256') != source_sha256:
                continue
            original = (workspace/manifest['source_path']).resolve(strict=True)
            index = (workspace/manifest['pages']).resolve(strict=True)
            if not original.is_relative_to(workspace) or not index.is_relative_to(workspace):
                raise ValueError('Native reading index leaves the task workspace')
            if hashlib.sha256(original.read_bytes()).hexdigest() != source_sha256:
                raise ValueError('Native reading index original changed')
            for page in _read(index):
                render = page['high_render'];image_path=(workspace/render['path']).resolve(strict=True)
                if not image_path.is_relative_to(workspace):
                    raise ValueError('Native page render leaves the task workspace')
                pages[str(image_path)] = (page['page'], render, str(original.relative_to(workspace)))
        for line in events_path.read_text().splitlines():
            event = json.loads(line)
            if event.get('type') != 'native_read_completed' or event.get('name') != 'imageView' or event.get('thread_id') != thread_id:
                continue
            match = pages.get(event.get('path'))
            if match is None:
                continue
            page, render, original = match
            digest = hashlib.sha256(Path(event['path']).read_bytes()).hexdigest()
            if digest != render['sha256'] or digest != event.get('image_sha256'):
                raise ValueError('Native image-open bytes differ from the bound page render')
            receipts.append({'evidence_kind':'NATIVE_IMAGE_OPENED','source_sha256':source_sha256,'source_path':original,
                'page':page,'image_sha256':digest,'thread_id':thread_id,'turn_id':event['turn_id'],'call_id':event['item_id'],
                'view_kind':'PAGE','scale':render['scale'],'annotations':render.get('annotations',False),
                'semantic_verification':'NOT_CLAIMED'})
    return sorted(
        receipts, key=lambda item: (item["page"], item["turn_id"], item["call_id"])
    )


def _verified_capture_chain(root, producers, source_ir_path, source):
    """Resolve actual capture producers, replaying every appended text capture."""
    workspace = (root/'workspace').resolve()
    source = Path(source).resolve(strict=True)
    if not source.is_relative_to(workspace):
        raise ValueError('Source capture original leaves the task workspace')
    current = source_ir_path.resolve(strict=True)
    chain, seen = [], set()
    while True:
        if current in seen or not current.is_relative_to(workspace):
            raise ValueError('Source capture chain cycles or leaves the task workspace')
        seen.add(current)
        matches = [e for e in producers if e['operation'] in {'capture','capture-pdf-text'}
            and (workspace/e['arguments']['output_dir']/'source_ir.json').resolve() == current]
        if not matches:
            raise ValueError('Initial Map does not resolve to an actual source capture tool call')
        event = matches[-1]; arguments = event['arguments']
        if (workspace/arguments['source']).resolve(strict=True) != source:
            raise ValueError('Source capture producer used a different original')
        item = {'operation':event['operation'],'source_ir_path':str(current.relative_to(workspace)),
            'source_ir_sha256':hashlib.sha256(current.read_bytes()).hexdigest()}
        chain.append(item)
        if event['operation'] == 'capture':
            return current.parent, list(reversed(chain))
        parent = (workspace/arguments['source_ir']).resolve(strict=True)
        if not parent.is_relative_to(workspace):
            raise ValueError('Source capture parent leaves the task workspace')
        output = workspace/'.host-validation'/uuid4().hex
        _portable(root,'capture-pdf-text',source=source,source_ir=parent,
            page=arguments.get('page'),query=arguments.get('query'),output_dir=output)
        if _read(output/'source_ir.json') != _read(current):
            raise ValueError('PDF text capture could not be reproduced from its exact original and parent')
        item['original_replay_verified'] = True
        current = parent


def _verified_initial_inputs(root, meta, review_sha):
    workspace, review = root / "workspace", root / "workspace/review"
    verified = _portable(root, "review-verify", review_dir=review)
    if (
        verified["review_sha256"] != review_sha
        or verified["source_sha256"] != meta["source_sha256"]
    ):
        raise ValueError(
            "Initial publication request does not match current review or original source"
        )
    head = _head(root)
    current = review / head["current"]["version_path"]
    producers = _producers(root)
    version_chain = verified.get("version_chain") or [head["current"]]
    original_map_sha = version_chain[0]["document_map_sha256"]
    matching = []
    for event in producers:
        if event["operation"] != "compile-map":
            continue
        directory = (workspace / event["arguments"]["output_dir"]).resolve(strict=True)
        if not directory.is_relative_to(workspace.resolve()):
            raise ValueError("Map producer path escaped task workspace")
        if (
            _read(directory / "document-map.json")["artifact_sha256"]
            == original_map_sha
        ):
            matching.append((event, directory))
    if not matching:
        raise ValueError(
            "No actual Agent compile-map call produced the selected initial Map"
        )
    map_event, map_dir = matching[-1]
    source = (workspace / meta["source_path"]).resolve(strict=True)
    if (workspace / map_event["arguments"]["source"]).resolve(strict=True) != source:
        raise ValueError(
            "Initial Map was not compiled from the authorized uploaded original"
        )
    source_ir_path = (workspace / map_event["arguments"]["source_ir"]).resolve(
        strict=True
    )
    if hashlib.sha256(source.read_bytes()).hexdigest() != meta['source_sha256']:
        raise ValueError('Original source changed before publication')
    capture_dir, capture_chain = _verified_capture_chain(root,producers,source_ir_path,source)
    # Re-run the same frozen deterministic Map compiler on the Agent's original
    # decisions. This is validation of actual decisions, not a semantic stage.
    output = workspace / ".host-validation" / uuid4().hex
    args = {
        key: workspace / value
        for key, value in map_event["arguments"].items()
        if key in {"source", "source_ir", "input", "map_dir"}
    }
    _portable(root, "compile-map", **args, output_dir=output)
    for name in (
        "document-map.json",
        "native-structure-reference.json",
        "table-previews.json",
    ):
        if _read(output / name) != _read(map_dir / name):
            raise ValueError(
                "Frozen Map compiler could not reproduce its actual source projection"
            )
    map_history = []
    previous_map_sha = original_map_sha
    final_reference = _read(map_dir / "native-structure-reference.json")
    initial_baseline = _read(
        review / version_chain[0]["version_path"] / "source-requirement-baseline.json"
    )
    original_item_receipt, item_producer_chain = _initial_item_receipt_from_producers(
        workspace, producers, initial_baseline
    )
    for version in version_chain:
        version_dir = review / version["version_path"]
        if version["document_map_sha256"] == previous_map_sha:
            continue
        revision_input = _read(version_dir / "revision-input.json")
        candidate_dir = (
            review / revision_input["map_revision"]["candidate_map_path"]
        ).resolve(strict=True)
        if not candidate_dir.is_relative_to(review.resolve()):
            raise ValueError(
                "Map transition candidate leaves its verified review workspace"
            )
        transition = {
            "candidate_map": _read(candidate_dir / "document-map.json"),
            "document_map": _read(version_dir / "document-map.json"),
            "native_reference": _read(version_dir / "native-structure-reference.json"),
            "receipt": _read(version_dir / "map-revision-receipt.json"),
        }
        map_history.append(transition)
        final_reference = transition["native_reference"]
        previous_map_sha = version["document_map_sha256"]
    return {
        "head": head,
        "current": current,
        "map_dir": map_dir,
        "capture_dir": capture_dir,
        "source_capture_chain": capture_chain,
        "source_ir_path": source_ir_path,
        "producers": producers,
        "initial_document_map": _read(map_dir / "document-map.json"),
        "initial_native_reference": _read(map_dir / "native-structure-reference.json"),
        "native_reference": final_reference,
        "map_history": map_history,
        "original_item_receipt": original_item_receipt,
        "item_producer_chain": item_producer_chain,
        "verified": verified,
        "map_recompilation": {
            "status": "MATCHED",
            "document_map_sha256": original_map_sha,
        },
    }


def run_durable_source_document_loop(job, context):
    container, root, meta = _raw_existing(
        job.payload["project_id"], job.aggregate_id, job.payload["task_id"]
    )
    if (
        meta["latest_job_id"] != job.job_id
        or job.payload.get("application_entrypoint") != ENTRYPOINT
    ):
        raise NonRetryableJobError(
            error_code="source_loop_job_superseded",
            message="The source-only task delivery is no longer current.",
        )
    request_path = root / ("publish-request-" + job.job_id + ".json")
    if job.payload.get("finalization"):
        from reqpilotx_server.application.agent_workflow.native_loop_finalization import (
            run_native_loop_finalization,
        )

        return run_native_loop_finalization(container, root, meta, job, context)

    def publish_request(review_sha):
        _verified_initial_inputs(root, meta, review_sha)
        if job.payload.get("native_graph_predecessor"):
            _require_review_predecessor(root, job.payload["native_graph_predecessor"])
        _write_json(request_path, {"review_sha256": review_sha, "job_id": job.job_id})
        return {
            "status": "PUBLICATION_REQUEST_RECORDED",
            "application_saved": False,
            "next": "Finish this Agent turn; the host saves only after actual completion and revalidation.",
        }

    from reqpilotx_server.application.agent_workflow.native_loop_capabilities import (
        native_loop_plugin_for_job,
    )

    plugin_root = native_loop_plugin_for_job(root, meta, job.payload)
    host = SourceDocumentToolHost(
        task_root=root,
        meta=meta,
        workspace=root / "workspace",
        plugin_root=plugin_root,
        publish_request=publish_request,
    )

    def steer_requested():
        with _locked(root):
            controls = _read(root / "controls.json")
            item = next(
                (item for item in controls["steering"] if item["status"] == "PENDING"),
                None,
            )
            if item:
                return item["command_id"], "新增已保存的反馈（" + item[
                    "origin"
                ] + "）：" + item["text"] + "。如review已经形成，请重新读取最新反馈。"

    def acknowledge(command, reason=None):
        with _locked(root):
            controls = _read(root / "controls.json")
            item = next(
                item for item in controls["steering"] if item["command_id"] == command
            )
            item["status"] = "FAILED" if reason else "DELIVERED"
            if reason:
                item["reason_code"] = reason
            _write_json(root / "controls.json", controls)

    def activity(event):
        if event["type"] in {"tool_started", "agent_message", "turn/started"}:
            message = event.get("text") or native_tool_activity(event) or "正在处理当前文档"
            context.heartbeat(
                progress={
                    "stage": "NATIVE_SOURCE_AGENT",
                    "task_id": meta["task_id"],
                    "message": message[:1000],
                    "thread_id": event.get("thread_id"),
                    "turn_id": event.get("turn_id"),
                }
            )

    timeout_seconds = 3600
    host.set_task_context(goal=job.payload['goal'],origin=job.payload['origin'],mode=job.payload['phase'],timeout_seconds=timeout_seconds)
    brief = host.task_brief()
    goal = source_document_goal(job.payload["goal"], origin=job.payload["origin"],
                                phase=job.payload["phase"], reading_v1=host.reading_v1, brief=brief)
    from reqpilotx_server.application.agent_workflow.native_task_brief import save_task_brief
    save_task_brief(root, brief, goal)
    if getattr(container.settings, "agent_runtime_backend", None) is AgentRuntimeBackend.PROVIDER_TOOL_LOOP:
        from reqpilotx_server.application.agent_workflow.provider_continuation import build_continuation_handoff
        continuation = build_continuation_handoff(container, root, meta, job,
            verify_review=lambda review: _portable(root, "review-verify", review_dir=review))
        driver = build_provider_tool_loop_driver(
            container.settings, model_id=meta.get("request", {}).get("model_id")
        )
    else:
        driver = LocalCodexAppServerNativeLoopDriver(
            codex_bin=container.settings.codex_bin,
            codex_home=container.settings.codex_home,
            repository_root=container.settings.development_root or root,
            model_id=meta.get("request", {}).get("model_id") or container.settings.agent_model_id,
            reasoning_effort=meta.get("request", {}).get("reasoning_effort") or "high",
        )
    from reqpilotx_server.adapters.agents.codex_app_server.native_loop_pdf import native_loop_skill_paths
    result = driver.run(
        **({"continuation_handoff": continuation} if getattr(container.settings, "agent_runtime_backend", None)
           is AgentRuntimeBackend.PROVIDER_TOOL_LOOP else {}),
        session_dir=root / "runtime",
        goal=goal,
        skill_paths=native_loop_skill_paths(plugin_root, include_pdf=Path(meta.get('source_path', '')).suffix.lower() == '.pdf'),
        skill_bundle_root=plugin_root,
        capability_epoch=job.payload.get("capability_epoch"),
        tool_host=host,
        binding=owned_task_identity({
            "task_id": meta["task_id"],
            "project_id": meta["project_id"],
            "document_id": meta["document_id"],
            "source_sha256": meta["source_sha256"],
            "mode": "RAW_SOURCE",
        }),
        timeout_seconds=timeout_seconds,
        cancellation_requested=context.cancellation_requested,
        steer_requested=steer_requested,
        steer_delivered=acknowledge,
        steer_failed=lambda command, reason: acknowledge(command, reason),
        event_sink=activity,
    )
    if result.status != "COMPLETED" or not request_path.exists():
        return {
            "schema_version": "source-document-loop-result/v1",
            "status": result.status
            if result.status != "COMPLETED"
            else "NEEDS_CONTINUATION",
            "task_id": meta["task_id"],
            "runtime_receipt": dict(result.receipt),
            "application_saved": False,
            "message": result.final_message,
        }
    with _locked(root):
        if any(
            item["status"] == "PENDING"
            for item in _read(root / "controls.json")["steering"]
        ):
            raise NonRetryableJobError(
                error_code="source_loop_feedback_arrived",
                message="New feedback arrived before publication; continue the same source task.",
            )
        request = _read(request_path)
        return _publish_initial_graph(
            container, root, meta, job, context, result.receipt, request
        )


def _publish_initial_graph(
    container, root, meta, job, context, runtime_receipt, request
):
    inputs = _verified_initial_inputs(root, meta, request["review_sha256"])
    from reqpilotx_server.application.agent_workflow.source_item_manual_publication import (
        current_manual_publication_context,
        attach_manual_publication_contexts,
    )

    predecessor = job.payload.get("native_graph_predecessor")
    manual_before = (
        current_manual_publication_context(
            container, meta["project_id"], meta["document_id"]
        )
        if predecessor
        else None
    )
    if job.payload["phase"] == "REVIEW":
        if not predecessor:
            raise ValueError(
                "Graph continuation requires its pinned application predecessor"
            )
        _require_review_predecessor(root, predecessor)
        live_graph = load_native_source_graph(
            meta["project_id"],
            meta["document_id"],
            meta["source_sha256"],
            container=container,
        )
        _, live_bundle, _, _ = _source_context(
            meta["project_id"], meta["document_id"], ()
        )
        if (
            live_graph is None
            or (getattr(live_graph, "publication", None) or {}).get("graph_pointer")
            != predecessor["graph_pointer"]
            or live_bundle["baseline"].artifact_sha256 != predecessor["baseline_sha256"]
            or live_bundle["document_map"].artifact_sha256
            != predecessor["document_map_sha256"]
        ):
            raise ValueError(
                "Application source graph or baseline changed before publication"
            )
    current, map_dir, capture_dir = (
        inputs["current"],
        inputs["map_dir"],
        inputs["capture_dir"],
    )
    values = {
        "source_sha256": meta["source_sha256"],
        "document_map": _read(current / "document-map.json"),
        "baseline": _read(current / "source-requirement-baseline.json"),
        "validation_receipt": _read(current / "validation-receipt.json"),
        "native_reference": inputs["native_reference"],
        "initial_document_map": inputs["initial_document_map"],
        "initial_native_reference": inputs["initial_native_reference"],
        "map_history": inputs["map_history"],
        "table_previews": _read(current / "table-previews.json"),
        "capture_receipt": _read(capture_dir / "manifest.json"),
        "map_compilation_receipt": _read(map_dir / "map-compilation-receipt.json"),
        "item_compilation_receipt": inputs["original_item_receipt"],
        "runtime_receipt": dict(runtime_receipt),
        "producer_receipts": inputs["producers"],
        "host_verification": {
            "review": inputs["verified"],
            "map_recompilation": inputs["map_recompilation"],
            "initial_item_producer_chain": inputs["item_producer_chain"],
            "source_page_returns": _source_page_return_receipts(
                root, meta["source_sha256"], runtime_receipt["thread_id"]
            ),
            **({'source_capture_chain':inputs['source_capture_chain']} if len(inputs.get('source_capture_chain',[])) > 1 else {}),
        },
        "plugin_bundle_sha256": meta.get("active_plugin_bundle_sha256")
        or meta["plugin_bundle_sha256"],
        "initial_plugin_bundle_sha256": meta["plugin_bundle_sha256"],
        "capability_epoch": meta.get("capability_epoch"),
        "predecessor_publication": predecessor,
    }
    expected_scopes = {
        "text:" + node["node_id"]
        for node in values["document_map"]["nodes"]
        if node.get("source_content")
    }
    expected_scopes.update(
        table["scope_id"] for table in values["document_map"]["logical_tables"]
    )
    values["final_scope_coverage"] = {
        "unprocessed_scope_ids": sorted(
            expected_scopes - set(values["baseline"]["covered_scope_ids"])
        )
    }
    for key, name in (
        ("analysis_policy", "analysis-policy.json"),
        ("annotations", "image-annotations.json"),
        ("decomposition", "source-item-decomposition.json"),
        ("assessments", "source-item-assessments.json"),
        ("analysis_packet", "analysis-packet.json"),
        ("knowledge_catalog", "knowledge-catalog.json"),
    ):
        if (current / name).exists():
            values[key] = _read(current / name)
    delivery = root / "workspace/review" / inputs["head"]["delivery"]["path"]
    values["delivery_receipt"] = _read(delivery / "delivery-receipt.json")
    values["matrix_snapshot"] = _read(delivery / "matrix-snapshot.json")
    values["matrix_snapshot_sha256"] = canonical_json_sha256(values["matrix_snapshot"])
    graph = seal_native_source_graph(**values)
    document = _require_project_document(meta["project_id"], meta["document_id"])
    if _document_source_sha256(document) != meta["source_sha256"]:
        raise ValueError("Uploaded source changed before initial graph publication")
    context.begin_publication()
    pointer = _write_private(
        container,
        project_id=meta["project_id"],
        document_id=meta["document_id"],
        namespace="working",
        stage="native-source-graph",
        value=graph,
    )
    result = {
        "schema_version": "source-document-loop-result/v1",
        "status": "SOURCE_GRAPH_READY"
        if graph["document_map"]["evidence_state"] == "SUFFICIENT"
        and not graph["final_scope_coverage"].get("unprocessed_scope_ids")
        else "SOURCE_GRAPH_PARTIAL",
        "task_id": meta["task_id"],
        "source_sha256": meta["source_sha256"],
        "document_map_artifact_sha256": graph["document_map"]["artifact_sha256"],
        "baseline_sha256": graph["baseline"]["artifact_sha256"],
        "baseline_revision": graph["baseline"]["revision"],
        "native_source_graph_artifact": pointer,
        "expected_no_source_graph": predecessor is None,
        "expected_structure_head_verified": True,
        "expected_structure_head_revision_sha256": job.payload.get(
            "structure_head_revision_sha256"
        ),
        "application_saved": True,
        "transport_kind": runtime_receipt.get("transport_kind", "CODEX_APP_SERVER_NATIVE_LOOP"),
        "is_human_accepted": False,
        "runtime_receipt": dict(runtime_receipt),
        "record_count": len(graph["baseline"]["records"]),
        "delivery_receipt": values["delivery_receipt"],
    }
    for key, result_key in (
        ("annotations", "annotations_artifact"),
        ("decomposition", "decomposition_artifact"),
        ("assessments", "assessments_artifact"),
    ):
        if values.get(key) is not None:
            result[result_key] = _write_private(
                container,
                project_id=meta["project_id"],
                document_id=meta["document_id"],
                namespace="working",
                stage="native-source-" + key,
                value=values[key],
            )
    if values.get("analysis_packet") is not None:
        result["analysis_input_artifact"] = _write_private(
            container,
            project_id=meta["project_id"],
            document_id=meta["document_id"],
            namespace="working",
            stage="native-source-analysis-input",
            value={
                "packet": values["analysis_packet"],
                "knowledge_catalog": values["knowledge_catalog"],
            },
        )
    result["source_ir_artifact"] = _write_private(
        container,
        project_id=meta["project_id"],
        document_id=meta["document_id"],
        namespace="working",
        stage="native-source-ir",
        value=_read(inputs["source_ir_path"]),
    )
    if predecessor:
        result.update(
            expected_live_baseline_sha256=predecessor["baseline_sha256"],
            expected_document_map_artifact_sha256=predecessor["document_map_sha256"],
            expected_native_source_graph_object_sha256=predecessor["graph_pointer"][
                "object_sha256"
            ],
            predecessor_native_source_graph_artifact=predecessor["graph_pointer"],
        )
    attach_manual_publication_contexts(
        result,
        container=container,
        project_id=meta["project_id"],
        document_id=meta["document_id"],
        previous_context=manual_before,
        document_map=values["document_map"],
        baseline=values["baseline"],
        table_previews=values["table_previews"],
        decomposition=values.get("decomposition"),
        annotations=values.get("annotations"),
    )
    return result
