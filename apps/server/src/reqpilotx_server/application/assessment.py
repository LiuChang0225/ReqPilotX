from __future__ import annotations

import logging
import re
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import BoundedSemaphore, Lock, RLock, Thread
from typing import Any, Callable

from reqpilotx_server import db
from reqpilotx_server.adapters.persistence.assessment_results import (
    PER_NODE_RECOVERABLE_PUBLICATION_MODE,
    abandon_incremental_analysis_job,
    cancel_analysis_node_attempt,
    cancel_waiting_analysis_targets,
    claim_analysis_node_attempt,
    claim_analysis_job,
    fail_analysis_node_attempt,
    fail_analysis_node_publication,
    finalize_incremental_analysis_job,
    publish_staged_analysis_node_result,
    publish_serving_results,
    recover_analysis_job_targets,
    stage_analysis_node_result,
)
from reqpilotx_server.application.assessment_contracts import (
    ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V2,
    AnalyzeRequest,
    RequirementAnalysisScopeRequest,
    RunAIAnalysisRequest,
    StopAIAnalysisRequest,
)
from reqpilotx_server.application.assessment_scope import (
    ASSESSMENT_SCOPE_SCHEMA_VERSION_V1,
    ASSESSMENT_SCOPE_SCHEMA_VERSION_V2,
    ATOMIC_BATCH_PUBLICATION_MODE,
    AssessmentScopeError,
    assessment_command_sha256,
    assessment_scope_preview,
    build_assessment_scope,
    canonical_json,
    parse_assessment_scope,
    scope_target_pairs,
    select_rows_for_assessment_scope,
    sha256_text,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.transport import (
    FileArtifact,
)
from reqpilotx_server.domain.assessment.execution_policy import evaluate_assessment_execution
from reqpilotx_server.infrastructure.runtime_settings import DatabaseBackend
from reqpilotx_server.ports.job_queue import (
    DurableJob,
    JobExecutionContext,
    JobLeaseLostError,
    JobStatus,
)
from reqpilotx_server.services.ai_copilot import (
    AIAnalysisCancelledError,
    build_requirement_normalization_detail,
    ensure_ai_profile_runnable,
    load_ai_copilot_config,
    run_ai_copilot_assessment,
)
from reqpilotx_server.services.extraction_preview import build_extraction_preview
from reqpilotx_server.services.preview_cache import (
    begin_preview_generation,
    load_cached_preview,
    persist_preview_cache,
    purge_preview_artifacts,
)

logger = logging.getLogger(__name__)

_AI_ANALYSIS_THREAD_LOCK = Lock()

_AI_ANALYSIS_START_LOCK = RLock()

_AI_ANALYSIS_THREADS: dict[str, dict[str, Any]] = {}

_AI_REASONING_EVENT_PREVIEW_LIMIT = 3200

_AI_REASONING_EVENT_CHAR_DELTA = 180

_AI_REASONING_EVENT_FLUSH_INTERVAL_SECONDS = 0.9

_AI_REASONING_OUTPUT_PREVIEW_LIMIT = 2200

_AI_ANALYSIS_CAPABILITIES = {
    "schema_version": "ai-analysis-capabilities/v2",
    "analysis_scope_contract": "requirement-analysis-scope/v2",
    "scope_preview": True,
    "scoped_start": True,
    "atomic_batch_publication": True,
    "per_node_recoverable_publication": True,
    "node_execution_status": True,
    "failed_only_retry": True,
    "controlled_parallelism": True,
}

_AI_ANALYSIS_MAX_CONCURRENCY = 2
_AI_ANALYSIS_PROVIDER_CAPACITY = BoundedSemaphore(_AI_ANALYSIS_MAX_CONCURRENCY)


class _IncrementalAnalysisDeliverySuperseded(RuntimeError):
    """Stop a stale delivery without mutating work owned by its replacement."""


@contextmanager
def _analysis_provider_lease():
    """Keep assessment inside the existing process-wide remote-call ceiling.

    Assessment also has its own two-slot ceiling, so it cannot occupy every
    shared extraction slot.  The user never configures this implementation
    budget and the single-item provider contract remains unchanged.
    """

    from reqpilotx_server.application.extraction.llm_provision_batch import (
        llm_document_remote_chunk_lease,
    )

    with _AI_ANALYSIS_PROVIDER_CAPACITY:
        with llm_document_remote_chunk_lease():
            yield


def _register_ai_analysis_thread(
    document_id: str, thread: Thread, *, job_id: str
) -> None:
    with _AI_ANALYSIS_THREAD_LOCK:
        _AI_ANALYSIS_THREADS[document_id] = {
            "thread": thread,
            "job_id": str(job_id or "").strip(),
            "starting": True,
            "cancel_requested": False,
            "publishing": False,
        }


def _get_ai_analysis_thread(document_id: str) -> Thread | None:
    with _AI_ANALYSIS_THREAD_LOCK:
        runtime = _AI_ANALYSIS_THREADS.get(document_id)
        if not runtime:
            return None
        return runtime.get("thread")


def _is_ai_analysis_cancel_requested(document_id: str, job_id: str) -> bool:
    with _AI_ANALYSIS_THREAD_LOCK:
        runtime = _AI_ANALYSIS_THREADS.get(document_id) or {}
        context = runtime.get("durable_context")
        if (
            isinstance(context, JobExecutionContext)
            and str(runtime.get("job_id") or "").strip() == str(job_id or "").strip()
        ):
            return context.cancellation_requested()
        return str(runtime.get("job_id") or "").strip() == str(
            job_id or ""
        ).strip() and bool(runtime.get("cancel_requested"))


def _request_ai_analysis_cancel(document_id: str, job_id: str) -> bool:
    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, _durable_jobs

    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        job = db.get_ai_analysis_job(job_id) or {}
        runtime_job_id = str(job.get("runtime_job_id") or "").strip()
        if not runtime_job_id:
            return False
        durable = _durable_jobs().request_cancel(runtime_job_id)
        return durable is not None and durable.cancel_requested
    with _AI_ANALYSIS_THREAD_LOCK:
        runtime = _AI_ANALYSIS_THREADS.get(document_id)
        if not runtime:
            return False
        if str(runtime.get("job_id") or "").strip() != str(job_id or "").strip():
            return False
        if bool(runtime.get("publishing")):
            return False
        runtime["cancel_requested"] = True
        return True


def _is_ai_analysis_publishing(document_id: str, job_id: str) -> bool:
    with _AI_ANALYSIS_THREAD_LOCK:
        runtime = _AI_ANALYSIS_THREADS.get(document_id) or {}
        return str(runtime.get("job_id") or "").strip() == str(
            job_id or ""
        ).strip() and bool(runtime.get("publishing"))


def _begin_ai_analysis_publish(document_id: str, job_id: str) -> bool:
    with _AI_ANALYSIS_THREAD_LOCK:
        runtime = _AI_ANALYSIS_THREADS.get(document_id)
        if not runtime:
            return False
        if str(runtime.get("job_id") or "").strip() != str(job_id or "").strip():
            return False
        context = runtime.get("durable_context")
        if isinstance(context, JobExecutionContext):
            if context.cancellation_requested():
                return False
            context.begin_publication()
        if bool(runtime.get("cancel_requested")):
            return False
        runtime["publishing"] = True
        return True


def _clear_ai_analysis_thread(
    document_id: str,
    *,
    thread: Thread | None = None,
    job_id: str | None = None,
    durable_context: JobExecutionContext | None = None,
) -> None:
    with _AI_ANALYSIS_THREAD_LOCK:
        current = _AI_ANALYSIS_THREADS.get(document_id)
        if current is None:
            return
        current_thread = current.get("thread")
        if thread is not None and current_thread is not thread:
            return
        if (
            job_id is not None
            and str(current.get("job_id") or "").strip() != str(job_id).strip()
        ):
            return
        if (
            durable_context is not None
            and current.get("durable_context") is not durable_context
        ):
            return
        _AI_ANALYSIS_THREADS.pop(document_id, None)


def _has_live_ai_analysis_thread(document_id: str) -> bool:
    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, _durable_jobs

    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        job = db.get_running_ai_analysis_job(document_id) or {}
        runtime_job_id = str(job.get("runtime_job_id") or "").strip()
        durable = _durable_jobs().get(runtime_job_id) if runtime_job_id else None
        return bool(
            durable
            and durable.status
            in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.CANCEL_REQUESTED,
            }
        )
    with _AI_ANALYSIS_THREAD_LOCK:
        runtime = _AI_ANALYSIS_THREADS.get(document_id) or {}
        thread = runtime.get("thread")
        return bool(runtime.get("starting") or (thread and thread.is_alive()))


def _mark_ai_analysis_thread_started(document_id: str, job_id: str) -> None:
    with _AI_ANALYSIS_THREAD_LOCK:
        runtime = _AI_ANALYSIS_THREADS.get(document_id)
        if (
            runtime
            and str(runtime.get("job_id") or "").strip() == str(job_id or "").strip()
        ):
            runtime["starting"] = False


def _raise_if_ai_analysis_cancel_requested(document_id: str, job_id: str) -> None:
    if _is_ai_analysis_cancel_requested(document_id, job_id):
        raise AIAnalysisCancelledError("AI analysis cancelled by user.")


def _finalize_orphaned_ai_analysis_job(document_id: str) -> dict[str, Any] | None:
    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, _durable_jobs

    with _AI_ANALYSIS_START_LOCK:
        job = db.get_running_ai_analysis_job(document_id)
        if not job:
            return None
        if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
            runtime_job_id = str(job.get("runtime_job_id") or "").strip()
            durable = _durable_jobs().get(runtime_job_id) if runtime_job_id else None
            if durable is not None and durable.status in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.CANCEL_REQUESTED,
            }:
                return job
            timestamp = datetime.now(timezone.utc).isoformat()
            cancelled = (
                bool(durable and durable.status is JobStatus.CANCELLED)
                or str(job.get("status") or "").strip().upper() == "CANCEL_REQUESTED"
            )
            if (
                str(job.get("publication_mode") or "").strip().upper()
                == PER_NODE_RECOVERABLE_PUBLICATION_MODE
            ):
                if not cancelled:
                    _resume_staged_analysis_publications(
                        str(job.get("id") or "")
                    )
                    try:
                        return finalize_incremental_analysis_job(
                            str(job.get("id") or "")
                        )
                    except RuntimeError as exc:
                        if str(exc) != "analysis_job_targets_not_terminal":
                            raise
                return abandon_incremental_analysis_job(
                    str(job.get("id") or ""),
                    cancelled=cancelled,
                )
            durable_error = str(
                (durable.error_message if durable else "") or ""
            ).strip()
            if durable is not None and durable.status is JobStatus.SUCCEEDED:
                durable_error = "Durable AI worker completed without publishing its business result."
            updated = db.update_ai_analysis_job(
                str(job.get("id") or ""),
                status="CANCELLED" if cancelled else "FAILED",
                current_stage="cancelled" if cancelled else "failed",
                last_message=(
                    "AI analysis was cancelled."
                    if cancelled
                    else "Durable AI worker stopped before publication."
                ),
                error_message=(
                    ""
                    if cancelled
                    else durable_error or "Durable AI worker is no longer active."
                ),
                finished_at=timestamp,
            )
            _try_restore_document_ai_projection_after_unpublished_job(
                str(job.get("id") or "")
            )
            return updated or job
        if _has_live_ai_analysis_thread(document_id):
            return job
        timestamp = datetime.now(timezone.utc).isoformat()
        status = str(job.get("status") or "").strip().upper()
        cancelled = status == "CANCEL_REQUESTED"
        if (
            str(job.get("publication_mode") or "").strip().upper()
            == PER_NODE_RECOVERABLE_PUBLICATION_MODE
        ):
            updated = abandon_incremental_analysis_job(
                str(job.get("id") or ""),
                cancelled=cancelled,
            )
            logger.warning(
                "Finalized orphaned incremental AI analysis job %s for document %s.",
                str(job.get("id") or ""),
                document_id,
            )
            return updated or job
        updated = db.update_ai_analysis_job(
            str(job.get("id") or ""),
            status="CANCELLED" if cancelled else "FAILED",
            current_stage="cancelled" if cancelled else "failed",
            last_message="AI analysis was stopped."
            if cancelled
            else "AI analysis worker stopped unexpectedly.",
            error_message="" if cancelled else "AI analysis worker is not active.",
            finished_at=timestamp,
            current_requirement_id="",
            current_requirement_code="",
        )
        db.add_ai_analysis_event(
            job_id=str(job.get("id") or ""),
            sor_document_id=document_id,
            profile_id=str(job.get("profile_id") or ""),
            provider=str(job.get("provider") or ""),
            event_type="job_cancelled" if cancelled else "job_failed",
            stage="cancelled" if cancelled else "failed",
            message="AI 分析已停止。"
            if cancelled
            else "AI analysis worker is not active. Please retry.",
            payload={"reason": "cancelled_by_user" if cancelled else "orphaned_worker"},
        )
        _try_restore_document_ai_projection_after_unpublished_job(
            str(job.get("id") or "")
        )
        logger.warning(
            "Finalized orphaned AI analysis job %s for document %s.",
            str(job.get("id") or ""),
            document_id,
        )
        return updated or job


