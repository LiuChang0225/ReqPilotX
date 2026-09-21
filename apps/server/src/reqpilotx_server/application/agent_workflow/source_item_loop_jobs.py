"""Durable autonomous ReqPilotX review tasks using the portable tool workspace."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reqpilotx_server.adapters.agents.codex_app_server.native_loop_driver import (
    LocalCodexAppServerNativeLoopDriver,
    _write_json,
)
from reqpilotx_server.application.agent_workflow.document_map_jobs import _container
from reqpilotx_server.application.agent_workflow.native_loop_tools import NativeLoopToolHost
from reqpilotx_server.application.agent_workflow.native_loop_workspace import (
    export_current_review_input,
)
from reqpilotx_server.application.agent_workflow.native_structure_bridge import (
    _no_store_headers,
    _require_project_document,
)
from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import (
    _source_context,
    _write_private,
    _read_private,
)
from reqpilotx_server.application.agent_workflow.source_item_classification_jobs import (
    _bound_review_feedback,
    _carry_review_feedback,
)
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1,
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineValidationReceiptV1,
    canonical_json_sha256,
)
from reqpilotx_server.application.agent_workflow.source_requirement_jobs import (
    publish_agent_source_item_revision,
    _load_product_baseline_artifact,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.ports.job_queue import NonRetryableJobError

LOOP_JOB_KIND = "agent.source-item.loop"
ENTRYPOINT = "source-item-workbench-v1"
TASK_SCHEMA = "source-item-loop-task/v1"
ACTIVE = {"PENDING", "RUNNING", "CANCEL_REQUESTED"}


class LoopStartRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=160)
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    goal: str = Field(min_length=1, max_length=12000)
    origin: Literal["USER_REQUEST", "AGENT_SELF_REVIEW"] = "USER_REQUEST"
    source_item_ids: tuple[str, ...] = Field(default=(), max_length=512)
    feedback_ids: tuple[str, ...] = Field(default=(), max_length=128)


class LoopSteerRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=160)
    feedback: str = Field(min_length=1, max_length=12000)
    origin: Literal["USER_REQUEST", "AGENT_SELF_REVIEW"] = "USER_REQUEST"
    source_item_ids: tuple[str, ...] = Field(default=(), max_length=512)


class LoopControlRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=160)


class LoopResumeRequestV1(LoopControlRequestV1):
    goal: str = Field(
        default="继续读取当前反馈和已保存工件，完成剩余修订、自查、导出与保存。",
        min_length=1,
        max_length=12000,
    )
    origin: Literal["USER_REQUEST", "AGENT_SELF_REVIEW"] = "USER_REQUEST"
    source_item_ids: tuple[str, ...] = Field(default=(), max_length=512)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _task_root(container, project_id, document_id, task_id):
    if not re.fullmatch(r"loop-[0-9a-f]{32}", task_id):
        raise ApplicationError(status_code=404, detail="未找到这个 Agent 任务。")
    scope = canonical_json_sha256(
        {"project_id": project_id, "document_id": document_id}
    )
    return (
        container.settings.object_root / "runtime/native-loop-tasks" / scope / task_id
    )


@contextmanager
def _locked(root):
    with (root / "task.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _existing(project_id, document_id, task_id):
    _require_project_document(project_id, document_id)
    container = _container()
    root = _task_root(container, project_id, document_id, task_id)
    if not (root / "task.json").is_file():
        raise ApplicationError(status_code=404, detail="当前文档没有这个 Agent 任务。")
    meta = _read(root / "task.json")
    if (meta["project_id"], meta["document_id"], meta["task_id"]) != (
        project_id,
        document_id,
        task_id,
    ):
        raise ApplicationError(status_code=409, detail="任务来源身份不一致。")
    return container, root, meta


def _portable(root, operation, **arguments):
    from reqpilotx_server.application.agent_workflow.native_loop_capabilities import (
        active_native_loop_plugin,
    )

    command = [
        sys.executable,
        str(active_native_loop_plugin(root) / "scripts/reqpilot.py"),
        operation,
    ]
    for name, value in arguments.items():
        if value is not None:
            for item in value if isinstance(value, (list, tuple)) else [value]:
                flag = '--' + name.replace('_', '-')
                command += [flag + '=' + str(item)] if name == 'query' else [flag, str(item)]
    result = subprocess.run(
        command,
        cwd=root,
        text=True,
        capture_output=True,
        timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout)[-12000:])
    return json.loads(result.stdout)


def _record_feedback(root, entries, command_id):
    review = root / "review"
    head = _read(review / "workspace-manifest.json")
    path = root / ("feedback-" + canonical_json_sha256(command_id)[:24] + ".json")
    value = {"expected_review_sha256": head["review_sha256"], "feedback": entries}
    _write_json(path, value)
    return _portable(root, "review-feedback", review_dir=review, input=path)


def _enqueue(
    container,
    root,
    meta,
    *,
    command_id,
    goal,
    expected_baseline_sha256,
    origin=None,
    native_publication=None,
):
    identity = canonical_json_sha256(
        {
            "task_id": meta["task_id"],
            "command_id": command_id,
            "goal": goal,
            "origin": origin or meta["origin"],
        }
    )
    existing = container.jobs.get_by_idempotency(
        kind=LOOP_JOB_KIND, idempotency_key=identity
    )
    if existing:
        return existing
    job = container.jobs.enqueue(
        kind=LOOP_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=meta["document_id"],
        idempotency_key=identity,
        priority=24,
        max_attempts=1,
        payload={
            "application_entrypoint": ENTRYPOINT,
            "project_id": meta["project_id"],
            "document_id": meta["document_id"],
            "task_id": meta["task_id"],
            "source_sha256": meta["source_sha256"],
            "baseline_sha256": expected_baseline_sha256,
            "expected_native_source_graph_object_sha256": (
                (native_publication or {}).get("graph_pointer") or {}
            ).get("object_sha256"),
            "goal": goal,
            "command_id": command_id,
            "origin": origin or meta["origin"],
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


def _public(container, root, meta):
    job = (
        container.jobs.get(meta["latest_job_id"]) if meta.get("latest_job_id") else None
    )
    runtime_path = root / "runtime/run-state.json"
    runtime = _read(runtime_path) if runtime_path.exists() else {}
    controls = _read(root / "controls.json")
    head = _read(root / "review/workspace-manifest.json")
    status = job.status.value if job else "PREPARING"
    active = status in ACTIVE
    non_cancellable = bool(job and job.progress.get("non_cancellable"))
    return {
        "schema_version": TASK_SCHEMA,
        "task_id": meta["task_id"],
        "goal": meta.get("request", {}).get("goal", ""),
        "updated_at": job.updated_at.isoformat() if job else None,
        "job_id": job.job_id if job else None,
        "job_status": status,
        "runtime_status": runtime.get("status", "NOT_STARTED"),
        "thread_id": runtime.get("thread_id"),
        "turn_id": runtime.get("turn_id"),
        "source_sha256": meta["source_sha256"],
        "initial_baseline_sha256": meta["initial_baseline_sha256"],
        "working_baseline_sha256": head["current"]["baseline_sha256"],
        "review_sha256": head["review_sha256"],
        "origin": meta["origin"],
        "progress": job.progress if job else {},
        "result": job.result if job and status == "SUCCEEDED" else None,
        "business_status": (job.result or {}).get("status")
        if job and status == "SUCCEEDED"
        else None,
        "error_code": job.error_code if job else "",
        "error_message": job.error_message if job else "",
        "pending_feedback_count": sum(
            item["status"] == "PENDING" for item in controls["steering"]
        ),
        "open_review_feedback_count": sum(
            item["status"] != "ADDRESSED" for item in head["feedback"]
        ),
        "can_steer": not (active and non_cancellable),
        "can_interrupt": active and not non_cancellable,
        "can_resume": not active,
        "is_human_accepted": False,
        "configured_plugin_bundle_sha256": meta.get("active_plugin_bundle_sha256")
        or meta.get("plugin_bundle_sha256"),
        "active_capability_epoch_id": (runtime.get("capability_epoch") or {}).get(
            "epoch_id"
        ),
        "configured_capability_epoch_id": (meta.get("capability_epoch") or {}).get(
            "epoch_id"
        ),
    }


def start_source_item_loop(
    project_id: str,
    document_id: str,
    payload: LoopStartRequestV1,
    response: ResponseSink,
):
    response.headers.update(_no_store_headers())
    container = _container()
    if (
        not container.settings.codex_bin
        or not container.settings.codex_home
        or not container.settings.agent_customer_data_enabled
    ):
        raise ApplicationError(
            status_code=503, detail="当前自主 Agent 执行器尚未配置。"
        )
    task_id = (
        "loop-"
        + canonical_json_sha256(
            {
                "project_id": project_id,
                "document_id": document_id,
                **payload.model_dump(mode="json"),
            }
        )[:32]
    )
    root = _task_root(container, project_id, document_id, task_id)
    _require_project_document(project_id, document_id)
    if (root / "task.json").exists():
        return _public(container, root, _read(root / "task.json"))
    _, bundle, _, _ = _source_context(project_id, document_id, payload.source_item_ids)
    baseline = bundle["baseline"]
    if (
        baseline.source_sha256 != payload.expected_source_sha256
        or baseline.artifact_sha256 != payload.expected_baseline_sha256
    ):
        raise ApplicationError(
            status_code=409, detail="原文工作版已改变，请刷新后提出修改。"
        )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _locked(root):
        if (root / "task.json").exists():
            return _public(container, root, _read(root / "task.json"))
        if (
            (root / "import").exists()
            or (root / "review").exists()
            or (root / "plugin").exists()
        ):
            raise ApplicationError(
                status_code=409,
                detail="这个任务的准备过程未完成，保留现场供恢复；请勿重复覆盖。",
            )
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
        exported = export_current_review_input(
            project_id,
            document_id,
            root / "import",
            expected_baseline_sha256=baseline.artifact_sha256,
            native_publication=bundle.get("native_publication"),
        )
        _portable(
            root,
            "review-open",
            review_dir=root / "review",
            baseline_dir=root / "import",
            source=exported["source_path"],
        )
        initial_feedback = [
            {
                "origin": "USER"
                if payload.origin == "USER_REQUEST"
                else "AGENT_SELF_REVIEW",
                "text": payload.goal,
                "source_item_ids": list(payload.source_item_ids),
                "rationale": "Application task goal; no human acceptance is claimed.",
            }
        ]
        for feedback in _bound_review_feedback(
            project_id,
            document_id,
            baseline,
            set(payload.source_item_ids)
            or {r.source_record_id for r in baseline.records},
            payload.feedback_ids,
        ):
            initial_feedback.append(
                {
                    "origin": "USER",
                    "text": feedback.get("message")
                    or feedback.get("feedback")
                    or feedback.get("text")
                    or json.dumps(feedback, ensure_ascii=False),
                    "source_item_ids": [feedback["source_record_id"]],
                    "rationale": "Existing source-bound feedback "
                    + feedback["feedback_id"],
                }
            )
        _record_feedback(root, initial_feedback, payload.command_id)
        meta = {
            "schema_version": TASK_SCHEMA,
            "task_id": task_id,
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": baseline.source_sha256,
            "document_map_sha256": bundle["document_map"].artifact_sha256,
            "initial_baseline_sha256": baseline.artifact_sha256,
            "origin": payload.origin,
            "plugin_bundle_sha256": capability["bundle_sha256"],
            "plugin_capability_source": capability["capability_source"],
            "engine_core_identity": (
                capability.get("engine_release") or {}
            ).get("core_identity"),
            "source_item_ids": list(payload.source_item_ids),
            "feedback_ids": list(payload.feedback_ids),
            "request": payload.model_dump(mode="json"),
            "latest_job_id": None,
        }
        _write_json(root / "controls.json", {"steering": [], "interrupt_commands": []})
        _write_json(root / "task.json", meta)
        _enqueue(
            container,
            root,
            meta,
            command_id=payload.command_id,
            goal=payload.goal,
            expected_baseline_sha256=baseline.artifact_sha256,
        )
        return _public(container, root, meta)


def get_source_item_loop(
    project_id: str, document_id: str, task_id: str, response: ResponseSink
):
    response.headers.update(_no_store_headers())
    container, root, meta = _existing(project_id, document_id, task_id)
    if meta.get("job_kind") == "agent.source-item.extract-loop":
        from reqpilotx_server.application.agent_workflow.source_document_loop_jobs import (
            _public as raw_public,
        )

        return _generic_task_view(raw_public(container, root, meta))
    return _public(container, root, meta)


def get_latest_source_item_loop(
    project_id: str, document_id: str, response: ResponseSink
):
    response.headers.update(_no_store_headers())
    _require_project_document(project_id, document_id)
    container = _container()
    candidates = [
        container.jobs.get_latest(
            kind=kind, aggregate_type="sor-document", aggregate_id=document_id
        )
        for kind in (LOOP_JOB_KIND, "agent.source-item.extract-loop")
    ]
    candidates = [
        job for job in candidates if job and job.payload.get("project_id") == project_id
    ]
    job = (
        max(candidates, key=lambda item: getattr(item, "created_at", item.updated_at))
        if candidates
        else None
    )
    if not job or job.payload.get("project_id") != project_id:
        return {
            "schema_version": TASK_SCHEMA,
            "task_id": None,
            "job_status": "NOT_STARTED",
        }
    _, root, meta = _existing(project_id, document_id, job.payload["task_id"])
    if meta.get("job_kind") == "agent.source-item.extract-loop":
        from reqpilotx_server.application.agent_workflow.source_document_loop_jobs import (
            _public as raw_public,
        )

        return _generic_task_view(raw_public(container, root, meta))
    return _public(container, root, meta)


def _generic_task_view(value):
    return {
        **value,
        "schema_version": TASK_SCHEMA,
        "task_mode": "RAW_SOURCE",
        "initial_baseline_sha256": value.get("initial_baseline_sha256"),
    }


def get_source_item_loop_events(
    project_id: str,
    document_id: str,
    task_id: str,
    response: ResponseSink,
    after: int = 0,
    limit: int = 100,
    include_message_deltas: bool = False,
):
    response.headers.update(_no_store_headers())
    _, root, _ = _existing(project_id, document_id, task_id)
    if after < 0 or not 1 <= limit <= 500:
        raise ApplicationError(status_code=422, detail="运行记录游标或数量无效。")
    path = root / "runtime/events.jsonl"
    if not path.exists():
        return {
            "schema_version": "source-item-loop-events/v1",
            "events": [],
            "next_cursor": 0,
        }
    events = []
    with path.open("rb") as stream:
        if after > path.stat().st_size:
            raise ApplicationError(status_code=409, detail="运行记录游标超出当前范围。")
        stream.seek(after)
        while len(events) < limit:
            position = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                stream.seek(position)
                break
            event = json.loads(line)
            allowed_types = {
                "thread/started",
                "turn/started",
                "turn/completed",
                "agent_message",
                "tool_started",
                "tool_completed",
                "steering_delivered",
                "steering_failed",
                "runtime_error",
                "runtime_service_error",
                "runtime_service_status",
                "context_compaction",
                "capability_activated",
            }
            if include_message_deltas:
                allowed_types.update({"agent_message_start", "agent_message_delta"})
            if event.get("type", "").startswith("agent_message") and event.get("phase") not in {None, "commentary", "final", "final_answer", "reply"}:
                continue
            if event.get("type") not in allowed_types:
                continue
            fields = {
                "attempt_id",
                "time_unix",
                "type",
                "thread_id",
                "turn_id",
                "status",
                "phase",
                "message_id",
                "version",
                "delta",
                "text",
                "call_id",
                "name",
                "arguments_sha256",
                "response_sha256",
                "success",
                "command_sha256",
                "reason_code",
                "will_retry",
                "error_sha256",
                "epoch_id",
                "previous_bundle_sha256",
                "bundle_sha256",
                "origin",
                "authorization_reference",
            }
            from reqpilotx_server.application.agent_workflow.native_loop_progress import native_tool_activity
            activity_message = native_tool_activity(event)
            events.append({**{key: value for key, value in event.items() if key in fields},
                **({"message": activity_message} if activity_message else {}), "event_cursor": stream.tell()})
        cursor = stream.tell()
    return {
        "schema_version": "source-item-loop-events/v1",
        "events": events,
        "next_cursor": cursor,
    }


def steer_source_item_loop(
    project_id: str,
    document_id: str,
    task_id: str,
    payload: LoopSteerRequestV1,
    response: ResponseSink,
):
    response.headers.update(_no_store_headers())
    container, root, initial_meta = _existing(project_id, document_id, task_id)
    if initial_meta.get("job_kind") == "agent.source-item.extract-loop":
        from reqpilotx_server.application.agent_workflow.source_document_loop_jobs import (
            steer_source_document_loop,
        )

        return _generic_task_view(
            steer_source_document_loop(
                project_id, document_id, task_id, payload, response
            )
        )
    with _locked(root):
        meta, controls = _read(root / "task.json"), _read(root / "controls.json")
        previous = next(
            (
                item
                for item in controls["steering"]
                if item["command_id"] == payload.command_id
            ),
            None,
        )
        value = payload.model_dump(mode="json")
        if previous:
            if previous["request_sha256"] != canonical_json_sha256(value):
                raise ApplicationError(
                    status_code=409, detail="相同反馈命令不能替换成不同内容。"
                )
            return _public(container, root, meta)
        job = container.jobs.get(meta["latest_job_id"])
        if job and job.status.value in ACTIVE and job.progress.get("non_cancellable"):
            raise ApplicationError(
                status_code=409, detail="正在保存本轮结果，请待保存完成后继续提出修改。"
            )
        receipt = _record_feedback(
            root,
            [
                {
                    "origin": "USER"
                    if payload.origin == "USER_REQUEST"
                    else "AGENT_SELF_REVIEW",
                    "text": payload.feedback,
                    "source_item_ids": list(payload.source_item_ids),
                }
            ],
            payload.command_id,
        )
        controls["steering"].append(
            {
                "command_id": payload.command_id,
                "request_sha256": canonical_json_sha256(value),
                "status": "PENDING",
                "text": payload.feedback,
                "origin": payload.origin,
                "feedback_ids": receipt["added_feedback_ids"],
                "review_sha256": receipt["review_sha256"],
            }
        )
        _write_json(root / "controls.json", controls)
        return _public(container, root, meta)


def interrupt_source_item_loop(
    project_id: str,
    document_id: str,
    task_id: str,
    payload: LoopControlRequestV1,
    response: ResponseSink,
):
    response.headers.update(_no_store_headers())
    container, root, initial_meta = _existing(project_id, document_id, task_id)
    if initial_meta.get("job_kind") == "agent.source-item.extract-loop":
        from reqpilotx_server.application.agent_workflow.source_document_loop_jobs import (
            interrupt_source_document_loop,
        )

        return _generic_task_view(
            interrupt_source_document_loop(
                project_id, document_id, task_id, payload, response
            )
        )
    with _locked(root):
        meta, controls = _read(root / "task.json"), _read(root / "controls.json")
        job = container.jobs.get(meta["latest_job_id"])
        if payload.command_id not in controls["interrupt_commands"]:
            if job and job.status.value in ACTIVE:
                container.jobs.request_cancel(job.job_id)
            controls["interrupt_commands"].append(payload.command_id)
            _write_json(root / "controls.json", controls)
        return _public(container, root, meta)


def resume_source_item_loop(
    project_id: str,
    document_id: str,
    task_id: str,
    payload: LoopResumeRequestV1,
    response: ResponseSink,
):
    response.headers.update(_no_store_headers())
    container, root, initial_meta = _existing(project_id, document_id, task_id)
    if initial_meta.get("job_kind") == "agent.source-item.extract-loop":
        from reqpilotx_server.application.agent_workflow.source_document_loop_jobs import (
            resume_source_document_loop,
        )

        return _generic_task_view(
            resume_source_document_loop(
                project_id, document_id, task_id, payload, response
            )
        )
    with _locked(root):
        meta = _read(root / "task.json")
        latest = (
            container.jobs.get(meta["latest_job_id"])
            if meta.get("latest_job_id")
            else None
        )
        if latest and latest.status.value in ACTIVE:
            if latest.payload["command_id"] == payload.command_id:
                return _public(container, root, meta)
            raise ApplicationError(
                status_code=409, detail="当前任务仍在执行，请直接补充反馈或先停止。"
            )
        _, bundle, _, _ = _source_context(project_id, document_id, ())
        baseline = bundle["baseline"]
        if (
            baseline.source_sha256 != meta["source_sha256"]
            or bundle["document_map"].artifact_sha256 != meta["document_map_sha256"]
        ):
            raise ApplicationError(
                status_code=409, detail="来源或原文组织已改变，请建立同源的新任务。"
            )
        known = {meta["initial_baseline_sha256"]}
        known.update(
            _read(path)["current"]["baseline_sha256"]
            for path in (root / "review/reviews").glob("*.json")
        )
        if baseline.artifact_sha256 not in known:
            raise ApplicationError(
                status_code=409, detail="应用已出现其它修订，请基于最新工作版创建任务。"
            )
        command_log = meta.setdefault("resume_commands", {})
        request_sha = canonical_json_sha256(payload.model_dump(mode="json"))
        if (
            payload.command_id in command_log
            and command_log[payload.command_id] != request_sha
        ):
            raise ApplicationError(
                status_code=409, detail="相同接续命令不能替换为不同内容。"
            )
        if payload.command_id not in command_log:
            _record_feedback(
                root,
                [
                    {
                        "origin": "USER"
                        if payload.origin == "USER_REQUEST"
                        else "AGENT_SELF_REVIEW",
                        "text": payload.goal,
                        "source_item_ids": list(payload.source_item_ids),
                    }
                ],
                payload.command_id,
            )
            command_log[payload.command_id] = request_sha
            _write_json(root / "task.json", meta)
        _enqueue(
            container,
            root,
            meta,
            command_id=payload.command_id,
            goal=payload.goal,
            expected_baseline_sha256=baseline.artifact_sha256,
            origin=payload.origin,
            native_publication=bundle.get("native_publication"),
        )
        return _public(container, root, meta)


def run_durable_source_item_loop(job, context):
    container, root, meta = _existing(
        job.payload["project_id"], job.aggregate_id, job.payload["task_id"]
    )
    if (
        meta["latest_job_id"] != job.job_id
        or job.payload.get("application_entrypoint") != ENTRYPOINT
    ):
        raise NonRetryableJobError(
            error_code="native_loop_job_superseded",
            message="The native task delivery is no longer current.",
        )
    publish_path = root / ("publish-request-" + job.job_id + ".json")
    if job.payload.get("finalization"):
        from reqpilotx_server.application.agent_workflow.native_loop_finalization import (
            run_native_loop_finalization,
        )

        return run_native_loop_finalization(container, root, meta, job, context)

    def publication_request(review_sha):
        verified = _portable(root, "review-verify", review_dir=root / "review")
        if verified["review_sha256"] != review_sha:
            raise ValueError("The review changed before publication request")
        _write_json(publish_path, {"review_sha256": review_sha, "job_id": job.job_id})
        return {
            "status": "PUBLICATION_REQUEST_RECORDED",
            "review_sha256": review_sha,
            "application_saved": False,
            "next": "Finish this Agent turn. Host validates and saves after terminal completion.",
        }

    from reqpilotx_server.application.agent_workflow.native_loop_capabilities import (
        native_loop_plugin_for_job,
    )

    plugin_root = native_loop_plugin_for_job(root, meta, job.payload)
    host = NativeLoopToolHost(
        workspace=root / "review",
        plugin_root=plugin_root,
        publish_request=publication_request,
    )

    def pending_steer():
        with _locked(root):
            controls = _read(root / "controls.json")
            pending = next(
                (item for item in controls["steering"] if item["status"] == "PENDING"),
                None,
            )
            if not pending:
                return None
            return (
                pending["command_id"],
                "新增已保存反馈（"
                + pending["origin"]
                + "）："
                + pending["text"]
                + "。请重新读取review-packet和最新反馈，避免覆盖新反馈。",
            )

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
        if event["type"] in {
            "tool_started",
            "agent_message",
            "turn/started",
            "steering_delivered",
        }:
            from reqpilotx_server.application.agent_workflow.native_loop_progress import native_tool_activity
            message = event.get("text") or native_tool_activity(event) or "正在处理当前文档"
            context.heartbeat(
                progress={
                    "stage": "NATIVE_AGENT",
                    "message": message[:1000],
                    "task_id": meta["task_id"],
                    "thread_id": event.get("thread_id"),
                    "turn_id": event.get("turn_id"),
                }
            )

    driver = LocalCodexAppServerNativeLoopDriver(
        codex_bin=container.settings.codex_bin,
        codex_home=container.settings.codex_home,
        repository_root=container.settings.development_root or root,
        model_id=container.settings.agent_model_id,
    )
    from reqpilotx_server.adapters.agents.codex_app_server.native_loop_pdf import native_loop_skill_paths
    from reqpilotx_server.application.agent_workflow.native_task_brief import render_task_goal, save_task_brief
    timeout_seconds = 1800
    host.set_task_context(goal=job.payload['goal'],origin=job.payload['origin'],mode='REVIEW',
        timeout_seconds=timeout_seconds,source={'sha256':meta['source_sha256']},
        initial_goal=meta.get('request',{}).get('goal'),
        project_id=meta['project_id'],document_id=meta['document_id'],
        focus_ids=meta.get('request',{}).get('source_item_ids',[]))
    brief = host.task_brief()
    goal = render_task_goal(job.payload['goal'],origin=job.payload['origin'],brief=brief,reading_v1=host.reading_v1)
    save_task_brief(root, brief, goal)
    result = driver.run(
        session_dir=root / "runtime",
        goal=goal,
        skill_paths=native_loop_skill_paths(plugin_root, include_pdf=any(host.workspace.rglob('original.pdf'))),
        skill_bundle_root=plugin_root,
        capability_epoch=job.payload.get("capability_epoch"),
        tool_host=host,
        binding={
            "task_id": meta["task_id"],
            "project_id": meta["project_id"],
            "document_id": meta["document_id"],
            "source_sha256": meta["source_sha256"],
            "document_map_sha256": meta["document_map_sha256"],
        },
        timeout_seconds=timeout_seconds,
        cancellation_requested=context.cancellation_requested,
        steer_requested=pending_steer,
        steer_delivered=acknowledge,
        steer_failed=lambda command, reason: acknowledge(command, reason),
        event_sink=activity,
    )
    if result.status != "COMPLETED":
        return {
            "schema_version": "source-item-loop-result/v1",
            "status": result.status,
            "task_id": meta["task_id"],
            "runtime_receipt": dict(result.receipt),
            "application_saved": False,
        }
    if not publish_path.exists():
        return {
            "schema_version": "source-item-loop-result/v1",
            "status": "NEEDS_CONTINUATION",
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
                error_code="native_loop_feedback_arrived",
                message="New feedback arrived before publication; resume the same task.",
            )
        return _publish_loop(
            container, root, meta, job, context, result.receipt, _read(publish_path)
        )


def _publish_loop(
    container, root, meta, job, context, runtime_receipt, request, *, review_path=None
):
    review = review_path or root / "review"
    from reqpilotx_server.application.agent_workflow.source_item_manual_publication import (
        current_manual_publication_context,
        attach_manual_publication_contexts,
    )

    manual_before = current_manual_publication_context(
        container, meta["project_id"], meta["document_id"]
    )
    verified = _portable(root, "review-verify", review_dir=review)
    if verified["review_sha256"] != request["review_sha256"]:
        raise ValueError(
            "Review or feedback changed after the Agent publication request"
        )
    head = _read(review / "workspace-manifest.json")
    repository = getattr(container, "agent_structure_loop_repository", None)
    expected_structure_head = (
        repository.get_working_revision_head(
            project_id=meta["project_id"],
            document_id=meta["document_id"],
            source_sha256=meta["source_sha256"],
        )
        if repository is not None
        else None
    )
    _, previous_bundle, _, _ = _source_context(
        meta["project_id"], meta["document_id"], ()
    )
    previous = previous_bundle["baseline"]
    expected_graph = job.payload.get("expected_native_source_graph_object_sha256")
    current_graph = (
        (previous_bundle.get("native_publication") or {}).get("graph_pointer") or {}
    ).get("object_sha256")
    if expected_graph is not None and current_graph != expected_graph:
        raise ValueError("The live native source graph changed while Agent was working")
    if previous.artifact_sha256 != job.payload["baseline_sha256"]:
        raise ValueError(
            "The live application baseline changed while Agent was working"
        )
    heads, cursor, visited = [], head, set()
    while cursor["review_sha256"] not in visited:
        visited.add(cursor["review_sha256"])
        if (
            canonical_json_sha256(
                {k: v for k, v in cursor.items() if k != "review_sha256"}
            )
            != cursor["review_sha256"]
        ):
            raise ValueError("Review history hash changed")
        heads.append(cursor)
        predecessor = cursor.get("predecessor_review_sha256")
        if not predecessor:
            break
        cursor = _read(review / "reviews" / (predecessor + ".json"))
    chain, seen = [], set()
    for entry in reversed(heads):
        sha = entry["current"]["baseline_sha256"]
        if sha in seen:
            continue
        seen.add(sha)
        if sha == previous.artifact_sha256:
            chain = []
            continue
        chain.append(entry)
    if previous.artifact_sha256 not in seen:
        raise ValueError(
            "Review history does not contain the live application predecessor"
        )
    products = []
    final_result = None
    if not chain:
        from reqpilotx_server.application.agent_workflow.source_requirement_jobs import (
            SourceItemNativeLoopRunReceiptV1,
        )

        provenance = SourceItemNativeLoopRunReceiptV1.model_validate(
            {
                "source_sha256": previous.source_sha256,
                "document_map_artifact_id": previous.document_map_artifact_id,
                "document_map_artifact_sha256": previous.document_map_artifact_sha256,
                "predecessor_sha256": previous.artifact_sha256,
                "model_id": runtime_receipt["identity"]["model_id"],
                "runtime_thread_id": runtime_receipt["thread_id"],
                "runtime_turn_id": runtime_receipt["turn_id"],
                "review_sha256": head["review_sha256"],
                "operation": "NATIVE_LOOP_REVIEW",
                "request_sha256": job.idempotency_key,
                "candidate_sha256": previous.artifact_sha256,
                "runtime_receipt": runtime_receipt,
            }
        )
        context.begin_publication()
        final_result = {
            "schema_version": "source-item-loop-result/v1",
            "status": "REVIEW_COMPLETE",
            "source_sha256": previous.source_sha256,
            "document_map_artifact_id": previous.document_map_artifact_id,
            "document_map_artifact_sha256": previous.document_map_artifact_sha256,
            "baseline_sha256": previous.artifact_sha256,
            "expected_live_baseline_sha256": previous.artifact_sha256,
            "transport_kind": "CODEX_APP_SERVER_NATIVE_LOOP",
            "native_loop_provenance": provenance.model_dump(mode="json"),
            "is_human_accepted": False,
        }
        native_publication = previous_bundle.get("native_publication") or {}
        if (native_publication.get("graph_pointer") or {}).get("object_sha256"):
            final_result["expected_native_source_graph_object_sha256"] = (
                native_publication["graph_pointer"]["object_sha256"]
            )
        if previous_bundle.get("product_baseline_pointer"):
            final_result["baseline_revision_artifact"] = previous_bundle[
                "product_baseline_pointer"
            ]
        if repository is not None:
            final_result.update(
                expected_structure_head_verified=True,
                expected_structure_head_revision_sha256=expected_structure_head.revision_sha256
                if expected_structure_head
                else None,
            )
    for entry in chain:
        candidate = (review / entry["current"]["version_path"]).resolve(strict=True)
        if not candidate.is_relative_to(review.resolve()) or candidate.is_symlink():
            raise ValueError("Review version is outside the authorized workspace")
        for name, digest in entry["current"]["files"].items():
            path = (candidate / name).resolve(strict=True)
            if (
                not path.is_relative_to(candidate)
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest
            ):
                raise ValueError("Closed version artifact changed")
        baseline = SourceRequirementBaselineArtifactV1.model_validate(
            _read(candidate / "source-requirement-baseline.json")
        )
        document_map = DocumentMapArtifactV1.model_validate(
            _read(candidate / "document-map.json")
        )
        validation = SourceRequirementBaselineValidationReceiptV1.model_validate(
            _read(candidate / "validation-receipt.json")
        )
        annotations = (
            _read(candidate / "image-annotations.json")
            if (candidate / "image-annotations.json").exists()
            else None
        )
        proof = _read(candidate / "revision-receipt.json")
        final_result = publish_agent_source_item_revision(
            project_id=meta["project_id"],
            document_id=meta["document_id"],
            document_map=document_map,
            previous_bundle=previous_bundle,
            baseline=baseline,
            validation_receipt=validation,
            operation="NATIVE_LOOP_REVISION",
            affected_scope_ids=tuple(sorted(baseline.covered_scope_ids)),
            runtime_receipts=(),
            request_sha256=job.idempotency_key,
            candidate_sha256=baseline.artifact_sha256,
            domain_proof={
                "review_sha256": head["review_sha256"],
                "revision": proof,
                "portable_verification": verified,
            },
            context=context,
            container=container,
            image_annotations=annotations,
            expected_live_baseline_sha256=job.payload["baseline_sha256"],
            native_provenance={
                "model_id": runtime_receipt["identity"]["model_id"],
                "runtime_thread_id": runtime_receipt["thread_id"],
                "runtime_turn_id": runtime_receipt["turn_id"],
                "review_sha256": head["review_sha256"],
                "runtime_receipt": dict(runtime_receipt),
            },
        )
        pointer = final_result["baseline_revision_artifact"]
        products.append(pointer)
        product = _load_product_baseline_artifact(
            container.object_store,
            pointer,
            project_id=meta["project_id"],
            document_id=meta["document_id"],
        )
        previous_bundle = {
            **previous_bundle,
            "baseline": baseline,
            "receipt": validation,
            "product_run_receipts": product.product_run_receipts,
            "product_baseline_pointer": pointer,
        }
    final_dir = review / head["current"]["version_path"]
    for filename, key, stage in (
        (
            "source-item-decomposition.json",
            "decomposition_artifact",
            "native-loop-decomposition",
        ),
        (
            "source-item-assessments.json",
            "assessments_artifact",
            "native-loop-assessments",
        ),
        ("image-annotations.json", "annotations_artifact", "native-loop-annotations"),
    ):
        if (final_dir / filename).exists():
            final_result[key] = _write_private(
                container,
                project_id=meta["project_id"],
                document_id=meta["document_id"],
                namespace="working",
                stage=stage,
                value=_read(final_dir / filename),
            )
    if (final_dir / "analysis-packet.json").exists():
        final_result["analysis_input_artifact"] = _write_private(
            container,
            project_id=meta["project_id"],
            document_id=meta["document_id"],
            namespace="working",
            stage="native-loop-analysis-input",
            value={
                "packet": _read(final_dir / "analysis-packet.json"),
                "knowledge_catalog": _read(final_dir / "knowledge-catalog.json"),
            },
        )
    final_result.update(
        task_id=meta["task_id"],
        application_saved=True,
        runtime_receipt=dict(runtime_receipt),
        baseline_sha256=previous_bundle["baseline"].artifact_sha256,
        baseline_chain_artifacts=products,
        review_sha256=head["review_sha256"],
        delivery_receipt=_read(
            review / head["delivery"]["path"] / "delivery-receipt.json"
        ),
    )
    _carry_review_feedback(
        meta["project_id"],
        meta["document_id"],
        previous,
        previous_bundle["baseline"],
        (),
    )
    if manual_before is not None:
        attach_manual_publication_contexts(
            final_result,
            container=container,
            project_id=meta["project_id"],
            document_id=meta["document_id"],
            previous_context=manual_before,
            document_map=_read(final_dir / "document-map.json"),
            baseline=_read(final_dir / "source-requirement-baseline.json"),
            table_previews=_read(final_dir / "table-previews.json"),
            decomposition=_read(final_dir / "source-item-decomposition.json")
            if (final_dir / "source-item-decomposition.json").exists()
            else None,
            annotations=_read(final_dir / "image-annotations.json")
            if (final_dir / "image-annotations.json").exists()
            else None,
        )
    return final_result


def load_native_loop_artifact(container, project_id, document_id, baseline, key):
    candidates = [
        container.jobs.get_latest_succeeded(
            kind=kind,
            aggregate_type="sor-document",
            aggregate_id=document_id,
            require_result_key=key,
        )
        for kind in (LOOP_JOB_KIND, "agent.source-item.extract-loop")
    ]
    candidates = [
        job
        for job in candidates
        if job
        and job.payload.get("project_id") == project_id
        and job.result
        and (
            job.result.get("baseline_sha256")
            or (job.result.get("baseline_revision_artifact") or {}).get(
                "baseline_artifact_sha256"
            )
        )
        == baseline.artifact_sha256
        and job.result.get(key)
    ]
    if not candidates:
        return None, None
    job = max(candidates, key=lambda value: value.updated_at)
    value = _read_private(
        container,
        job.result[key],
        project_id=project_id,
        document_id=document_id,
        namespace="working",
    )
    if (
        value.get("baseline_sha256") != baseline.artifact_sha256
        or value.get("source_sha256") != baseline.source_sha256
    ):
        raise ValueError("Native-loop reasoning source baseline changed")
    return value, job.job_id