def run_document_ai_analysis(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> dict:
    project, document, _requirements, eligible_rows, knowledge_docs, ai_config = (
        _prepare_document_ai_analysis_request(
            project_id,
            document_id,
            requirement_set_id=requirement_set_id,
        )
    )
    return _execute_document_ai_analysis(
        project=project,
        document=document,
        eligible_rows=eligible_rows,
        knowledge_docs=knowledge_docs,
        ai_config=ai_config,
    )


def analyze_document(project_id: str, payload: AnalyzeRequest) -> dict:
    from reqpilotx_server.application.knowledge import _list_project_knowledge_documents
    from reqpilotx_server.application.projects import _require_project
    from reqpilotx_server.application.requirements import (
        _build_analysis_rows_from_preview,
        _persist_requirements,
    )
    from reqpilotx_server.application.runtime import _runtime_record_path

    project = _require_project(project_id)
    document = db.get_sor_document(payload.document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )

    preview_generation_id, document = begin_preview_generation(document["id"])
    try:
        preview = build_extraction_preview(
            str(_runtime_record_path(document)),
            document_id=document["id"],
            snapshot_root=purge_preview_artifacts(document["id"]),
        )
        preview["preview_generation_id"] = preview_generation_id
        extracted = _build_analysis_rows_from_preview(preview)
        if not extracted:
            raise ApplicationError(
                status_code=422,
                detail="No atomic requirements extracted from document.",
            )

        inserted = _persist_requirements(
            project_id=project_id,
            sor_document_id=payload.document_id,
            rows=extracted,
            knowledge_docs=[],
            dedupe_version=str(preview.get("summary", {}).get("dedupe_version") or ""),
            workflow_stage="EXTRACTED",
        )
        document_state = db.get_sor_document(payload.document_id) or document
        document_state = (
            persist_preview_cache(document_state, preview) or document_state
        )
        knowledge_docs = _list_project_knowledge_documents(project_id)
    except Exception as exc:
        db.update_sor_document_preview_state(
            document["id"],
            preview_status="FAILED",
            preview_error=str(exc)[:500],
            preview_generation_id=preview_generation_id,
        )
        raise

    return {
        "project_id": project_id,
        "document_id": payload.document_id,
        "requirements_generated": inserted,
        "knowledge_docs_used": 0,
        "knowledge_docs_available": len(knowledge_docs),
        "knowledge_base_version": int(project.get("knowledge_base_version") or 0),
        "workflow_stage": str(document_state.get("workflow_stage") or "EXTRACTED"),
        "requirement_set_version": int(
            document_state.get("requirement_set_version") or 0
        ),
        "ai_status": str(document_state.get("ai_status") or "NOT_RUN"),
        "ai_triggered": False,
        "dedupe_version": str(preview.get("summary", {}).get("dedupe_version") or ""),
        "dedup_exact_count": int(
            preview.get("summary", {}).get("dedup_exact_count") or 0
        ),
        "dedup_prefix_count": int(
            preview.get("summary", {}).get("dedup_prefix_count") or 0
        ),
        "hybrid_entries_count": int(
            preview.get("summary", {}).get("hybrid_entries_count") or 0
        ),
    }


def preview_document_ai_analysis_scope(
    project_id: str,
    document_id: str,
    payload: RequirementAnalysisScopeRequest,
) -> dict[str, Any]:
    """Resolve an analysis scope without starting AI or mutating runtime state."""

    _project, _document, _requirement_set, scope_envelope = (
        _resolve_document_ai_analysis_scope(
            project_id,
            document_id,
            requirement_set_id=payload.requirement_set_id,
            expected_tree_sha256=payload.expected_tree_sha256,
            scope=payload.scope,
            node_ids=payload.node_ids,
            scope_schema_version=(
                ASSESSMENT_SCOPE_SCHEMA_VERSION_V2
                if payload.schema_version == ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V2
                else ASSESSMENT_SCOPE_SCHEMA_VERSION_V1
            ),
            source_job_id=str(getattr(payload, "source_job_id", "") or ""),
        )
    )
    return assessment_scope_preview(
        scope_envelope,
        command_id=payload.command_id,
    )


def _resolve_document_ai_analysis_scope(
    project_id: str,
    document_id: str,
    *,
    requirement_set_id: str,
    expected_tree_sha256: str,
    scope: str,
    node_ids: tuple[str, ...],
    scope_schema_version: str = ASSESSMENT_SCOPE_SCHEMA_VERSION_V1,
    source_job_id: str = "",
    retry_exclude_job_id: str = "",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Read and canonicalize the locked Matrix analysis scope.

    This path must stay free of the profile/knowledge preparation performed by
    ``_prepare_document_ai_analysis_request`` so preview remains observational.
    """

    from reqpilotx_server.application.projects import _require_project
    from reqpilotx_server.application.requirement_matrix import build_requirement_matrix_snapshot

    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or str(document.get("project_id") or "") != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )
    requested_set_id = str(requirement_set_id or "").strip()
    requirement_set = db.get_requirement_set(requested_set_id)
    if (
        not requirement_set
        or str(requirement_set.get("project_id") or "") != project_id
        or str(requirement_set.get("sor_document_id") or "") != document_id
    ):
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "analysis_requirement_set_not_found",
                "message": "该需求版本不属于当前项目和文档。",
            },
        )
    active_set_id = str(document.get("active_requirement_set_id") or "").strip()
    set_version = max(int(requirement_set.get("version") or 0), 0)
    document_version = max(int(document.get("requirement_set_version") or 0), 0)
    if (
        requested_set_id != active_set_id
        or not bool(int(requirement_set.get("is_active") or 0))
        or set_version != document_version
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "analysis_requirement_set_not_current",
                "message": "当前分析范围不是正式生效的需求版本，请刷新后重试。",
                "requested_requirement_set_id": requested_set_id,
                "active_requirement_set_id": active_set_id,
            },
        )
    if (
        str(requirement_set.get("status") or "").strip().upper() != "LOCKED"
        or not str(document.get("extraction_locked_at") or "").strip()
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "analysis_requirement_set_not_locked",
                "message": "请先锁定当前需求版本，再进入需求分析。",
            },
        )
    persisted_tree_sha256 = str(
        requirement_set.get("requirement_tree_sha256") or ""
    ).strip()
    matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requested_set_id,
        require_current_runtime=True,
    )
    actual_tree_sha256 = str(matrix.tree_sha256 or "").strip()
    if persisted_tree_sha256 and actual_tree_sha256 != persisted_tree_sha256:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "analysis_tree_identity_mismatch",
                "message": "需求矩阵与冻结结构树身份不一致，请重新提取。",
            },
        )
    expected_tree = str(expected_tree_sha256 or "").strip()
    if expected_tree and expected_tree != actual_tree_sha256:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "analysis_tree_changed",
                "message": "需求结构树已变化，请刷新分析范围后重试。",
                "expected_tree_sha256": expected_tree,
                "actual_tree_sha256": actual_tree_sha256,
            },
        )
    retry_node_ids: tuple[str, ...] = ()
    retry_metadata: dict[str, Any] | None = None
    if str(scope or "").strip().upper() == "FAILED_ONLY":
        retry_node_ids, retry_metadata = _resolve_failed_analysis_retry_targets(
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requested_set_id,
            requirement_set_version=set_version,
            requirement_tree_sha256=actual_tree_sha256,
            knowledge_base_version=max(
                int(project.get("knowledge_base_version") or 0), 0
            ),
            matrix=matrix,
            source_job_id=source_job_id,
            exclude_job_id=retry_exclude_job_id,
        )
    try:
        scope_envelope = build_assessment_scope(
            matrix,
            requirement_set_version=set_version,
            scope=scope,
            requested_node_ids=node_ids,
            schema_version=scope_schema_version,
            source_job_id=source_job_id,
            retry_node_ids=retry_node_ids,
            retry_metadata=retry_metadata,
        )
    except AssessmentScopeError as exc:
        status_code = (
            409
            if exc.code
            in {
                "matrix_requires_reextraction",
                "analysis_scope_node_not_in_requirement_set",
            }
            else 422
        )
        raise ApplicationError(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return project, document, requirement_set, scope_envelope


def _resolve_failed_analysis_retry_targets(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    requirement_set_version: int,
    requirement_tree_sha256: str,
    knowledge_base_version: int,
    matrix: Any,
    source_job_id: str,
    exclude_job_id: str = "",
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Resolve retryable failures without trusting a browser-supplied node list."""

    normalized_source_job_id = str(source_job_id or "").strip()
    source_job = db.get_ai_analysis_job(normalized_source_job_id) or {}
    if (
        not source_job
        or str(source_job.get("project_id") or "") != project_id
        or str(source_job.get("sor_document_id") or "") != document_id
        or str(source_job.get("requirement_set_id") or "") != requirement_set_id
    ):
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "analysis_retry_source_not_found",
                "message": "未找到属于当前需求版本的失败分析任务。",
            },
        )
    if str(source_job.get("status") or "").strip().upper() not in {
        "FAILED",
        "COMPLETED",
        "CANCELLED",
    }:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "analysis_retry_source_not_terminal",
                "message": "当前分析任务尚未结束，不能创建失败项重试。",
            },
        )
    if (
        int(source_job.get("requirement_set_version") or 0)
        != max(int(requirement_set_version or 0), 0)
        or str(source_job.get("requirement_tree_sha256") or "")
        != str(requirement_tree_sha256 or "")
        or int(source_job.get("knowledge_base_version") or 0)
        != max(int(knowledge_base_version or 0), 0)
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "analysis_retry_source_stale",
                "message": "失败任务的结构或知识库版本已变化，请重新选择分析范围。",
            },
        )
    source_targets = db.list_ai_analysis_job_targets(normalized_source_job_id)
    failed_targets = [
        item
        for item in source_targets
        if str(item.get("status") or "").strip().upper() == "FAILED"
        and bool(int(item.get("retryable") or 0))
    ]
    if not failed_targets:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "analysis_retry_no_failures",
                "message": "该任务没有仍可重试的失败项。",
            },
        )
    failure_snapshot = {
        "schema_version": "analysis-failed-target-snapshot/v1",
        "source_job_id": normalized_source_job_id,
        "targets": [
            {
                "target_id": str(item.get("id") or ""),
                "node_id": str(item.get("node_id") or ""),
                "requirement_id": str(item.get("requirement_id") or ""),
                "latest_attempt_id": str(item.get("latest_attempt_id") or ""),
                "status": "FAILED",
                "retryable": True,
            }
            for item in failed_targets
        ],
    }
    failed_snapshot_sha256 = sha256_text(canonical_json(failure_snapshot))
    universe_pairs = {
        (str(node.node_id or ""), str(node.requirement_id or ""))
        for node in matrix.nodes
        if str(
            node.matrix_visibility.value
            if hasattr(node.matrix_visibility, "value")
            else node.matrix_visibility
        )
        == "VISIBLE"
        and str(
            node.disposition.value
            if hasattr(node.disposition, "value")
            else node.disposition
        )
        == "ANALYZE"
        and str(
            node.analysis_eligibility.value
            if hasattr(node.analysis_eligibility, "value")
            else node.analysis_eligibility
        )
        == "ELIGIBLE"
        and str(node.requirement_id or "").strip()
    }
    retry_ids: list[str] = []
    already_current = 0
    skipped = 0
    with db.get_conn() as conn:
        for item in failed_targets:
            pair = (
                str(item.get("node_id") or ""),
                str(item.get("requirement_id") or ""),
            )
            if pair not in universe_pairs:
                skipped += 1
                continue
            repaired = conn.execute(
                """
                SELECT 1
                FROM ai_analysis_job_targets t
                INNER JOIN ai_analysis_jobs j ON j.id = t.job_id
                WHERE t.requirement_id = ? AND t.node_id = ?
                  AND t.status = 'SUCCEEDED' AND t.published_attempt_id <> ''
                  AND j.sor_document_id = ? AND j.requirement_set_id = ?
                  AND j.requirement_set_version = ?
                  AND j.requirement_tree_sha256 = ?
                  AND j.knowledge_base_version = ?
                  AND j.created_at > ? AND j.id <> ?
                LIMIT 1
                """,
                (
                    pair[1],
                    pair[0],
                    document_id,
                    requirement_set_id,
                    int(source_job.get("requirement_set_version") or 0),
                    requirement_tree_sha256,
                    max(int(knowledge_base_version or 0), 0),
                    str(source_job.get("created_at") or ""),
                    str(exclude_job_id or "").strip(),
                ),
            ).fetchone()
            if repaired is not None:
                already_current += 1
                continue
            retry_ids.append(pair[0])
    if not retry_ids:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "analysis_retry_already_resolved",
                "message": "这些失败项已经由后续分析修复，无需重复运行。",
            },
        )
    return tuple(retry_ids), {
        "failed_snapshot_sha256": failed_snapshot_sha256,
        "source_failed_count": len(failed_targets),
        "already_current": already_current,
        "skipped": skipped,
    }


def _prepare_document_ai_analysis_request(
    project_id: str,
    document_id: str,
    profile_id: str | None = None,
    *,
    requirement_set_id: str | None = None,
    hydrate_knowledge_payloads: bool = True,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    Any,
]:
    from reqpilotx_server.application.knowledge import (
        _attach_knowledge_payloads_for_profile,
        _ensure_profile_knowledge_syncs,
        _list_project_knowledge_documents,
    )
    from reqpilotx_server.application.projects import _require_project
    from reqpilotx_server.application.requirement_matrix import (
        build_requirement_matrix_snapshot,
        select_matrix_analysis_requirements,
    )
    from reqpilotx_server.application.requirements import (
        _enrich_requirements_with_preview_context,
        _prepare_requirements_for_display,
    )
    from reqpilotx_server.application.runtime import _runtime_record_path

    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )
    document = db.backfill_sor_document_runtime(document_id) or document
    requested_requirement_set_id = str(requirement_set_id or "").strip()
    active_requirement_set_id = str(
        document.get("active_requirement_set_id") or ""
    ).strip()
    if not requested_requirement_set_id:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "requirement_set_id_required",
                "message": "启动需求分析前必须明确选择一个锁定版本。",
            },
        )
    selected_requirement_set = db.get_requirement_set(requested_requirement_set_id)
    if (
        not selected_requirement_set
        or str(selected_requirement_set.get("project_id") or "") != project_id
        or str(selected_requirement_set.get("sor_document_id") or "") != document_id
    ):
        raise ApplicationError(
            status_code=404,
            detail="Requirement set not found for this SOR document.",
        )
    if requested_requirement_set_id != active_requirement_set_id:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "analysis_requirement_set_not_current",
                "message": (
                    "当前分析页面选择的需求版本尚未设为正式当前版本。"
                    "请先返回需求确认并固定该版本。"
                ),
                "requested_requirement_set_id": requested_requirement_set_id,
                "active_requirement_set_id": active_requirement_set_id,
            },
        )
    selected_requirement_set_id = requested_requirement_set_id
    if not str(document.get("extraction_locked_at") or "").strip():
        raise ApplicationError(
            status_code=409,
            detail="Lock the reviewed requirement set before running AI analysis.",
        )
    if not selected_requirement_set_id:
        raise ApplicationError(
            status_code=409,
            detail="No locked Requirement Set is selected for analysis.",
        )
    selected_requirement_set = db.get_requirement_set(selected_requirement_set_id)
    if (
        not selected_requirement_set
        or str(selected_requirement_set.get("status") or "").upper() != "LOCKED"
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "analysis_requirement_set_not_locked",
                "message": "请先锁定当前需求版本，再进入需求分析。",
                "requirement_set_id": selected_requirement_set_id,
            },
        )

    requirements = db.list_requirements(
        project_id,
        document_id,
        requirement_set_id=selected_requirement_set_id,
    )
    # Freeze the version-bound Matrix before preview enrichment touches any
    # display convenience fields.  The immutable tree, not a refreshed cache
    # projection, owns the text and analysis target for this run.
    matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        selected_requirement_set_id,
        require_current_runtime=True,
    )
    requirements = _enrich_requirements_with_preview_context(
        requirements, str(_runtime_record_path(document))
    )
    preview, _ = load_cached_preview(document)
    presented_requirements, _order_diagnostics = _prepare_requirements_for_display(
        requirements, preview=preview
    )
    presented_by_id = {
        str(row.get("id") or "").strip(): row
        for row in presented_requirements
        if str(row.get("id") or "").strip()
    }
    requirements = [
        {
            **row,
            **(
                {
                    "display_req_code": str(displayed.get("display_req_code") or ""),
                    "display_sort_index": int(displayed.get("display_sort_index") or 0),
                }
                if (displayed := presented_by_id.get(str(row.get("id") or "").strip()))
                else {}
            ),
        }
        for row in requirements
    ]
    eligible_rows = select_matrix_analysis_requirements(matrix, requirements)
    if not eligible_rows:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "matrix_has_no_analysis_targets",
                "message": "当前确认清单中没有标记为“需分析”的可判定条目。",
            },
        )
    ai_config = load_ai_copilot_config(profile_id)
    ensure_ai_profile_runnable(ai_config)
    knowledge_docs = _ensure_profile_knowledge_syncs(
        project_id,
        _list_project_knowledge_documents(project_id),
        ai_config,
    )
    if hydrate_knowledge_payloads:
        knowledge_docs = _attach_knowledge_payloads_for_profile(
            knowledge_docs,
            profile_id=ai_config.profile_id,
        )
    return project, document, requirements, eligible_rows, knowledge_docs, ai_config


def _serialize_ai_analysis_document(
    document: dict[str, Any] | None,
    *,
    knowledge_base_version: int | None = None,
) -> dict[str, Any]:
    current = document or {}
    ai_status = str(current.get("ai_status") or "NOT_RUN").strip().upper() or "NOT_RUN"
    requirement_set_version = int(current.get("requirement_set_version") or 0)
    ai_requirement_set_version = int(current.get("ai_requirement_set_version") or 0)
    ai_knowledge_base_version = int(current.get("ai_knowledge_base_version") or 0)
    if ai_status == "READY" and (
        ai_requirement_set_version != requirement_set_version
        or (
            knowledge_base_version is not None
            and ai_knowledge_base_version != max(int(knowledge_base_version or 0), 0)
        )
    ):
        ai_status = "STALE"
    return {
        "id": current.get("id"),
        "workflow_stage": str(current.get("workflow_stage") or "UPLOADED"),
        "ai_status": ai_status,
        "ai_assessed_at": str(current.get("ai_assessed_at") or ""),
        "requirement_set_version": requirement_set_version,
        "ai_requirement_set_version": ai_requirement_set_version,
        "ai_knowledge_base_version": ai_knowledge_base_version,
    }


def _serialize_assessment_run_for_ui(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run.get("id"),
        "profile_id": str(run.get("profile_id") or ""),
        "provider": str(run.get("provider") or ""),
        "mode": str(run.get("mode") or ""),
        "retrieval_backend": str(run.get("retrieval_backend") or ""),
        "model_name": str(run.get("model_name") or ""),
        "prompt_version": str(run.get("prompt_version") or ""),
        "validation_status": str(run.get("validation_status") or ""),
        "fallback_reason": str(run.get("fallback_reason") or ""),
        "created_at": str(run.get("created_at") or ""),
    }


def _restore_document_ai_projection_after_unpublished_job(
    job_id: str,
) -> dict[str, Any]:
    """Restore a last-good READY projection after a non-publishing outcome.

    Claiming a job temporarily projects RUNNING on the document but does not
    erase its former version provenance.  A failed/cancelled scoped attempt
    must not turn still-current, fully covered serving results into STALE.
    """

    job = db.get_ai_analysis_job(job_id) or {}
    document_id = str(job.get("sor_document_id") or "").strip()
    project_id = str(job.get("project_id") or "").strip()
    requirement_set_id = str(job.get("requirement_set_id") or "").strip()
    document = db.get_sor_document(document_id) or {}
    if not job or not document:
        return {}
    if str(job.get("scope_sha256") or "").strip():
        # W6-A scoped jobs never replace the document's last-good aggregate
        # while running.  An unpublished outcome therefore has nothing to
        # restore and, importantly, must not race a newer KB/set projection.
        return document
    set_version = max(int(job.get("requirement_set_version") or 0), 0)
    if not set_version:
        set_version = max(int(document.get("requirement_set_version") or 0), 0)
    knowledge_version = max(int(job.get("knowledge_base_version") or 0), 0)
    current_project = db.get_project(project_id) or {}
    prior_projection_current = bool(
        str(document.get("ai_assessed_at") or "").strip()
        and int(document.get("ai_requirement_set_version") or 0) == set_version
        and int(document.get("ai_knowledge_base_version") or 0) == knowledge_version
        and int(current_project.get("knowledge_base_version") or 0) == knowledge_version
    )
    fully_covered = False
    if prior_projection_current:
        try:
            _project, _document, _requirement_set, all_scope = (
                _resolve_document_ai_analysis_scope(
                    project_id,
                    document_id,
                    requirement_set_id=requirement_set_id,
                    expected_tree_sha256=str(job.get("requirement_tree_sha256") or ""),
                    scope="ALL",
                    node_ids=(),
                )
            )
            target_requirement_ids = [
                requirement_id
                for _node_id, requirement_id in scope_target_pairs(all_scope)
            ]
            fully_covered = bool(target_requirement_ids) and all(
                str(
                    (db.get_assessment(requirement_id) or {}).get("reference_status")
                    or ""
                )
                .strip()
                .upper()
                == "CURRENT"
                for requirement_id in target_requirement_ids
            )
        except (ApplicationError, AssessmentScopeError, ValueError):
            fully_covered = False
    if prior_projection_current and fully_covered:
        return db.update_sor_document_ai_state_for_job(
            job_id,
            ai_status="READY",
            ai_assessed_at=str(document.get("ai_assessed_at") or ""),
            ai_requirement_set_version=set_version,
            ai_knowledge_base_version=knowledge_version,
            workflow_stage="AI_READY",
            expected_requirement_set_version=set_version,
            expected_knowledge_base_version=knowledge_version,
        )
    return db.update_sor_document_ai_state_for_job(
        job_id,
        ai_status="STALE",
        workflow_stage="EXTRACTION_LOCKED",
    )


def _try_restore_document_ai_projection_after_unpublished_job(
    job_id: str,
) -> dict[str, Any]:
    """Restore serving projection without masking the job's terminal cause."""

    try:
        return _restore_document_ai_projection_after_unpublished_job(job_id)
    except Exception:
        logger.exception(
            "Could not restore the last serving projection after AI job %s.",
            job_id,
        )
        return {}


def _serialize_analysis_node_executions(
    job_id: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.*, a.attempt_no AS attempt_no,
                   a.status AS attempt_status,
                   a.started_at AS attempt_started_at,
                   a.finished_at AS attempt_finished_at
            FROM ai_analysis_job_targets t
            LEFT JOIN ai_analysis_node_attempts a ON a.id = t.latest_attempt_id
            WHERE t.job_id = ?
            ORDER BY t.source_order ASC
            """,
            (job_id,),
        ).fetchall()
    executions: list[dict[str, Any]] = []
    counts = {
        "total": 0,
        "waiting": 0,
        "running": 0,
        "publishing": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
        "retryable_failed": 0,
        "settled": 0,
    }
    for row in rows:
        status = str(row["status"] or "WAITING").strip().upper()
        status_key = status.lower()
        if status_key in counts:
            counts[status_key] += 1
        retryable = bool(int(row["retryable"] or 0)) and status == "FAILED"
        if retryable:
            counts["retryable_failed"] += 1
        executions.append(
            {
                "schema_version": "assessment-node-execution/v1",
                "execution_id": str(row["id"] or ""),
                "target_id": str(row["id"] or ""),
                "job_id": str(row["job_id"] or ""),
                "node_id": str(row["node_id"] or ""),
                "requirement_id": str(row["requirement_id"] or ""),
                "requirement_code": str(row["requirement_code"] or ""),
                "source_order": max(int(row["source_order"] or 0), 0),
                "attempt_id": str(row["latest_attempt_id"] or ""),
                "attempt_no": max(int(row["attempt_no"] or 0), 0),
                "status": status,
                "stage": status.lower(),
                "published": status == "SUCCEEDED"
                and bool(str(row["published_attempt_id"] or "")),
                "retryable": retryable,
                "error_code": str(row["error_code"] or ""),
                "error_message": str(row["error_message"] or ""),
                "started_at": str(row["attempt_started_at"] or ""),
                "finished_at": str(row["attempt_finished_at"] or ""),
            }
        )
    counts["total"] = len(executions)
    counts["settled"] = counts["succeeded"] + counts["failed"] + counts["cancelled"]
    return executions, counts


def _build_ai_analysis_status_payload(
    project_id: str,
    document_id: str,
    *,
    job: dict[str, Any] | None = None,
    event_limit: int = 40,
) -> dict[str, Any]:
    from reqpilotx_server.application.knowledge import (
        _attach_knowledge_syncs,
        _build_ai_profile_catalog,
        _list_project_knowledge_documents,
    )
    from reqpilotx_server.application.projects import _require_project

    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )
    latest_job = (
        job
        or _finalize_orphaned_ai_analysis_job(document_id)
        or db.get_latest_ai_analysis_job(document_id)
    )
    job_payload = dict(latest_job) if latest_job else None
    if job_payload:
        job_payload["has_live_worker"] = _has_live_ai_analysis_thread(document_id)
        job_payload["computed_requirements"] = max(
            int(job_payload.get("completed_requirements") or 0), 0
        )
        job_payload["published_requirements"] = max(
            int(job_payload.get("published_requirements") or 0), 0
        )
        job_payload["publication_mode"] = (
            str(job_payload.get("publication_mode") or "").strip().upper()
            or ATOMIC_BATCH_PUBLICATION_MODE
        )
        job_payload["scope"] = (
            str(job_payload.get("scope_kind") or "").strip().upper() or "ALL"
        )
        status = str(job_payload.get("status") or "").strip().upper()
        if job_payload["publication_mode"] == PER_NODE_RECOVERABLE_PUBLICATION_MODE:
            node_executions, execution_counts = _serialize_analysis_node_executions(
                str(job_payload.get("id") or "")
            )
            job_payload["node_executions"] = node_executions
            job_payload["execution_counts"] = execution_counts
            job_payload["computed_requirements"] = (
                execution_counts["publishing"] + execution_counts["succeeded"]
            )
            job_payload["published_requirements"] = execution_counts["succeeded"]
            job_payload["waiting_requirements"] = execution_counts["waiting"]
            job_payload["running_requirements"] = execution_counts["running"]
            job_payload["failed_requirements"] = execution_counts["failed"]
            job_payload["cancelled_requirements"] = execution_counts["cancelled"]
        job_payload["can_cancel"] = bool(
            status in {"PENDING", "RUNNING"}
            and (
                job_payload["publication_mode"] == PER_NODE_RECOVERABLE_PUBLICATION_MODE
                or str(job_payload.get("current_stage") or "").strip().lower()
                != "publishing"
            )
            and job_payload["has_live_worker"]
        )
        scope_payload_json = str(job_payload.get("scope_payload_json") or "").strip()
        scope_sha256 = str(job_payload.get("scope_sha256") or "").strip()
        if scope_payload_json and scope_sha256:
            try:
                frozen_scope = parse_assessment_scope(
                    scope_payload_json,
                    expected_scope_sha256=str(job_payload.get("scope_sha256") or ""),
                )
                job_payload["scope_preview"] = assessment_scope_preview(
                    frozen_scope,
                    command_id=str(job_payload.get("command_id") or ""),
                )
                job_payload["scope"] = str(frozen_scope.get("scope") or "ALL")
            except AssessmentScopeError:
                logger.exception(
                    "AI analysis job %s has an unreadable frozen scope.",
                    str(job_payload.get("id") or ""),
                )
                job_payload["scope_preview"] = None
        else:
            job_payload["scope_preview"] = None
    refreshed_document = db.backfill_sor_document_runtime(document_id) or document
    events = (
        db.list_ai_analysis_events(str(job_payload.get("id") or ""), limit=event_limit)
        if job_payload
        else []
    )
    if job_payload:
        terminal_event = next(
            (
                item
                for item in reversed(events)
                if str(item.get("event_type") or "")
                in {"job_completed", "job_failed", "job_cancelled"}
            ),
            None,
        )
        terminal_status = {
            "job_completed": "COMPLETED",
            "job_failed": "FAILED",
            "job_cancelled": "CANCELLED",
        }.get(str((terminal_event or {}).get("event_type") or ""))
        if terminal_status:
            job_payload["status"] = terminal_status
            job_payload["current_stage"] = terminal_status.lower()
        completion_event = next(
            (
                item
                for item in reversed(events)
                if str(item.get("event_type") or "") == "job_completed"
            ),
            None,
        )
        completion_payload = (completion_event or {}).get("payload") or {}
        job_payload["outcome"] = (
            str(
                completion_payload.get("outcome")
                or terminal_status
                or job_payload.get("status")
                or ""
            )
            .strip()
            .upper()
        )
        job_payload["warning_count"] = max(
            int(completion_payload.get("warning_count") or 0), 0
        )
        job_payload["warning_requirement_count"] = max(
            int(
                completion_payload.get("warning_requirement_count")
                or job_payload["warning_count"]
            ),
            0,
        )
        job_payload["serving_mode"] = (
            str(completion_payload.get("serving_mode") or "").strip().lower()
        )
    knowledge_docs = _attach_knowledge_syncs(
        _list_project_knowledge_documents(project_id)
    )
    return {
        "capabilities": dict(_AI_ANALYSIS_CAPABILITIES),
        "job": job_payload,
        "events": events,
        "document": _serialize_ai_analysis_document(
            refreshed_document,
            knowledge_base_version=int(project.get("knowledge_base_version") or 0),
        ),
        "ai_profiles": _build_ai_profile_catalog(knowledge_docs),
    }


class _AIReasoningEventEmitter:
    def __init__(
        self,
        *,
        job_id: str,
        document_id: str,
        requirement_id: str,
        requirement_code: str,
        profile_id: str,
        provider: str,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> None:
        self.job_id = job_id
        self.document_id = document_id
        self.requirement_id = requirement_id
        self.requirement_code = requirement_code
        self.profile_id = profile_id
        self.provider = provider
        self._cancel_checker = cancel_checker
        self._stream_buffers: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_stage_key = ""

    def handle(self, payload: dict[str, Any]) -> None:
        self._raise_if_cancelled()
        event_type = str(payload.get("type") or "").strip().lower()
        stage = self._normalize_stage(payload.get("stage"))
        if event_type == "stage":
            self._emit_stage_update(
                stage=stage,
                mode=str(payload.get("mode") or "").strip(),
                model_name=str(payload.get("model_name") or "").strip(),
                message=str(payload.get("message") or "").strip(),
            )
            return
        if event_type not in {"reasoning", "answer"}:
            return
        delta = str(payload.get("delta") or "")
        if not delta:
            return
        stream_kind = "reasoning" if event_type == "reasoning" else "output"
        key = (stage, stream_kind)
        buffer = self._stream_buffers.setdefault(
            key,
            {
                "content": "",
                "chars_emitted": 0,
                "sequence": 0,
                "last_emit_at": 0.0,
                "mode": "",
                "model_name": "",
                "answer_started_emitted": False,
            },
        )
        buffer["content"] = str(buffer.get("content") or "") + delta
        if str(payload.get("mode") or "").strip():
            buffer["mode"] = str(payload.get("mode") or "").strip()
        if str(payload.get("model_name") or "").strip():
            buffer["model_name"] = str(payload.get("model_name") or "").strip()
        self._emit_stream_if_needed(
            stage=stage,
            stream_kind=stream_kind,
            buffer=buffer,
            force=int(buffer.get("sequence") or 0) == 0,
        )

    def flush(self) -> None:
        self._raise_if_cancelled()
        for (stage, stream_kind), buffer in self._stream_buffers.items():
            self._emit_stream_if_needed(
                stage=stage, stream_kind=stream_kind, buffer=buffer, force=True
            )

    def _raise_if_cancelled(self) -> None:
        if self._cancel_checker and self._cancel_checker():
            raise AIAnalysisCancelledError("AI analysis cancelled by user.")

    def _normalize_stage(self, value: object) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"screening", "retrieval", "assessment"}:
            return normalized
        if self._last_stage_key:
            return self._last_stage_key
        return "assessment"

    def _emit_stage_update(
        self, *, stage: str, mode: str, model_name: str, message: str
    ) -> None:
        final_message = message or self._default_stage_message(stage)
        if not final_message:
            return
        self._last_stage_key = stage
        db.update_ai_analysis_job(
            self.job_id,
            current_stage=stage,
            last_message=final_message,
        )
        db.add_ai_analysis_event(
            job_id=self.job_id,
            sor_document_id=self.document_id,
            profile_id=self.profile_id,
            provider=self.provider,
            event_type="requirement_stage_update",
            stage=stage,
            message=final_message,
            requirement_id=self.requirement_id,
            requirement_code=self.requirement_code,
            payload={
                "stage": stage,
                "mode": mode,
                "model_name": model_name,
                "provider": self.provider,
            },
        )

    def _emit_stream_if_needed(
        self,
        *,
        stage: str,
        stream_kind: str,
        buffer: dict[str, Any],
        force: bool,
    ) -> None:
        content = str(buffer.get("content") or "")
        total_chars = len(content)
        if total_chars <= 0:
            return
        now = time.monotonic()
        chars_since_emit = total_chars - int(buffer.get("chars_emitted") or 0)
        if not force and chars_since_emit < _AI_REASONING_EVENT_CHAR_DELTA:
            last_emit_at = float(buffer.get("last_emit_at") or 0.0)
            if (
                last_emit_at
                and (now - last_emit_at) < _AI_REASONING_EVENT_FLUSH_INTERVAL_SECONDS
            ):
                return
        buffer["chars_emitted"] = total_chars
        buffer["sequence"] = int(buffer.get("sequence") or 0) + 1
        buffer["last_emit_at"] = now
        self._last_stage_key = stage
        preview_limit = (
            _AI_REASONING_OUTPUT_PREVIEW_LIMIT
            if stream_kind == "output"
            else _AI_REASONING_EVENT_PREVIEW_LIMIT
        )
        preview = content[-preview_limit:]
        message = self._build_stream_message(
            stage=stage, stream_kind=stream_kind, total_chars=total_chars
        )
        payload = {
            "stage": stage,
            "phase": "reasoning" if stream_kind == "reasoning" else "output",
            "mode": str(buffer.get("mode") or ""),
            "model_name": str(buffer.get("model_name") or ""),
            "provider": self.provider,
            "characters_emitted": total_chars,
            "sequence": int(buffer.get("sequence") or 0),
            "content_preview": preview,
            "truncated": len(preview) < total_chars,
        }
        db.update_ai_analysis_job(
            self.job_id,
            current_stage=stage,
            last_message=message,
        )
        if (
            stage == "assessment"
            and stream_kind == "output"
            and not bool(buffer.get("answer_started_emitted"))
        ):
            buffer["answer_started_emitted"] = True
            db.add_ai_analysis_event(
                job_id=self.job_id,
                sor_document_id=self.document_id,
                profile_id=self.profile_id,
                provider=self.provider,
                event_type="requirement_answer_started",
                stage=stage,
                message=f"{self.requirement_code} 正在生成结构化结论。",
                requirement_id=self.requirement_id,
                requirement_code=self.requirement_code,
                payload=payload,
            )
        db.add_ai_analysis_event(
            job_id=self.job_id,
            sor_document_id=self.document_id,
            profile_id=self.profile_id,
            provider=self.provider,
            event_type=self._stream_event_type(stage, stream_kind),
            stage=stage,
            message=message,
            requirement_id=self.requirement_id,
            requirement_code=self.requirement_code,
            payload=payload,
        )

    def _stream_event_type(self, stage: str, stream_kind: str) -> str:
        if stage == "screening":
            return (
                "requirement_screening_reasoning"
                if stream_kind == "reasoning"
                else "requirement_screening_output"
            )
        return (
            "requirement_reasoning"
            if stream_kind == "reasoning"
            else "requirement_output"
        )

    def _default_stage_message(self, stage: str) -> str:
        if stage == "screening":
            return f"正在筛选 {self.requirement_code} 是否进入需求分析。"
        if stage == "retrieval":
            return f"{self.requirement_code} 筛选完成，正在检索候选证据。"
        if stage == "assessment":
            return f"{self.requirement_code} 候选证据已就绪，正在生成结构化结论。"
        return ""

    def _build_stream_message(
        self, *, stage: str, stream_kind: str, total_chars: int
    ) -> str:
        if stage == "screening":
            if stream_kind == "reasoning":
                return (
                    f"{self.requirement_code} 正在筛选推理（已生成 {total_chars} 字）"
                )
            return (
                f"{self.requirement_code} 正在生成筛选结果（已输出 {total_chars} 字）"
            )
        if stream_kind == "reasoning":
            return f"{self.requirement_code} 正在推理（已生成 {total_chars} 字）"
        return f"{self.requirement_code} 正在生成结构化结论（已输出 {total_chars} 字）"


_INCREMENTAL_DELIVERY_STALE_ERRORS = frozenset(
    {
        "analysis_job_not_running",
        "analysis_job_targets_not_terminal",
        "analysis_node_attempt_cancelled",
        "analysis_node_attempt_stale",
        "analysis_node_publication_stale",
        "analysis_target_not_waiting",
    }
)


def _is_incremental_delivery_superseded_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current, (_IncrementalAnalysisDeliverySuperseded, JobLeaseLostError)
        ):
            return True
        if str(current or "").strip() in _INCREMENTAL_DELIVERY_STALE_ERRORS:
            return True
        current = current.__cause__ or current.__context__
    return False


def _incremental_delivery_cancel_requested(
    document_id: str,
    job_id: str,
    delivery_context: JobExecutionContext | None,
) -> bool:
    if delivery_context is not None:
        return bool(delivery_context.cancellation_requested())
    return _is_ai_analysis_cancel_requested(document_id, job_id)


def _incremental_item_error_code(exc: BaseException, *, publication: bool) -> str:
    if isinstance(exc, AIAnalysisCancelledError):
        return "analysis_cancelled"
    text = str(exc or "").lower()
    if "incomplete result" in text or "returned" in text:
        return "analysis_result_unserviceable"
    if publication:
        if any(
            marker in text
            for marker in ("superseded", "no longer", "stale", "frozen scope")
        ):
            return "analysis_publication_lineage_changed"
        return "analysis_publication_failed"
    return "analysis_provider_failed"


def _incremental_item_public_message(
    requirement_code: str,
    *,
    error_code: str,
) -> str:
    if error_code == "analysis_publication_lineage_changed":
        return f"{requirement_code} 的输入版本已变化，本次结果未发布。"
    if error_code == "analysis_result_unserviceable":
        return f"{requirement_code} 未返回可采用的完整结论，可重试。"
    if error_code == "analysis_cancelled":
        return f"{requirement_code} 已停止，未发布新结果。"
    if error_code == "analysis_publication_failed":
        return f"{requirement_code} 已计算但发布失败，可重试。"
    return f"{requirement_code} 分析失败，可重试。"


def _execute_incremental_analysis_target(
    *,
    job_id: str,
    document_id: str,
    target: dict[str, Any],
    row: dict[str, Any],
    knowledge_docs: list[dict[str, Any]],
    ai_config: Any,
    delivery_context: JobExecutionContext | None = None,
) -> dict[str, Any]:
    from reqpilotx_server.application.requirement_set_impact_service import (
        REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION,
        requirement_analysis_input_sha256,
    )

    target_id = str(target.get("id") or "")
    requirement_id = str(row.get("id") or "").strip()
    requirement_code = str(
        row.get("display_req_code")
        or row.get("req_code")
        or target.get("requirement_code")
        or requirement_id
    ).strip()
    try:
        if _incremental_delivery_cancel_requested(
            document_id, job_id, delivery_context
        ):
            return {"status": "CANCELLED", "target_id": target_id}
    except Exception as exc:
        if _is_incremental_delivery_superseded_error(exc):
            raise _IncrementalAnalysisDeliverySuperseded() from exc
        raise
    try:
        claimed = claim_analysis_node_attempt(
            job_id=job_id,
            target_id=target_id,
            delivery_fence=(
                delivery_context.lease_fence()
                if delivery_context is not None
                else None
            ),
        )
    except Exception as exc:
        try:
            if _incremental_delivery_cancel_requested(
                document_id, job_id, delivery_context
            ):
                return {"status": "CANCELLED", "target_id": target_id}
        except Exception as control_exc:
            if _is_incremental_delivery_superseded_error(control_exc):
                raise _IncrementalAnalysisDeliverySuperseded() from control_exc
            raise
        if _is_incremental_delivery_superseded_error(exc):
            raise _IncrementalAnalysisDeliverySuperseded() from exc
        raise
    attempt_id = str(claimed.get("attempt_id") or "")
    fence_token = str(claimed.get("fence_token") or "")
    staged = False
    db.add_ai_analysis_event(
        job_id=job_id,
        sor_document_id=document_id,
        profile_id=ai_config.profile_id,
        provider=ai_config.provider,
        event_type="requirement_started",
        stage="screening",
        message=f"开始处理 {requirement_code}",
        requirement_id=requirement_id,
        requirement_code=requirement_code,
        payload={
            "source_order": max(int(target.get("source_order") or 0), 0),
            "attempt_id": attempt_id,
        },
    )
    reasoning_emitter = _AIReasoningEventEmitter(
        job_id=job_id,
        document_id=document_id,
        requirement_id=requirement_id,
        requirement_code=requirement_code,
        profile_id=ai_config.profile_id,
        provider=ai_config.provider,
        cancel_checker=lambda: _incremental_delivery_cancel_requested(
            document_id, job_id, delivery_context
        ),
    )
    try:
        if _incremental_delivery_cancel_requested(
            document_id, job_id, delivery_context
        ):
            raise AIAnalysisCancelledError("AI analysis cancelled by user.")
        display_text = str(
            row.get("display_text") or row.get("requirement_text") or ""
        ).strip()
        if not display_text:
            raise RuntimeError("analysis input has no analysable requirement text")
        try:
            with _analysis_provider_lease():
                result = run_ai_copilot_assessment(
                    row,
                    knowledge_docs,
                    config=ai_config,
                    progress_callback=reasoning_emitter.handle,
                )
        finally:
            if not _incremental_delivery_cancel_requested(
                document_id, job_id, delivery_context
            ):
                reasoning_emitter.flush()
        if _incremental_delivery_cancel_requested(
            document_id, job_id, delivery_context
        ):
            raise AIAnalysisCancelledError("AI analysis cancelled by user.")
        assessment = result["assessment"]
        runs = [item for item in (result.get("runs") or []) if isinstance(item, dict)]
        verdict = evaluate_assessment_execution(
            assessment=assessment,
            runs=runs,
            configured_mode=str(ai_config.assessment_mode or ""),
        )
        if not verdict.serviceable:
            raise RuntimeError(
                f"{requirement_code} returned an incomplete result: "
                f"{verdict.failure_reason}"
            )
        # Trace files are local runtime diagnostics, not portable serving
        # state.  The durable payload contains only the fields publication
        # consumes, so cutover and redelivery never depend on a worker path.
        staged_result = {
            "assessment": assessment,
            "evidences": [
                item
                for item in (result.get("evidences") or [])
                if isinstance(item, dict)
            ],
            "runs": runs,
        }
        stage_analysis_node_result(
            job_id=job_id,
            target_id=target_id,
            attempt_id=attempt_id,
            fence_token=fence_token,
            result=staged_result,
            analysis_input_schema_version=REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION,
            analysis_input_sha256=requirement_analysis_input_sha256(row),
        )
        staged = True
        publication = publish_staged_analysis_node_result(
            job_id=job_id,
            target_id=target_id,
            attempt_id=attempt_id,
            fence_token=fence_token,
        )
        return {
            "status": "SUCCEEDED",
            "target_id": target_id,
            "attempt_id": attempt_id,
            "shadow_degraded": bool(verdict.shadow_degraded),
            "publication": publication,
        }
    except AIAnalysisCancelledError:
        try:
            cancel_analysis_node_attempt(
                job_id=job_id,
                target_id=target_id,
                attempt_id=attempt_id,
                fence_token=fence_token,
            )
        except (LookupError, RuntimeError) as exc:
            if _is_incremental_delivery_superseded_error(exc):
                raise _IncrementalAnalysisDeliverySuperseded() from exc
            logger.debug(
                "Incremental analysis cancellation lost the node publication race for %s.",
                requirement_code,
            )
        return {"status": "CANCELLED", "target_id": target_id}
    except Exception as exc:
        try:
            cancel_requested = _incremental_delivery_cancel_requested(
                document_id, job_id, delivery_context
            )
        except Exception as control_exc:
            if _is_incremental_delivery_superseded_error(control_exc):
                raise _IncrementalAnalysisDeliverySuperseded() from control_exc
            raise
        if cancel_requested:
            try:
                cancel_analysis_node_attempt(
                    job_id=job_id,
                    target_id=target_id,
                    attempt_id=attempt_id,
                    fence_token=fence_token,
                )
            except (LookupError, RuntimeError) as cancel_exc:
                if _is_incremental_delivery_superseded_error(cancel_exc):
                    raise _IncrementalAnalysisDeliverySuperseded() from cancel_exc
                logger.debug(
                    "Incremental analysis cancellation lost the node race for %s.",
                    requirement_code,
                )
            return {"status": "CANCELLED", "target_id": target_id}
        if _is_incremental_delivery_superseded_error(exc):
            raise _IncrementalAnalysisDeliverySuperseded() from exc
        error_code = _incremental_item_error_code(exc, publication=staged)
        retryable = error_code != "analysis_publication_lineage_changed"
        public_message = _incremental_item_public_message(
            requirement_code,
            error_code=error_code,
        )
        logger.exception(
            "Incremental AI analysis item %s failed for job %s.",
            requirement_code,
            job_id,
        )
        try:
            if staged:
                fail_analysis_node_publication(
                    job_id=job_id,
                    target_id=target_id,
                    attempt_id=attempt_id,
                    fence_token=fence_token,
                    error_code=error_code,
                    error_message=public_message,
                    retryable=retryable,
                )
            else:
                fail_analysis_node_attempt(
                    job_id=job_id,
                    target_id=target_id,
                    attempt_id=attempt_id,
                    fence_token=fence_token,
                    error_code=error_code,
                    error_message=public_message,
                    retryable=retryable,
                )
        except (LookupError, RuntimeError) as persistence_exc:
            if _is_incremental_delivery_superseded_error(persistence_exc):
                raise _IncrementalAnalysisDeliverySuperseded() from persistence_exc
            logger.exception(
                "Could not persist the terminal node state for %s.",
                requirement_code,
            )
            raise
        return {
            "status": "FAILED",
            "target_id": target_id,
            "error_code": error_code,
        }


def _resume_staged_analysis_publications(job_id: str) -> None:
    targets = db.list_ai_analysis_job_targets(job_id)
    with db.get_conn() as conn:
        attempts = {
            str(row["id"] or ""): {key: row[key] for key in row.keys()}
            for row in conn.execute(
                """
                SELECT * FROM ai_analysis_node_attempts
                WHERE job_id = ? AND status = 'SUCCEEDED'
                """,
                (job_id,),
            ).fetchall()
        }
    for target in targets:
        if str(target.get("status") or "").upper() != "PUBLISHING":
            continue
        attempt_id = str(target.get("latest_attempt_id") or "")
        attempt = attempts.get(attempt_id) or {}
        fence_token = str(attempt.get("fence_token") or "")
        if not attempt_id or not fence_token:
            raise RuntimeError("analysis_staged_attempt_missing")
        try:
            publish_staged_analysis_node_result(
                job_id=job_id,
                target_id=str(target.get("id") or ""),
                attempt_id=attempt_id,
                fence_token=fence_token,
            )
        except Exception as exc:
            error_code = _incremental_item_error_code(exc, publication=True)
            fail_analysis_node_publication(
                job_id=job_id,
                target_id=str(target.get("id") or ""),
                attempt_id=attempt_id,
                fence_token=fence_token,
                error_code=error_code,
                error_message=_incremental_item_public_message(
                    str(target.get("requirement_code") or "该项"),
                    error_code=error_code,
                ),
                retryable=error_code != "analysis_publication_lineage_changed",
            )


def _execute_incremental_document_ai_analysis(
    project: dict[str, Any],
    document: dict[str, Any],
    eligible_rows: list[dict[str, Any]],
    knowledge_docs: list[dict[str, Any]],
    *,
    ai_config: Any,
    job_id: str,
    delivery_context: JobExecutionContext | None = None,
) -> dict[str, Any]:
    document_id = str(document.get("id") or "")
    started_at = datetime.now(timezone.utc).isoformat()
    job = db.get_ai_analysis_job(job_id) or {}
    prior_status = str(job.get("status") or "").strip().upper()
    if prior_status not in {"PENDING", "RUNNING"}:
        return job
    try:
        _incremental_delivery_cancel_requested(document_id, job_id, delivery_context)
    except Exception as exc:
        if _is_incremental_delivery_superseded_error(exc):
            raise _IncrementalAnalysisDeliverySuperseded() from exc
        raise
    db.update_ai_analysis_job(
        job_id,
        status="RUNNING",
        current_stage="assessment",
        started_at=str(job.get("started_at") or "") or started_at,
        last_message=f"AI 分析开始，共 {len(eligible_rows)} 条；成功项将逐条保留。",
        error_message="",
    )
    if prior_status == "PENDING":
        db.add_ai_analysis_event(
            job_id=job_id,
            sor_document_id=document_id,
            profile_id=ai_config.profile_id,
            provider=ai_config.provider,
            event_type="job_started",
            stage="assessment",
            message=f"AI 分析启动，共 {len(eligible_rows)} 条待分析需求。",
            payload={
                "total_requirements": len(eligible_rows),
                "publication_mode": PER_NODE_RECOVERABLE_PUBLICATION_MODE,
                "max_parallel_items": _AI_ANALYSIS_MAX_CONCURRENCY,
            },
        )
    try:
        recover_analysis_job_targets(job_id)
        _resume_staged_analysis_publications(job_id)
    except Exception as exc:
        if _is_incremental_delivery_superseded_error(exc):
            raise _IncrementalAnalysisDeliverySuperseded() from exc
        raise
    targets = db.list_ai_analysis_job_targets(job_id)
    row_by_pair = {
        (
            str(row.get("matrix_node_id") or ""),
            str(row.get("id") or ""),
        ): row
        for row in eligible_rows
    }
    frozen_pairs = [
        (str(item.get("node_id") or ""), str(item.get("requirement_id") or ""))
        for item in targets
    ]
    if set(row_by_pair) != set(frozen_pairs):
        raise RuntimeError(
            "AI analysis worker rows do not exactly match the frozen per-node scope."
        )
    waiting_targets = [
        item for item in targets if str(item.get("status") or "").upper() == "WAITING"
    ]
    next_index = 0
    futures: dict[Future[dict[str, Any]], dict[str, Any]] = {}
    unexpected_failure: BaseException | None = None
    delivery_superseded = False
    max_workers = max(1, min(_AI_ANALYSIS_MAX_CONCURRENCY, len(waiting_targets) or 1))
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=f"ai-analysis-node-{document_id[:8]}",
    ) as executor:
        while next_index < len(waiting_targets) or futures:
            cancel_requested = False
            if not delivery_superseded and unexpected_failure is None:
                try:
                    cancel_requested = _incremental_delivery_cancel_requested(
                        document_id, job_id, delivery_context
                    )
                except BaseException as exc:
                    if _is_incremental_delivery_superseded_error(exc):
                        delivery_superseded = True
                        logger.info(
                            "Incremental AI analysis delivery was superseded for job %s.",
                            job_id,
                        )
                    else:
                        unexpected_failure = unexpected_failure or exc
            if cancel_requested:
                cancel_waiting_analysis_targets(job_id)
            while (
                not cancel_requested
                and not delivery_superseded
                and unexpected_failure is None
                and next_index < len(waiting_targets)
                and len(futures) < max_workers
            ):
                target = waiting_targets[next_index]
                next_index += 1
                pair = (
                    str(target.get("node_id") or ""),
                    str(target.get("requirement_id") or ""),
                )
                future = executor.submit(
                    _execute_incremental_analysis_target,
                    job_id=job_id,
                    document_id=document_id,
                    target=target,
                    row=row_by_pair[pair],
                    knowledge_docs=knowledge_docs,
                    ai_config=ai_config,
                    delivery_context=delivery_context,
                )
                futures[future] = target
            if not futures:
                break
            done, _pending = wait(tuple(futures), return_when=FIRST_COMPLETED)
            for future in done:
                target = futures.pop(future)
                try:
                    future.result()
                except (
                    BaseException
                ) as exc:  # keep peers, then close the ledger honestly
                    if _is_incremental_delivery_superseded_error(exc):
                        delivery_superseded = True
                        logger.info(
                            "Incremental AI analysis target delivery was superseded for %s.",
                            str(
                                target.get("requirement_code") or target.get("id") or ""
                            ),
                        )
                    else:
                        logger.exception(
                            "Incremental analysis scheduler lost target %s.",
                            str(
                                target.get("requirement_code") or target.get("id") or ""
                            ),
                        )
                        unexpected_failure = unexpected_failure or exc
    if delivery_superseded:
        raise _IncrementalAnalysisDeliverySuperseded()
    if unexpected_failure is not None:
        raise unexpected_failure
    if _incremental_delivery_cancel_requested(document_id, job_id, delivery_context):
        cancel_waiting_analysis_targets(job_id)
    try:
        return finalize_incremental_analysis_job(job_id)
    except RuntimeError as exc:
        if _is_incremental_delivery_superseded_error(exc):
            raise _IncrementalAnalysisDeliverySuperseded() from exc
        raise


def _execute_document_ai_analysis(
    project: dict[str, Any],
    document: dict[str, Any],
    eligible_rows: list[dict[str, Any]],
    knowledge_docs: list[dict[str, Any]],
    *,
    ai_config: Any,
    job_id: str | None = None,
    all_analysis_target_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from reqpilotx_server.application.requirements import (
        _build_document_workspace,
        _build_published_analysis_fallback_workspace,
        _enrich_requirements_with_preview_context,
        _is_info_only_requirement,
        _record_assessment_runs,
        _strip_workspace_preview,
    )
    from reqpilotx_server.application.runtime import _runtime_record_path

    project_id = str(project.get("id") or "")
    document_id = str(document.get("id") or "")
    total_requirements = len(eligible_rows)
    started_at = datetime.now(timezone.utc).isoformat()
    job_snapshot: dict[str, Any] = {}
    current_document = db.get_sor_document(document_id) or document
    current_project = db.get_project(project_id) or project
    analysis_requirement_set_id = str(
        current_document.get("active_requirement_set_id") or ""
    )
    analysis_requirement_set_version = int(
        current_document.get("requirement_set_version") or 0
    )
    analysis_knowledge_base_version = int(
        current_project.get("knowledge_base_version") or 0
    )
    frozen_scope: dict[str, Any] | None = None
    frozen_target_pairs: tuple[tuple[str, str], ...] = ()
    if job_id:
        job_snapshot = db.get_ai_analysis_job(job_id) or {}
        job_requirement_set_id = str(job_snapshot.get("requirement_set_id") or "")
        pending_membership = {
            str(row.get("requirement_set_id") or "") for row in eligible_rows
        }
        if (
            not job_snapshot
            or str(job_snapshot.get("sor_document_id") or "") != document_id
            or job_requirement_set_id != analysis_requirement_set_id
            or pending_membership != {job_requirement_set_id}
            or int(job_snapshot.get("knowledge_base_version") or 0)
            != analysis_knowledge_base_version
        ):
            raise RuntimeError(
                "AI analysis input snapshot was superseded before worker execution "
                "(Requirement Set or knowledge lineage changed)."
            )
        job_scope_payload = str(job_snapshot.get("scope_payload_json") or "").strip()
        job_scope_sha256 = str(job_snapshot.get("scope_sha256") or "").strip()
        if job_scope_payload and job_scope_sha256:
            try:
                frozen_scope = parse_assessment_scope(
                    job_scope_payload,
                    expected_scope_sha256=str(job_snapshot.get("scope_sha256") or ""),
                )
            except AssessmentScopeError as exc:
                raise RuntimeError(
                    f"AI analysis job scope is invalid: {exc.code}."
                ) from exc
            current_set = db.get_requirement_set(job_requirement_set_id) or {}
            current_tree_sha256 = str(
                current_set.get("requirement_tree_sha256") or ""
            ).strip()
            if (
                str(frozen_scope.get("requirement_set_id") or "")
                != job_requirement_set_id
                or int(frozen_scope.get("requirement_set_version") or 0)
                != analysis_requirement_set_version
                or int(job_snapshot.get("requirement_set_version") or 0)
                != analysis_requirement_set_version
                or str(frozen_scope.get("tree_sha256") or "")
                != str(job_snapshot.get("requirement_tree_sha256") or "")
                or (
                    current_tree_sha256
                    and str(frozen_scope.get("tree_sha256") or "")
                    != current_tree_sha256
                )
                or str(job_snapshot.get("publication_mode") or "").upper()
                != ATOMIC_BATCH_PUBLICATION_MODE
            ):
                raise RuntimeError(
                    "AI analysis input snapshot was superseded before worker execution "
                    "(frozen scope lineage changed)."
                )
            frozen_target_pairs = scope_target_pairs(frozen_scope)
            actual_target_pairs = tuple(
                (
                    str(row.get("matrix_node_id") or "").strip(),
                    str(row.get("id") or "").strip(),
                )
                for row in eligible_rows
            )
            if actual_target_pairs != frozen_target_pairs:
                raise RuntimeError(
                    "AI analysis worker rows do not exactly match the frozen scope."
                )
    logger.info(
        "Starting AI analysis job %s for document %s with profile %s (%s), %s requirement(s) and %s knowledge document(s).",
        job_id or "sync",
        document_id,
        ai_config.profile_id,
        ai_config.provider,
        total_requirements,
        len(knowledge_docs),
    )
    if job_id:
        if frozen_scope is None:
            projected = db.update_sor_document_ai_state_for_job(
                job_id,
                ai_status="RUNNING",
                workflow_stage="AI_ANALYZING",
            )
            if not projected:
                raise RuntimeError(
                    "AI analysis input snapshot was superseded before worker execution."
                )
    else:
        db.update_sor_document_ai_state(
            document_id, ai_status="RUNNING", workflow_stage="AI_ANALYZING"
        )
    if job_id:
        db.update_ai_analysis_job(
            job_id,
            status="RUNNING",
            total_requirements=total_requirements,
            current_stage="starting",
            started_at=started_at,
            last_message=f"AI analysis started for {total_requirements} requirement(s).",
            error_message="",
        )
        db.add_ai_analysis_event(
            job_id=job_id,
            sor_document_id=document_id,
            profile_id=ai_config.profile_id,
            provider=ai_config.provider,
            event_type="job_started",
            stage="starting",
            message=f"AI 分析启动，共 {total_requirements} 条待分析需求。",
            payload={
                "profile_id": ai_config.profile_id,
                "provider": ai_config.provider,
                "total_requirements": total_requirements,
                "knowledge_docs_used": len(knowledge_docs),
            },
        )

    analyzed_count = 0
    info_count = 0
    screened_count = 0
    completed_count = 0
    warning_count = 0
    warning_reasons: list[str] = []
    pending_results: list[tuple[str, dict[str, Any]]] = []
    from reqpilotx_server.application.requirement_set_impact_service import (
        REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION,
        requirement_analysis_input_sha256,
    )

    analysis_input_sha256_by_requirement_id: dict[str, str] = {}
    active_requirement_id = ""
    active_requirement_code = ""
    try:
        for index, row in enumerate(eligible_rows, start=1):
            if job_id:
                _raise_if_ai_analysis_cancel_requested(document_id, job_id)
            requirement_id = str(row.get("id") or "").strip()
            requirement_code = str(
                row.get("display_req_code") or row.get("req_code") or requirement_id
            ).strip()
            active_requirement_id = requirement_id
            active_requirement_code = requirement_code
            if not requirement_id:
                raise RuntimeError(
                    f"AI analysis row {index}/{total_requirements} is missing a requirement id."
                )
            display_text = str(
                row.get("display_text") or row.get("requirement_text") or ""
            ).strip()
            if not display_text:
                raise RuntimeError(
                    f"{requirement_code} has no analysable requirement text "
                    f"({completed_count}/{total_requirements} rows completed)."
                )
            if job_id:
                db.update_ai_analysis_job(
                    job_id,
                    current_requirement_id=requirement_id,
                    current_requirement_code=requirement_code,
                    current_requirement_index=index,
                    current_stage="screening",
                    last_message=f"正在筛选 {requirement_code}（{index}/{total_requirements}）",
                )
                db.add_ai_analysis_event(
                    job_id=job_id,
                    sor_document_id=document_id,
                    profile_id=ai_config.profile_id,
                    provider=ai_config.provider,
                    event_type="requirement_started",
                    stage="screening",
                    message=f"开始处理 {requirement_code}（{index}/{total_requirements}）",
                    requirement_id=requirement_id,
                    requirement_code=requirement_code,
                    payload={"index": index, "total": total_requirements},
                )

            reasoning_emitter = (
                _AIReasoningEventEmitter(
                    job_id=job_id,
                    document_id=document_id,
                    requirement_id=requirement_id,
                    requirement_code=requirement_code,
                    profile_id=ai_config.profile_id,
                    provider=ai_config.provider,
                    cancel_checker=(
                        lambda: _is_ai_analysis_cancel_requested(
                            document_id, str(job_id or "")
                        )
                    ),
                )
                if job_id
                else None
            )
            try:
                try:
                    result = run_ai_copilot_assessment(
                        row,
                        knowledge_docs,
                        config=ai_config,
                        progress_callback=reasoning_emitter.handle
                        if reasoning_emitter
                        else None,
                    )
                finally:
                    if reasoning_emitter and not (
                        job_id and _is_ai_analysis_cancel_requested(document_id, job_id)
                    ):
                        reasoning_emitter.flush()
            except AIAnalysisCancelledError:
                raise
            except Exception as exc:
                if job_id:
                    db.add_ai_analysis_event(
                        job_id=job_id,
                        sor_document_id=document_id,
                        profile_id=ai_config.profile_id,
                        provider=ai_config.provider,
                        event_type="requirement_failed",
                        stage="failed",
                        message=f"{requirement_code} 执行失败：{exc}",
                        requirement_id=requirement_id,
                        requirement_code=requirement_code,
                        payload={
                            "reason": str(exc),
                            "completed_requirements": completed_count,
                        },
                    )
                raise RuntimeError(
                    f"{requirement_code} execution failed: {exc}"
                ) from exc
            if job_id:
                _raise_if_ai_analysis_cancel_requested(document_id, job_id)
            assessment = result["assessment"]
            runs = [
                item for item in (result.get("runs") or []) if isinstance(item, dict)
            ]
            _record_assessment_runs(requirement_id, runs)
            verdict = evaluate_assessment_execution(
                assessment=assessment,
                runs=runs,
                configured_mode=str(ai_config.assessment_mode or ""),
            )
            if not verdict.serviceable:
                failure_message = (
                    f"{requirement_code} returned {verdict.assessment_mode or 'an incomplete result'}: "
                    f"{verdict.failure_reason}"
                )
                if job_id:
                    db.add_ai_analysis_event(
                        job_id=job_id,
                        sor_document_id=document_id,
                        profile_id=ai_config.profile_id,
                        provider=ai_config.provider,
                        event_type="requirement_failed",
                        stage="failed",
                        message=failure_message,
                        requirement_id=requirement_id,
                        requirement_code=requirement_code,
                        payload={
                            "assessment_mode": verdict.assessment_mode,
                            "fallback_reasons": [verdict.failure_reason]
                            if verdict.failure_reason
                            else [],
                            "completed_requirements": completed_count,
                        },
                    )
                raise RuntimeError(failure_message)

            pending_results.append((requirement_id, result))
            analysis_input_sha256_by_requirement_id[requirement_id] = (
                requirement_analysis_input_sha256(row)
            )
            if _is_info_only_requirement(assessment):
                info_count += 1
                result_label = "INFO"
            elif verdict.ai_decision:
                analyzed_count += 1
                result_label = verdict.ai_decision
            else:
                screened_count += 1
                result_label = "待分析"
            completed_count += 1
            if verdict.shadow_degraded:
                warning_count += 1
                for reason in verdict.shadow_warning_reasons:
                    if reason not in warning_reasons:
                        warning_reasons.append(reason)

            fallback_reasons = [
                str(run.get("fallback_reason") or "").strip()
                for run in runs
                if str(run.get("fallback_reason") or "").strip()
            ]
            validation_statuses = sorted(
                {
                    str(run.get("validation_status") or "").strip()
                    for run in runs
                    if str(run.get("validation_status") or "").strip()
                }
            )
            event_payload = {
                "profile_id": ai_config.profile_id,
                "provider": ai_config.provider,
                "analysis_label": verdict.analysis_label,
                "ai_decision": verdict.ai_decision,
                "assessment_mode": verdict.assessment_mode,
                "evidence_count": len(result.get("evidences") or []),
                "fallback_reasons": fallback_reasons,
                "validation_statuses": validation_statuses,
                "outcome": "SHADOW_DEGRADED"
                if verdict.shadow_degraded
                else "COMPLETED",
            }
            if verdict.shadow_degraded:
                event_message = (
                    f"完成 {requirement_code}：服务结论来自 heuristic；shadow 执行失败或降级，"
                    f"未用于服务（{'；'.join(verdict.shadow_warning_reasons)}）。"
                )
            else:
                event_message = f"完成 {requirement_code}：{result_label}，证据 {len(result.get('evidences') or [])} 条。"
            if job_id:
                db.update_ai_analysis_job(
                    job_id,
                    completed_requirements=completed_count,
                    analyzed_requirements=analyzed_count,
                    info_requirements=info_count,
                    screened_requirements=screened_count,
                    current_stage="assessment",
                    last_message=event_message,
                )
                db.add_ai_analysis_event(
                    job_id=job_id,
                    sor_document_id=document_id,
                    profile_id=ai_config.profile_id,
                    provider=ai_config.provider,
                    event_type="requirement_completed",
                    stage="assessment",
                    message=event_message,
                    requirement_id=requirement_id,
                    requirement_code=requirement_code,
                    payload=event_payload,
                )
        if (
            completed_count != total_requirements
            or len(pending_results) != total_requirements
        ):
            raise RuntimeError(
                f"AI analysis was incomplete: {completed_count}/{total_requirements} rows produced valid results."
            )
        configured_mode = str(ai_config.assessment_mode or "").strip().lower()
        serving_mode = (
            "heuristic"
            if configured_mode in {"heuristic", "llm_shadow"}
            else "llm_primary"
        )
        if warning_count:
            completion_message = (
                f"AI 分析完成但有警告：{warning_count} 条需求的 shadow 执行失败或降级；"
                f"服务结论均来自 heuristic。判定 {analyzed_count} 条，INFO {info_count} 条，"
                f"待分析 {screened_count} 条。"
            )
            completion_outcome = "COMPLETED_WITH_WARNINGS"
        else:
            completion_message = (
                f"AI 分析完成：判定 {analyzed_count} 条，标记 INFO {info_count} 条，"
                f"待分析 {screened_count} 条，知识文档 {len(knowledge_docs)} 份。"
            )
            completion_outcome = "COMPLETED"
        completion_event_payload = {
            "profile_id": ai_config.profile_id,
            "provider": ai_config.provider,
            "configured_mode": configured_mode,
            "serving_mode": serving_mode,
            "analyzed_requirements": analyzed_count,
            "info_requirements": info_count,
            "screened_requirements": screened_count,
            "knowledge_docs_used": len(knowledge_docs),
            "completed_requirements": completed_count,
            "total_requirements": total_requirements,
            "warning_count": warning_count,
            "warning_requirement_count": warning_count,
            "warning_reasons": warning_reasons,
            "outcome": completion_outcome,
            "published": True,
            "publication_mode": ATOMIC_BATCH_PUBLICATION_MODE,
            "published_requirements": total_requirements,
        }
        job_completion: dict[str, Any] | None = None
        if job_id:
            if not _begin_ai_analysis_publish(document_id, job_id):
                if _is_ai_analysis_cancel_requested(document_id, job_id):
                    raise AIAnalysisCancelledError(
                        "AI analysis cancelled before atomic publication."
                    )
                raise RuntimeError(
                    "AI analysis worker runtime disappeared before atomic publication."
                )
            db.update_ai_analysis_job(
                job_id,
                current_stage="publishing",
                last_message=f"全部 {completed_count} 条已计算，正在发布本次完整结果。",
            )
            job_completion = {
                "job_id": job_id,
                "completed_requirements": completed_count,
                "analyzed_requirements": analyzed_count,
                "info_requirements": info_count,
                "screened_requirements": screened_count,
                "total_requirements": total_requirements,
                "message": completion_message,
                "event_payload": completion_event_payload,
            }
        updated_document = publish_serving_results(
            pending_results,
            document_id=document_id,
            requirement_set_id=(
                str(job_snapshot.get("requirement_set_id") or "")
                if job_id
                else analysis_requirement_set_id
            ),
            requirement_set_version=analysis_requirement_set_version,
            knowledge_base_version=analysis_knowledge_base_version,
            profile_id=ai_config.profile_id,
            provider=ai_config.provider,
            retrieval_backend=ai_config.retrieval_backend,
            analysis_input_schema_version=(REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION),
            analysis_input_sha256_by_requirement_id=(
                analysis_input_sha256_by_requirement_id
            ),
            scope_sha256=(
                str(job_snapshot.get("scope_sha256") or "") if job_id else ""
            ),
            requirement_tree_sha256=(
                str(job_snapshot.get("requirement_tree_sha256") or "")
                if job_id
                else str(
                    (db.get_requirement_set(analysis_requirement_set_id) or {}).get(
                        "requirement_tree_sha256"
                    )
                    or ""
                )
            ),
            pending_target_pairs=(
                list(frozen_target_pairs)
                if frozen_scope is not None
                else [
                    (
                        str(row.get("matrix_node_id") or "").strip(),
                        str(row.get("id") or "").strip(),
                    )
                    for row in eligible_rows
                    if str(row.get("matrix_node_id") or "").strip()
                ]
            ),
            all_analysis_target_requirement_ids=[
                str(row.get("id") or "").strip()
                for row in (all_analysis_target_rows or eligible_rows)
                if str(row.get("id") or "").strip()
            ],
            job_completion=job_completion,
        )
    except AIAnalysisCancelledError:
        logger.info(
            "AI analysis execution cancelled for job %s on document %s.",
            job_id or "sync",
            document_id,
        )
        if not job_id:
            db.update_sor_document_ai_state(
                document_id,
                ai_status="STALE",
                workflow_stage="EXTRACTION_LOCKED",
            )
        if job_id:
            finished_at = datetime.now(timezone.utc).isoformat()
            completion_message = f"AI 分析已停止：已完成 {completed_count}/{total_requirements} 条；未发布本次部分结果。"
            db.update_ai_analysis_job(
                job_id,
                status="CANCELLED",
                completed_requirements=completed_count,
                analyzed_requirements=analyzed_count,
                info_requirements=info_count,
                screened_requirements=screened_count,
                current_stage="cancelled",
                last_message=completion_message,
                error_message="",
                finished_at=finished_at,
                current_requirement_id="",
                current_requirement_code="",
            )
            db.add_ai_analysis_event(
                job_id=job_id,
                sor_document_id=document_id,
                profile_id=ai_config.profile_id,
                provider=ai_config.provider,
                event_type="job_cancelled",
                stage="cancelled",
                message=completion_message,
                payload={
                    "profile_id": ai_config.profile_id,
                    "provider": ai_config.provider,
                    "completed_requirements": completed_count,
                    "total_requirements": total_requirements,
                    "published": False,
                },
            )
            _try_restore_document_ai_projection_after_unpublished_job(job_id)
        updated_document = (
            db.backfill_sor_document_runtime(document_id)
            or db.get_sor_document(document_id)
            or document
        )
        requirements = db.list_requirements(project_id, document_id)
        requirements = _enrich_requirements_with_preview_context(
            requirements, str(_runtime_record_path(updated_document))
        )
        preview, preview_meta = load_cached_preview(updated_document)
        workspace = _build_document_workspace(
            project=project,
            document=updated_document,
            requirements=requirements,
            knowledge_docs_count=len(knowledge_docs),
            preview=preview,
            preview_meta=preview_meta,
        )
        return {
            **_strip_workspace_preview(workspace),
            "ai_processed_requirements": analyzed_count,
            "ai_info_requirements": info_count,
            "ai_screened_requirements": screened_count,
            "knowledge_docs_used": len(knowledge_docs),
        }
    except Exception as exc:
        logger.exception(
            "AI analysis execution failed for job %s on document %s.",
            job_id or "sync",
            document_id,
        )
        if not job_id:
            db.update_sor_document_ai_state(
                document_id,
                ai_status="STALE",
                workflow_stage="EXTRACTION_LOCKED",
            )
        if job_id:
            finished_at = datetime.now(timezone.utc).isoformat()
            db.update_ai_analysis_job(
                job_id,
                status="FAILED",
                completed_requirements=completed_count,
                analyzed_requirements=analyzed_count,
                info_requirements=info_count,
                screened_requirements=screened_count,
                current_stage="failed",
                last_message=(
                    f"AI 分析失败：{exc}（已完成 {completed_count}/{total_requirements} 条；"
                    "未发布本次部分结果。）"
                ),
                error_message=str(exc),
                finished_at=finished_at,
            )
            db.add_ai_analysis_event(
                job_id=job_id,
                sor_document_id=document_id,
                profile_id=ai_config.profile_id,
                provider=ai_config.provider,
                event_type="job_failed",
                stage="failed",
                message=(
                    f"AI 分析失败：{exc}（已完成 {completed_count}/{total_requirements} 条；"
                    "未发布本次部分结果。）"
                ),
                requirement_id=active_requirement_id,
                requirement_code=active_requirement_code,
                payload={
                    "reason": str(exc),
                    "completed_requirements": completed_count,
                    "total_requirements": total_requirements,
                    "analyzed_requirements": analyzed_count,
                    "info_requirements": info_count,
                    "screened_requirements": screened_count,
                    "published": False,
                },
            )
            _try_restore_document_ai_projection_after_unpublished_job(job_id)
        raise

    workspace_projection_status = "READY"
    workspace_projection_error = ""
    fallback_requirements = eligible_rows
    fallback_requirements_current = False
    try:
        requirements = db.list_requirements(project_id, document_id)
        requirements = _enrich_requirements_with_preview_context(
            requirements,
            str(_runtime_record_path(updated_document)),
        )
        fallback_requirements = requirements
        fallback_requirements_current = True
        preview, preview_meta = load_cached_preview(updated_document)
        workspace = _build_document_workspace(
            project=project,
            document=updated_document,
            requirements=requirements,
            knowledge_docs_count=len(knowledge_docs),
            preview=preview,
            preview_meta=preview_meta,
        )
    except Exception as exc:
        logger.exception(
            "AI results were published for document %s, but workspace response assembly failed.",
            document_id,
        )
        workspace_projection_status = "FAILED"
        workspace_projection_error = str(exc)
        workspace = _build_published_analysis_fallback_workspace(
            project=project,
            document=updated_document,
            requirements=fallback_requirements,
            knowledge_docs_count=len(knowledge_docs),
            projection_error=workspace_projection_error,
        )
    logger.info(
        "Completed AI analysis job %s for document %s: analyzed=%s info=%s screened=%s knowledge_docs=%s.",
        job_id or "sync",
        document_id,
        analyzed_count,
        info_count,
        screened_count,
        len(knowledge_docs),
    )
    return {
        **_strip_workspace_preview(workspace),
        "ai_processed_requirements": analyzed_count,
        "ai_info_requirements": info_count,
        "ai_screened_requirements": screened_count,
        "knowledge_docs_used": len(knowledge_docs),
        "ai_analysis_outcome": completion_outcome,
        "ai_analysis_published": True,
        "ai_analysis_warning_count": warning_count,
        "ai_analysis_warning_requirement_count": warning_count,
        "ai_analysis_warning_reasons": warning_reasons,
        "workspace_projection_status": workspace_projection_status,
        "workspace_projection_error": workspace_projection_error,
        "workspace_requirements_current": (
            True
            if workspace_projection_status == "READY"
            else fallback_requirements_current
        ),
    }


def _run_document_ai_analysis_worker(
    job_id: str,
    project_id: str,
    document_id: str,
    *,
    delivery_context: JobExecutionContext | None = None,
) -> bool:
    incremental = False
    try:
        analysis_job = db.get_ai_analysis_job(job_id) or {}
        if str(analysis_job.get("status") or "").strip().upper() in {
            "COMPLETED",
            "CANCELLED",
        }:
            return
        if (
            str(analysis_job.get("status") or "").strip().upper() == "FAILED"
            and str(analysis_job.get("publication_mode") or "").strip().upper()
            == PER_NODE_RECOVERABLE_PUBLICATION_MODE
        ):
            return
        incremental = (
            str(analysis_job.get("publication_mode") or "").strip().upper()
            == PER_NODE_RECOVERABLE_PUBLICATION_MODE
        )
        if incremental and delivery_context is not None:
            delivery_context.heartbeat()
        # A staged serving payload is self-contained.  Replay it before
        # loading provider/profile/knowledge state so a transient runtime
        # outage cannot discard a result that was already computed durably.
        if incremental and str(analysis_job.get("status") or "").strip().upper() in {
            "PENDING",
            "RUNNING",
        }:
            recover_analysis_job_targets(job_id)
            _resume_staged_analysis_publications(job_id)
            recovered_targets = db.list_ai_analysis_job_targets(job_id)
            if recovered_targets and all(
                str(item.get("status") or "").strip().upper()
                in {"SUCCEEDED", "FAILED", "CANCELLED"}
                for item in recovered_targets
            ):
                finalize_incremental_analysis_job(job_id)
                return
        (
            project,
            document,
            _requirements,
            all_eligible_rows,
            knowledge_docs,
            ai_config,
        ) = _prepare_document_ai_analysis_request(
            project_id,
            document_id,
            profile_id=str(analysis_job.get("profile_id") or ""),
            requirement_set_id=str(analysis_job.get("requirement_set_id") or ""),
        )
        execution_identity = {
            "profile_id": str(ai_config.profile_id or "").strip(),
            "provider": str(ai_config.provider or "").strip(),
            "assessment_mode": str(ai_config.assessment_mode or "").strip(),
            "retrieval_backend": str(ai_config.retrieval_backend or "").strip(),
            "chat_model": str(ai_config.chat_model or "").strip(),
            "embedding_model": str(ai_config.embedding_model or "").strip(),
        }
        if any(
            str(analysis_job.get(field) or "").strip() != value
            for field, value in execution_identity.items()
        ):
            raise RuntimeError(
                "AI analysis execution identity changed before worker execution."
            )
        scope_payload_json = str(analysis_job.get("scope_payload_json") or "").strip()
        scope_sha256 = str(analysis_job.get("scope_sha256") or "").strip()
        if scope_payload_json and scope_sha256:
            try:
                frozen_scope = parse_assessment_scope(
                    scope_payload_json,
                    expected_scope_sha256=str(analysis_job.get("scope_sha256") or ""),
                )
            except AssessmentScopeError as exc:
                raise RuntimeError(
                    f"AI analysis job scope is invalid: {exc.code}."
                ) from exc
            frozen_scope_kind = str(frozen_scope.get("scope") or "").upper()
            requested_node_ids = (
                ()
                if frozen_scope_kind in {"ALL", "FAILED_ONLY"}
                else tuple(
                    str(item or "").strip()
                    for item in frozen_scope.get("requested_node_ids") or []
                )
            )
            _scope_project, _scope_document, _scope_set, current_scope = (
                _resolve_document_ai_analysis_scope(
                    project_id,
                    document_id,
                    requirement_set_id=str(
                        analysis_job.get("requirement_set_id") or ""
                    ),
                    expected_tree_sha256=str(
                        analysis_job.get("requirement_tree_sha256") or ""
                    ),
                    scope=frozen_scope_kind,
                    node_ids=requested_node_ids,
                    scope_schema_version=str(
                        frozen_scope.get("schema_version")
                        or ASSESSMENT_SCOPE_SCHEMA_VERSION_V1
                    ),
                    source_job_id=str(frozen_scope.get("source_job_id") or ""),
                    retry_exclude_job_id=job_id,
                )
            )
            if str(current_scope.get("scope_sha256") or "") != str(
                analysis_job.get("scope_sha256") or ""
            ):
                raise RuntimeError(
                    "AI analysis Matrix target membership changed before worker execution."
                )
            try:
                eligible_rows = select_rows_for_assessment_scope(
                    all_eligible_rows,
                    frozen_scope,
                )
            except AssessmentScopeError as exc:
                raise RuntimeError(
                    f"AI analysis Matrix target membership is stale: {exc.code}."
                ) from exc
        else:
            # Jobs created before W6-A remain readable and run as whole-Matrix
            # batches.  New jobs always persist a frozen scope.
            eligible_rows = all_eligible_rows
        if incremental:
            _execute_incremental_document_ai_analysis(
                project,
                document,
                eligible_rows,
                knowledge_docs,
                ai_config=ai_config,
                job_id=job_id,
                delivery_context=delivery_context,
            )
        else:
            _execute_document_ai_analysis(
                project,
                document,
                eligible_rows,
                knowledge_docs,
                ai_config=ai_config,
                job_id=job_id,
                all_analysis_target_rows=all_eligible_rows,
            )
    except Exception as exc:
        if _is_incremental_delivery_superseded_error(exc):
            logger.info(
                "AI analysis delivery was superseded for job %s; the replacement delivery owns recovery.",
                job_id,
            )
            return True
        logger.exception(
            "AI analysis worker failed for job %s on document %s.",
            job_id,
            document_id,
        )
        if incremental and delivery_context is not None:
            # A durable delivery may overlap its replacement after losing the
            # queue lease.  Leave its fenced node state for redelivery instead
            # of sweeping job-wide targets from an unowned delivery.
            raise
        current = db.get_ai_analysis_job(job_id) or {}
        current_status = str(current.get("status") or "").strip().upper()
        if current_status not in {"FAILED", "COMPLETED", "CANCELLED"}:
            if (
                str(current.get("publication_mode") or "").strip().upper()
                == PER_NODE_RECOVERABLE_PUBLICATION_MODE
            ):
                abandon_incremental_analysis_job(
                    job_id,
                    cancelled=(
                        current_status == "CANCEL_REQUESTED"
                        or isinstance(exc, AIAnalysisCancelledError)
                    ),
                )
                return
            finished_at = datetime.now(timezone.utc).isoformat()
            cancelled = current_status == "CANCEL_REQUESTED" or isinstance(
                exc, AIAnalysisCancelledError
            )
            db.update_ai_analysis_job(
                job_id,
                status="CANCELLED" if cancelled else "FAILED",
                current_stage="cancelled" if cancelled else "failed",
                last_message="AI 分析已停止。" if cancelled else f"AI 分析失败：{exc}",
                error_message="" if cancelled else str(exc),
                finished_at=finished_at,
                current_requirement_id="",
                current_requirement_code="",
            )
            db.add_ai_analysis_event(
                job_id=job_id,
                sor_document_id=document_id,
                profile_id=str(current.get("profile_id") or ""),
                provider=str(current.get("provider") or ""),
                event_type="job_cancelled" if cancelled else "job_failed",
                stage="cancelled" if cancelled else "failed",
                message="AI 分析已停止。" if cancelled else f"AI 分析失败：{exc}",
                payload={"reason": "cancelled_by_user" if cancelled else str(exc)},
            )
            _try_restore_document_ai_projection_after_unpublished_job(job_id)
    finally:
        _clear_ai_analysis_thread(
            document_id,
            job_id=job_id,
            durable_context=delivery_context,
        )
    return False


def run_durable_document_assessment(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    payload = dict(durable_job.payload or {})
    analysis_job_id = str(payload.get("analysis_job_id") or "")
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    bound = db.get_ai_analysis_job(analysis_job_id) or {}
    requested_requirement_set_id = str(payload.get("requirement_set_id") or "")
    requested_command_id = str(payload.get("command_id") or "").strip()
    requested_scope_sha256 = str(payload.get("scope_sha256") or "").strip()
    if (
        not bound
        or str(bound.get("project_id") or "") != project_id
        or str(bound.get("sor_document_id") or "") != document_id
        or str(bound.get("runtime_job_id") or "") != durable_job.job_id
        or not requested_requirement_set_id
        or str(bound.get("requirement_set_id") or "") != requested_requirement_set_id
        or requested_command_id != str(bound.get("command_id") or "").strip()
        or requested_scope_sha256 != str(bound.get("scope_sha256") or "").strip()
    ):
        raise RuntimeError("AI analysis durable binding is stale.")
    bound_status = str(bound.get("status") or "").strip().upper()
    if bound_status == "COMPLETED":
        return {
            "analysis_job_id": analysis_job_id,
            "document_id": document_id,
            "status": "COMPLETED",
            "completed_requirements": int(bound.get("completed_requirements") or 0),
        }
    if bound_status == "CANCELLED":
        return {
            "analysis_job_id": analysis_job_id,
            "document_id": document_id,
            "status": "CANCELLED",
        }
    if (
        bound_status == "FAILED"
        and str(bound.get("publication_mode") or "").strip().upper()
        == PER_NODE_RECOVERABLE_PUBLICATION_MODE
    ):
        return {
            "analysis_job_id": analysis_job_id,
            "document_id": document_id,
            "status": "FAILED",
            "published_requirements": int(bound.get("published_requirements") or 0),
        }
    with _AI_ANALYSIS_THREAD_LOCK:
        _AI_ANALYSIS_THREADS[document_id] = {
            "thread": None,
            "job_id": analysis_job_id,
            "starting": False,
            "cancel_requested": False,
            "publishing": False,
            "durable_context": context,
        }
    delivery_superseded = _run_document_ai_analysis_worker(
        analysis_job_id,
        project_id,
        document_id,
        delivery_context=context,
    )
    if delivery_superseded:
        current = db.get_ai_analysis_job(analysis_job_id) or bound
        return {
            "analysis_job_id": analysis_job_id,
            "document_id": document_id,
            "status": str(current.get("status") or "RUNNING").strip().upper(),
            "delivery_superseded": True,
        }
    terminal = db.get_ai_analysis_job(analysis_job_id) or {}
    status = str(terminal.get("status") or "").upper()
    if status == "COMPLETED":
        return {
            "analysis_job_id": analysis_job_id,
            "document_id": document_id,
            "status": status,
            "completed_requirements": int(terminal.get("completed_requirements") or 0),
        }
    if status == "CANCELLED" or context.cancellation_requested():
        return {
            "analysis_job_id": analysis_job_id,
            "document_id": document_id,
            "status": "CANCELLED",
        }
    if (
        status == "FAILED"
        and str(terminal.get("publication_mode") or "").strip().upper()
        == PER_NODE_RECOVERABLE_PUBLICATION_MODE
    ):
        return {
            "analysis_job_id": analysis_job_id,
            "document_id": document_id,
            "status": "FAILED",
            "published_requirements": int(terminal.get("published_requirements") or 0),
        }
    raise RuntimeError(
        str(terminal.get("error_message") or "document_assessment_failed")
    )


def start_document_ai_analysis(
    project_id: str,
    document_id: str,
    payload: RunAIAnalysisRequest,
) -> dict:
    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, _durable_jobs

    requested_profile_id = str(payload.profile_id or "").strip().lower() or None
    requested_requirement_set_id = str(payload.requirement_set_id or "").strip() or None
    command_id = str(payload.command_id or "").strip()
    retry_exclude_job_id = ""
    if str(payload.scope or "").strip().upper() == "FAILED_ONLY" and command_id:
        with db.get_conn() as conn:
            replay_candidate = conn.execute(
                """
                SELECT id
                FROM ai_analysis_jobs
                WHERE sor_document_id = ? AND command_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (document_id, command_id),
            ).fetchone()
        retry_exclude_job_id = str(
            replay_candidate["id"] if replay_candidate is not None else ""
        ).strip()
    project, document, requirement_set, scope_envelope = (
        _resolve_document_ai_analysis_scope(
            project_id,
            document_id,
            requirement_set_id=str(requested_requirement_set_id or ""),
            expected_tree_sha256=str(payload.expected_tree_sha256 or ""),
            scope=payload.scope,
            node_ids=payload.node_ids,
            scope_schema_version=(
                ASSESSMENT_SCOPE_SCHEMA_VERSION_V2
                if payload.schema_version == ASSESSMENT_SCOPE_REQUEST_SCHEMA_VERSION_V2
                else ASSESSMENT_SCOPE_SCHEMA_VERSION_V1
            ),
            source_job_id=str(payload.source_job_id or ""),
            retry_exclude_job_id=retry_exclude_job_id,
        )
    )
    expected_scope_sha256 = str(payload.expected_scope_sha256 or "").strip()
    if expected_scope_sha256 and expected_scope_sha256 != str(
        scope_envelope.get("scope_sha256") or ""
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "analysis_scope_changed",
                "message": "分析范围已变化，请重新预览并确认。",
                "expected_scope_sha256": expected_scope_sha256,
                "actual_scope_sha256": str(scope_envelope.get("scope_sha256") or ""),
            },
        )
    (
        prepared_project,
        prepared_document,
        _requirements,
        all_eligible_rows,
        _knowledge_docs,
        ai_config,
    ) = _prepare_document_ai_analysis_request(
        project_id,
        document_id,
        requested_profile_id,
        requirement_set_id=requested_requirement_set_id,
        hydrate_knowledge_payloads=False,
    )
    try:
        eligible_rows = select_rows_for_assessment_scope(
            all_eligible_rows,
            scope_envelope,
        )
    except AssessmentScopeError as exc:
        raise ApplicationError(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if not eligible_rows:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "analysis_scope_empty",
                "message": "当前范围没有可进入需求分析的条目。",
                "scope_preview": assessment_scope_preview(
                    scope_envelope,
                    command_id=payload.command_id,
                ),
            },
        )
    project = prepared_project
    document = prepared_document
    requirement_set_version = max(int(requirement_set.get("version") or 0), 0)
    requirement_tree_sha256 = str(scope_envelope.get("tree_sha256") or "").strip()
    knowledge_base_version = max(int(project.get("knowledge_base_version") or 0), 0)
    is_scoped_command = bool(command_id)
    command_sha256 = (
        assessment_command_sha256(
            command_id=command_id,
            scope_sha256=str(scope_envelope.get("scope_sha256") or ""),
            requirement_set_id=str(requested_requirement_set_id or ""),
            requirement_set_version=requirement_set_version,
            requirement_tree_sha256=requirement_tree_sha256,
            knowledge_base_version=knowledge_base_version,
            profile_id=ai_config.profile_id,
            provider=ai_config.provider,
            assessment_mode=ai_config.assessment_mode,
            retrieval_backend=ai_config.retrieval_backend,
            chat_model=ai_config.chat_model,
            embedding_model=ai_config.embedding_model,
        )
        if is_scoped_command
        else ""
    )
    queued_message = f"AI 分析已排队，共 {len(eligible_rows)} 条待分析需求。"
    publication_mode = (
        str(scope_envelope.get("publication_mode") or ATOMIC_BATCH_PUBLICATION_MODE)
        .strip()
        .upper()
    )
    target_descriptors = (
        [
            {
                "node_id": str(row.get("matrix_node_id") or "").strip(),
                "requirement_id": str(row.get("id") or "").strip(),
                "requirement_code": str(
                    row.get("display_req_code")
                    or row.get("req_code")
                    or row.get("id")
                    or ""
                ).strip(),
                "source_order": index,
            }
            for index, row in enumerate(eligible_rows)
        ]
        if publication_mode == PER_NODE_RECOVERABLE_PUBLICATION_MODE
        else None
    )
    with _AI_ANALYSIS_START_LOCK:
        _finalize_orphaned_ai_analysis_job(document_id)
        try:
            job, claimed = claim_analysis_job(
                project_id=str(project.get("id") or ""),
                document_id=str(document.get("id") or ""),
                requirement_set_id=str(requested_requirement_set_id or ""),
                profile_id=ai_config.profile_id,
                provider=ai_config.provider,
                assessment_mode=ai_config.assessment_mode,
                retrieval_backend=ai_config.retrieval_backend,
                chat_model=ai_config.chat_model,
                embedding_model=ai_config.embedding_model,
                command_id=command_id,
                command_sha256=command_sha256,
                scope_schema_version=(
                    str(scope_envelope.get("schema_version") or "")
                    if is_scoped_command
                    else ""
                ),
                scope_kind=(
                    str(scope_envelope.get("scope") or "") if is_scoped_command else ""
                ),
                scope_payload_json=(
                    str(scope_envelope.get("scope_payload_json") or "")
                    if is_scoped_command
                    else "{}"
                ),
                scope_sha256=(
                    str(scope_envelope.get("scope_sha256") or "")
                    if is_scoped_command
                    else ""
                ),
                requirement_set_version=requirement_set_version,
                requirement_tree_sha256=requirement_tree_sha256,
                expected_knowledge_base_version=(
                    knowledge_base_version if is_scoped_command else None
                ),
                publication_mode=publication_mode,
                target_descriptors=target_descriptors,
                total_requirements=len(eligible_rows),
                queued_message=queued_message,
            )
        except ValueError as exc:
            conflict_code = str(exc)
            if conflict_code == "analysis_command_id_conflict":
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": conflict_code,
                        "message": "同一操作 ID 已绑定到不同分析范围或执行配置。",
                    },
                ) from exc
            if conflict_code == "analysis_job_already_running":
                active_job = db.get_running_ai_analysis_job(document_id) or {}
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": conflict_code,
                        "message": "当前文档已有另一项分析任务在运行，请等待或先停止。",
                        "active_job_id": str(active_job.get("id") or ""),
                        "active_command_id": str(active_job.get("command_id") or ""),
                    },
                ) from exc
            if conflict_code == "analysis_knowledge_base_changed":
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": conflict_code,
                        "message": "知识库版本已变化，请重新预览并启动分析。",
                    },
                ) from exc
            if conflict_code != "requirement_set_selection_changed":
                raise
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "requirement_set_selection_changed",
                    "message": "当前分析版本已变化，请刷新后重新启动。",
                    "selected_requirement_set_id": requested_requirement_set_id,
                },
            ) from exc
        if not claimed:
            return _build_ai_analysis_status_payload(project_id, document_id, job=job)
        if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
            try:
                durable = _durable_jobs().enqueue(
                    kind="document.assess",
                    aggregate_type="sor-document",
                    aggregate_id=document_id,
                    idempotency_key=str(job.get("id") or ""),
                    payload={
                        "analysis_job_id": str(job.get("id") or ""),
                        "project_id": project_id,
                        "document_id": document_id,
                        "requirement_set_id": requested_requirement_set_id,
                        "profile_id": ai_config.profile_id,
                        "command_id": command_id,
                        "scope_sha256": (
                            str(scope_envelope.get("scope_sha256") or "")
                            if is_scoped_command
                            else ""
                        ),
                    },
                    priority=5,
                    max_attempts=2,
                )
                job = db.bind_ai_analysis_runtime_job(
                    str(job.get("id") or ""),
                    durable.job_id,
                )
            except Exception as exc:
                if publication_mode == PER_NODE_RECOVERABLE_PUBLICATION_MODE:
                    abandon_incremental_analysis_job(
                        str(job.get("id") or ""), cancelled=False
                    )
                else:
                    db.update_ai_analysis_job(
                        str(job.get("id") or ""),
                        status="FAILED",
                        current_stage="failed",
                        last_message="AI analysis could not enter the durable queue.",
                        error_message=str(exc),
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )
                    _try_restore_document_ai_projection_after_unpublished_job(
                        str(job.get("id") or "")
                    )
                raise
            return _build_ai_analysis_status_payload(
                project_id,
                document_id,
                job=job,
            )
        thread = Thread(
            target=_run_document_ai_analysis_worker,
            args=(str(job.get("id") or ""), project_id, document_id),
            name=f"ai-analysis-{document_id[:8]}",
            daemon=True,
        )
        _register_ai_analysis_thread(
            document_id, thread, job_id=str(job.get("id") or "")
        )
        try:
            thread.start()
            _mark_ai_analysis_thread_started(document_id, str(job.get("id") or ""))
        except Exception as exc:
            _clear_ai_analysis_thread(
                document_id, thread=thread, job_id=str(job.get("id") or "")
            )
            finished_at = datetime.now(timezone.utc).isoformat()
            if publication_mode == PER_NODE_RECOVERABLE_PUBLICATION_MODE:
                abandon_incremental_analysis_job(
                    str(job.get("id") or ""), cancelled=False
                )
            else:
                db.update_ai_analysis_job(
                    str(job.get("id") or ""),
                    status="FAILED",
                    current_stage="failed",
                    last_message=f"AI 分析任务无法启动：{exc}",
                    error_message=str(exc),
                    finished_at=finished_at,
                )
                db.add_ai_analysis_event(
                    job_id=str(job.get("id") or ""),
                    sor_document_id=document_id,
                    profile_id=ai_config.profile_id,
                    provider=ai_config.provider,
                    event_type="job_failed",
                    stage="failed",
                    message=f"AI 分析任务无法启动：{exc}",
                    payload={"reason": str(exc), "published": False},
                )
                _try_restore_document_ai_projection_after_unpublished_job(
                    str(job.get("id") or "")
                )
            raise
    logger.info(
        "Queued AI analysis job %s for document %s using profile %s with %s requirement(s).",
        str(job.get("id") or ""),
        document_id,
        ai_config.profile_id,
        len(eligible_rows),
    )
    return _build_ai_analysis_status_payload(project_id, document_id)


def stop_document_ai_analysis(
    project_id: str,
    document_id: str,
    payload: StopAIAnalysisRequest | None = None,
) -> dict:
    from reqpilotx_server.application.projects import _require_project
    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS

    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )
    _finalize_orphaned_ai_analysis_job(document_id)
    job = db.get_running_ai_analysis_job(document_id)
    if not job:
        return _build_ai_analysis_status_payload(project_id, document_id)
    job_id = str(job.get("id") or "")
    expected_job_id = str(
        (payload or StopAIAnalysisRequest()).expected_job_id or ""
    ).strip()
    if expected_job_id and expected_job_id != job_id:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "analysis_stop_job_changed",
                "message": "当前分析任务已经变化，请刷新状态后再停止。",
                "expected_job_id": expected_job_id,
                "active_job_id": job_id,
            },
        )
    status = str(job.get("status") or "").strip().upper()
    if status == "CANCEL_REQUESTED":
        return _build_ai_analysis_status_payload(project_id, document_id, job=job)
    if not _has_live_ai_analysis_thread(document_id):
        finalized = _finalize_orphaned_ai_analysis_job(document_id)
        return _build_ai_analysis_status_payload(
            project_id, document_id, job=finalized or job
        )
    if (
        RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL
        and str(job.get("current_stage") or "").strip().lower() == "publishing"
        and str(job.get("publication_mode") or "").strip().upper()
        != PER_NODE_RECOVERABLE_PUBLICATION_MODE
    ):
        raise ApplicationError(
            status_code=409,
            detail=(
                "AI analysis has entered atomic result publication and "
                "can no longer be cancelled."
            ),
        )
    if not _request_ai_analysis_cancel(document_id, job_id):
        if _is_ai_analysis_publishing(document_id, job_id):
            raise ApplicationError(
                status_code=409,
                detail="AI analysis has entered atomic result publication and can no longer be cancelled.",
            )
        return _build_ai_analysis_status_payload(project_id, document_id)
    incremental = (
        str(job.get("publication_mode") or "").strip().upper()
        == PER_NODE_RECOVERABLE_PUBLICATION_MODE
    )
    message = (
        "已请求停止当前 AI 分析；已发布成功项会保留，正在停止其余项目。"
        if incremental
        else "已请求停止当前 AI 分析，正在等待当前调用安全中断。"
    )
    updated, cancel_claimed = db.request_ai_analysis_job_cancel(
        job_id,
        last_message=message,
    )
    if not cancel_claimed:
        return _build_ai_analysis_status_payload(
            project_id,
            document_id,
            job=updated or db.get_ai_analysis_job(job_id) or job,
        )
    db.add_ai_analysis_event(
        job_id=job_id,
        sor_document_id=document_id,
        profile_id=str(job.get("profile_id") or ""),
        provider=str(job.get("provider") or ""),
        event_type="job_cancel_requested",
        stage="cancelling",
        message=message,
        payload={"reason": "user_requested_stop"},
    )
    if incremental:
        cancel_waiting_analysis_targets(job_id)
    return _build_ai_analysis_status_payload(project_id, document_id, job=updated)


def get_document_ai_analysis_status(
    project_id: str, document_id: str, event_limit: int = 40
) -> dict:
    return _build_ai_analysis_status_payload(
        project_id, document_id, event_limit=max(10, min(event_limit, 200))
    )


def get_requirement_analysis_detail(requirement_id: str) -> dict:
    from reqpilotx_server.application.requirements import (
        _enrich_requirements_with_preview_context,
        _prepare_requirements_for_display,
    )
    from reqpilotx_server.application.runtime import _runtime_record_path

    requirement = db.get_requirement(requirement_id)
    if not requirement:
        raise ApplicationError(status_code=404, detail="Requirement not found.")
    presented_requirement = requirement
    project_id = str(requirement.get("project_id") or "").strip()
    document_id = str(requirement.get("sor_document_id") or "").strip()
    if project_id and document_id:
        document = db.get_sor_document(document_id)
        if document and document.get("project_id") == project_id:
            requirements = db.list_requirements(project_id, document_id)
            requirements = _enrich_requirements_with_preview_context(
                requirements, str(_runtime_record_path(document))
            )
            preview, _ = load_cached_preview(document)
            presented_requirements, _order_diagnostics = (
                _prepare_requirements_for_display(requirements, preview=preview)
            )
            matched_requirement = next(
                (
                    row
                    for row in presented_requirements
                    if str(row.get("id") or "").strip() == requirement_id
                ),
                None,
            )
            if matched_requirement is not None:
                presented_requirement = {**requirement, **matched_requirement}
    assessment = db.get_assessment(requirement_id)
    runs = [
        _serialize_assessment_run_for_ui(item)
        for item in db.list_assessment_runs(requirement_id)
    ]
    normalization = build_requirement_normalization_detail(
        presented_requirement,
        persisted_summary=str((assessment or {}).get("normalized_requirement") or ""),
    )
    return {
        "requirement_id": requirement_id,
        "requirement": presented_requirement,
        "assessment": assessment,
        "normalization": normalization,
        "evidences": presented_requirement.get("evidences", []),
        "assessment_runs": runs,
    }


def get_evidence_context(evidence_id: str) -> dict:
    from reqpilotx_server.application.runtime import _runtime_record_path

    evidence = db.get_evidence(evidence_id)
    if not evidence:
        raise ApplicationError(status_code=404, detail="Evidence not found.")
    source_document = db.get_knowledge_document(
        str(evidence.get("source_doc_id") or "")
    )
    if source_document and source_document.get("project_id") != evidence.get(
        "project_id"
    ):
        source_document = None
    extracted_text = str((source_document or {}).get("extracted_text") or "")
    excerpt = str(evidence.get("excerpt") or "")
    offset, location_strategy, location_confidence, candidate_count = (
        _resolve_evidence_text_position(
            extracted_text,
            excerpt,
            str(evidence.get("source_location") or ""),
        )
    )
    if offset < 0:
        context_text = excerpt
        line_number = 0
    else:
        start = max(0, offset - 700)
        end = min(len(extracted_text), offset + len(excerpt) + 700)
        context_text = extracted_text[start:end]
        line_number = extracted_text.count("\n", 0, offset) + 1
    file_path = _runtime_record_path(source_document) if source_document else Path()
    file_available = bool(
        source_document and file_path.exists() and file_path.is_file()
    )
    evidence_metadata = (
        dict(evidence.get("evidence_metadata") or {})
        if isinstance(evidence.get("evidence_metadata"), dict)
        else {}
    )
    return {
        "evidence_id": evidence_id,
        "retrieval_evidence_id": str(evidence.get("retrieval_evidence_id") or ""),
        "content_hash": str(evidence.get("content_hash") or ""),
        "evidence_contract_version": str(
            evidence.get("evidence_contract_version") or ""
        ),
        "content_kind": str(evidence.get("content_kind") or ""),
        "evidence_metadata": evidence_metadata,
        "requirement_id": evidence.get("requirement_id"),
        "requirement_set_id": evidence.get("requirement_set_id"),
        "reference_status": str(evidence.get("reference_status") or "CURRENT"),
        "source_requirement_id": str(evidence.get("source_requirement_id") or ""),
        "source": {
            "document_id": evidence.get("source_doc_id"),
            "file_name": evidence.get("source_doc_name"),
            "version": evidence.get("source_doc_version"),
            "location": evidence.get("source_location"),
            "line_number": line_number,
            "status": str((source_document or {}).get("status") or "UNKNOWN"),
            "effective_from": str((source_document or {}).get("effective_from") or ""),
            "effective_to": str((source_document or {}).get("effective_to") or ""),
            "assessment_time_status": str(evidence_metadata.get("status") or ""),
            "assessment_time_effective_from": str(
                evidence_metadata.get("effective_from") or ""
            ),
            "assessment_time_effective_to": str(
                evidence_metadata.get("effective_to") or ""
            ),
            "assessment_time_doc_type": str(evidence_metadata.get("doc_type") or ""),
        },
        "excerpt": excerpt,
        "context": context_text,
        "relevance": evidence.get("relevance"),
        "location_strategy": location_strategy,
        "location_confidence": location_confidence,
        "location_candidate_count": candidate_count,
        "file_url": f"/api/evidences/{evidence_id}/file" if file_available else "",
        "open_url": f"/api/evidences/{evidence_id}/file" if file_available else "",
    }


def _resolve_evidence_text_position(
    text: str,
    excerpt: str,
    source_location: str,
) -> tuple[int, str, str, int]:
    if not text or not excerpt:
        return -1, "excerpt_unavailable", "NONE", 0
    occurrences = [match.start() for match in re.finditer(re.escape(excerpt), text)]
    location = str(source_location or "").strip()
    line_match = re.search(r"(?:line|行)\s*[:#]?\s*(\d+)", location, re.I)
    if line_match:
        target_line = max(int(line_match.group(1)), 1)
        lines = text.splitlines(keepends=True)
        if target_line <= len(lines):
            line_offset = sum(len(item) for item in lines[: target_line - 1])
            local_index = lines[target_line - 1].find(excerpt)
            if local_index >= 0:
                return (
                    line_offset + local_index,
                    "source_line_exact",
                    "HIGH",
                    len(occurrences),
                )
            if occurrences:
                nearest = min(occurrences, key=lambda value: abs(value - line_offset))
                return (
                    nearest,
                    "source_line_nearest_excerpt",
                    "MEDIUM",
                    len(occurrences),
                )
    offset_match = re.search(
        r"(?:offset|char(?:acter)?|字符)\s*[:#]?\s*(\d+)", location, re.I
    )
    if offset_match:
        expected = max(int(offset_match.group(1)), 0)
        if text.startswith(excerpt, expected):
            return expected, "source_offset_exact", "HIGH", len(occurrences)
        if occurrences:
            nearest = min(occurrences, key=lambda value: abs(value - expected))
            return nearest, "source_offset_nearest_excerpt", "MEDIUM", len(occurrences)
    if len(occurrences) == 1:
        return occurrences[0], "unique_excerpt", "HIGH", 1
    if occurrences:
        strategy = (
            "page_hint_first_excerpt"
            if re.search(r"(?:page|页)\s*\d+", location, re.I)
            else "ambiguous_excerpt_first"
        )
        return occurrences[0], strategy, "LOW", len(occurrences)
    return -1, "excerpt_not_found", "NONE", 0


def open_evidence_file(evidence_id: str) -> FileArtifact:
    from reqpilotx_server.application.requirements import _safe_name
    from reqpilotx_server.application.runtime import _runtime_record_path

    evidence = db.get_evidence(evidence_id)
    if not evidence:
        raise ApplicationError(status_code=404, detail="Evidence not found.")
    source_document = db.get_knowledge_document(
        str(evidence.get("source_doc_id") or "")
    )
    if not source_document or source_document.get("project_id") != evidence.get(
        "project_id"
    ):
        raise ApplicationError(
            status_code=404, detail="Evidence source file not found."
        )
    path = _runtime_record_path(source_document)
    if not path.exists() or not path.is_file():
        raise ApplicationError(
            status_code=404, detail="Evidence source file not found."
        )
    return FileArtifact(
        path, filename=_safe_name(str(source_document.get("file_name") or path.name))
    )
