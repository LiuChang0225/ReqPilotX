from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app.runtime_readiness import (
    build_ai_profile_check,
    build_readiness_report,
    ensure_runtime_directories,
    runtime_directory_map,
)
from app.runtime_paths import resolve_storage_dir
from app.services.ai_copilot import (
    AIAnalysisCancelledError,
    INDEX_PAYLOAD_DIR,
    apply_runtime_ai_provider_setup,
    build_runtime_ai_provider_setup,
    build_requirement_normalization_detail,
    ensure_ai_profile_runnable,
    get_ai_profile_missing_config,
    is_ascii_runtime_secret,
    list_ai_copilot_profiles,
    load_ai_copilot_config,
    probe_runtime_azure_rbac,
    run_ai_copilot_assessment,
    serialize_ai_profile,
    sync_knowledge_document_index,
)
from app.services.complex_table_mode import (
    hydrate_complex_row_metadata,
    is_complex_review_only_row,
    is_complex_table_row,
    row_is_shadowed_by_complex_group,
)
from app.services.extraction_preview import build_extraction_preview
from app.services.exporter import export_compliance_matrix
from app.services.parser import parse_document
from app.services.phase0_review import (
    load_review_dataset,
    refresh_baseline_report,
    update_item_labels,
    update_page_labels,
)
from app.services.iteration_insights import (
    build_error_closure,
    build_round_diff,
    list_iteration_rounds,
    load_high_risk_queue,
)
from app.services.progress_tracker import (
    add_progress_event,
    load_branch_snapshots,
    load_gate_statuses,
    load_progress_snapshot,
    replace_plan_items,
    update_progress_checkpoint,
)
from app.services.preview_cache import (
    get_preview_cache_state,
    load_cached_preview,
    mark_preview_stale,
    persist_preview_cache,
    reset_document_snapshot_root,
    request_preview_refresh,
)
from app.services.table_assets import (
    _refresh_enriched_entry_text_fields,
    enrich_entries_with_table_assets,
    enrich_entry_with_table_assets,
    export_blocks_from_requirement,
    normalize_preserved_entry_table_assets,
    prepare_table_asset_urls,
    prepare_table_ref_urls,
)


APP_DIR = Path(__file__).resolve().parent
PROTOTYPE_DIR = APP_DIR.parent
STORAGE_DIR = resolve_storage_dir(PROTOTYPE_DIR)
UPLOADS_DIR = STORAGE_DIR / "uploads"
KNOWLEDGE_DIR = STORAGE_DIR / "knowledge"
EXPORTS_DIR = STORAGE_DIR / "exports"
TABLE_SNAPSHOT_DIR = STORAGE_DIR / "table_snapshots"
STATIC_DIR = APP_DIR / "static"


logger = logging.getLogger(__name__)


_AI_ANALYSIS_THREAD_LOCK = Lock()
_AI_ANALYSIS_THREADS: dict[str, dict[str, Any]] = {}
_APP_STARTUP_STATE = {
    "initialized": False,
    "error": "",
    "checked_at": "",
}


_ASSET_RESPONSE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

_AI_REASONING_EVENT_PREVIEW_LIMIT = 3200
_AI_REASONING_EVENT_CHAR_DELTA = 180
_AI_REASONING_EVENT_FLUSH_INTERVAL_SECONDS = 0.9
_AI_REASONING_OUTPUT_PREVIEW_LIMIT = 2200


def _asset_file_ready(path_value: object) -> bool:
    path = Path(str(path_value or "").strip())
    if not path.exists():
        return False
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _table_asset_variant_is_ready(
    asset: dict | None,
    *,
    require_snapshot: bool = False,
    require_source: bool = False,
) -> bool:
    if not isinstance(asset, dict):
        return False
    if require_snapshot and not _asset_file_ready(asset.get("snapshot_path")):
        return False
    if require_source and not _asset_file_ready(asset.get("source_snapshot_path")):
        return False
    return True


def _resolve_document_table_asset(
    project_id: str,
    document_id: str,
    table_id: str,
    *,
    require_snapshot: bool = False,
    require_source: bool = False,
) -> dict | None:
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")

    preview_asset = None
    preview, _ = load_cached_preview(document)
    preview_assets = (preview or {}).get("table_assets") if isinstance(preview, dict) else []
    for item in preview_assets or []:
        if isinstance(item, dict) and str(item.get("table_id") or "").strip() == table_id:
            preview_asset = item
            break

    db_asset = db.get_document_table_asset(project_id, document_id, table_id)
    if _table_asset_variant_is_ready(
        preview_asset,
        require_snapshot=require_snapshot,
        require_source=require_source,
    ):
        return preview_asset
    if _table_asset_variant_is_ready(
        db_asset,
        require_snapshot=require_snapshot,
        require_source=require_source,
    ):
        return db_asset
    return preview_asset or db_asset


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    customer_name: str = Field(min_length=1, max_length=255)


class AnalyzeRequest(BaseModel):
    document_id: str


class RunAIAnalysisRequest(BaseModel):
    profile_id: str | None = None


class AzureProviderSetupRequest(BaseModel):
    auth_mode: str | None = None
    allow_interactive_login: bool = False


class AliyunProviderSetupRequest(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    chat_model: str | None = None
    embedding_model: str | None = None
    qdrant_url: str | None = None
    qdrant_collection: str | None = None
    qdrant_api_key: str | None = None
    retrieval_backend: str | None = None


class RuntimeAIProviderSetupRequest(BaseModel):
    default_profile_id: str | None = None
    azure: AzureProviderSetupRequest | None = None
    aliyun: AliyunProviderSetupRequest | None = None


class ReviewedRequirementEntry(BaseModel):
    entry_id: str | None = None
    req_code: str | None = None
    entry_kind: str | None = None
    section_code: str | None = None
    section_title: str | None = None
    section_label: str | None = None
    section_key: str | None = None
    section_instance_key: str | None = None
    source_page: int | None = None
    source_page_end: int | None = None
    source_anchor: str | None = None
    requirement_text: str | None = None
    display_text: str | None = None
    raw_text: str | None = None
    requirement_type: str | None = None
    priority: str | None = None
    text_relation: str | None = None
    is_deduped: bool | None = None
    heading_text: str | None = None
    parent_section_code: str | None = None
    parent_section_title: str | None = None
    parent_section_label: str | None = None
    owner_section_code: str | None = None
    owner_section_title: str | None = None
    owner_section_label: str | None = None
    structural_parent_section_code: str | None = None
    structural_parent_section_key: str | None = None
    structural_parent_section_title: str | None = None
    structural_parent_section_label: str | None = None
    merged_from_req_codes: list[str] | None = None
    merged_from_entry_ids: list[str] | None = None
    dedupe_version: str | None = None
    table_refs: list[dict] | None = None
    content_blocks: list[dict] | None = None
    has_table_asset: bool | None = None
    table_render_mode: str | None = None
    complex_table_mode: bool | None = None
    complex_zone_id: str | None = None
    complex_family: str | None = None
    complex_role: str | None = None
    complex_group_label: str | None = None
    complex_split_level: str | None = None
    complex_warnings: list[str] | None = None
    complex_row_policy: str | None = None
    keep: bool = True


class ApplyReviewedExtractionRequest(BaseModel):
    entries: list[ReviewedRequirementEntry]


_COMPLEX_ROW_FIELD_NAMES = (
    "complex_table_mode",
    "complex_zone_id",
    "complex_family",
    "complex_role",
    "complex_group_label",
    "complex_split_level",
    "complex_warnings",
    "complex_row_policy",
)


def _copy_complex_row_fields(row: dict[str, object]) -> dict[str, object]:
    hydrated = hydrate_complex_row_metadata(dict(row))
    return {field: hydrated.get(field) for field in _COMPLEX_ROW_FIELD_NAMES}


class DecisionRequest(BaseModel):
    final_decision: str = Field(pattern="^(ACCEPT|REJECT|DEVIATION_ACCEPT|NEED_CLARIFICATION)$")
    engineer_reason: str = Field(default="", max_length=4000)


class BatchDecisionRequest(BaseModel):
    final_decision: str = Field(pattern="^(ACCEPT|REJECT|DEVIATION_ACCEPT|NEED_CLARIFICATION)$")
    reason_template: str = Field(default="", max_length=4000)
    requirement_type: str | None = Field(default=None, max_length=80)
    section_code_prefix: str | None = Field(default=None, max_length=64)
    keyword: str | None = Field(default=None, max_length=200)
    document_id: str | None = None
    only_without_final: bool = True


class ExportRequest(BaseModel):
    version_label: str | None = Field(default=None, max_length=64)
    compare_export_id: str | None = Field(default=None, max_length=64)
    document_id: str | None = None


class AnalysisLabelOverrideRequest(BaseModel):
    analysis_label: str = Field(pattern="^(REQUIREMENT)$")
    rerun_assessment: bool = True
    reason: str = Field(default="", max_length=4000)


class Phase0ItemUpdate(BaseModel):
    item_id: str
    is_true_requirement: str | None = None
    is_split_correct: str | None = None
    is_type_correct: str | None = None
    gold_requirement_text: str | None = None
    gold_requirement_type: str | None = None
    error_tags: str | None = None
    annotator: str | None = None
    notes: str | None = None


class Phase0ItemBatchUpdateRequest(BaseModel):
    updates: list[Phase0ItemUpdate]


class Phase0PageUpdate(BaseModel):
    sample_id: str
    is_page_readable: str | None = None
    gold_total_requirements: str | None = None
    gold_noise_lines_pct: str | None = None
    annotator: str | None = None
    notes: str | None = None


class Phase0PageBatchUpdateRequest(BaseModel):
    updates: list[Phase0PageUpdate]


class ProgressEventCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=3000)
    category: str = Field(default="manual", max_length=32)
    status: str = Field(default="DONE", max_length=32)
    event_time: str | None = None
    track_id: str = Field(default="", max_length=80)
    branch_name: str = Field(default="", max_length=255)
    event_type: str = Field(default="manual", max_length=32)


class ProgressPlanItem(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1, max_length=200)
    owner: str = Field(default="", max_length=100)
    status: str = Field(default="TODO", max_length=32)
    progress_pct: int = Field(default=0, ge=0, le=100)
    target_date: str = Field(default="", max_length=32)
    notes: str = Field(default="", max_length=1000)
    track_id: str = Field(default="", max_length=80)
    branch_name: str = Field(default="", max_length=255)
    worktree_path: str = Field(default="", max_length=1024)
    scope_summary: str = Field(default="", max_length=1000)
    acceptance_gate: str = Field(default="", max_length=500)
    depends_on: str = Field(default="", max_length=500)
    drift_status: str = Field(default="ON_PLAN", max_length=32)
    last_update_at: str = Field(default="", max_length=64)
    latest_event_summary: str = Field(default="", max_length=1000)


class ProgressPlanUpdateRequest(BaseModel):
    items: list[ProgressPlanItem]


class ProgressCheckpointRequest(BaseModel):
    track_id: str = Field(min_length=1, max_length=80)
    progress_pct: int = Field(ge=0, le=100)
    status: str = Field(default="INFO", max_length=32)
    summary: str = Field(min_length=1, max_length=3000)
    branch_name: str = Field(default="", max_length=255)


app = FastAPI(title="SOR AI Prototype", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


def _runtime_directories(ai_config: Any | None = None) -> dict[str, Path]:
    config = ai_config or load_ai_copilot_config()
    return runtime_directory_map(
        storage_dir=STORAGE_DIR,
        uploads_dir=UPLOADS_DIR,
        knowledge_dir=KNOWLEDGE_DIR,
        exports_dir=EXPORTS_DIR,
        table_snapshot_dir=TABLE_SNAPSHOT_DIR,
        ai_trace_dir=config.trace_dir,
    )


def _record_startup_state(*, initialized: bool, error: str = "") -> None:
    _APP_STARTUP_STATE["initialized"] = initialized
    _APP_STARTUP_STATE["error"] = error
    _APP_STARTUP_STATE["checked_at"] = datetime.now(timezone.utc).isoformat()


def _build_health_response() -> JSONResponse:
    ai_profiles = list_ai_copilot_profiles()
    ai_config = ai_profiles[0] if ai_profiles else load_ai_copilot_config()
    payload = build_readiness_report(
        storage_root=STORAGE_DIR,
        runtime_directories=_runtime_directories(ai_config),
        db_path=db.DB_PATH,
        ai_profiles=ai_profiles,
        startup_initialized=bool(_APP_STARTUP_STATE.get("initialized")),
        startup_error=str(_APP_STARTUP_STATE.get("error") or ""),
        startup_checked_at=str(_APP_STARTUP_STATE.get("checked_at") or ""),
    )
    status_code = 200 if payload["ready"] else 503
    return JSONResponse(status_code=status_code, content=payload)


@app.on_event("startup")
def startup() -> None:
    ai_config = load_ai_copilot_config()
    try:
        ensure_runtime_directories(_runtime_directories(ai_config))
        db.init_db()
    except Exception as exc:
        _record_startup_state(initialized=False, error=str(exc))
        logger.exception("Application startup initialization failed.")
        return
    _record_startup_state(initialized=True)
    logger.info(
        "Application startup initialization completed. storage_root=%s db_path=%s assessment_mode=%s retrieval_backend=%s",
        STORAGE_DIR,
        db.DB_PATH,
        ai_config.assessment_mode,
        ai_config.retrieval_backend,
    )


def _register_ai_analysis_thread(document_id: str, thread: Thread, *, job_id: str) -> None:
    with _AI_ANALYSIS_THREAD_LOCK:
        _AI_ANALYSIS_THREADS[document_id] = {
            "thread": thread,
            "job_id": str(job_id or "").strip(),
            "cancel_requested": False,
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
        return (
            str(runtime.get("job_id") or "").strip() == str(job_id or "").strip()
            and bool(runtime.get("cancel_requested"))
        )


def _request_ai_analysis_cancel(document_id: str, job_id: str) -> bool:
    with _AI_ANALYSIS_THREAD_LOCK:
        runtime = _AI_ANALYSIS_THREADS.get(document_id)
        if not runtime:
            return False
        if str(runtime.get("job_id") or "").strip() != str(job_id or "").strip():
            return False
        runtime["cancel_requested"] = True
        return True


def _clear_ai_analysis_thread(document_id: str, *, thread: Thread | None = None) -> None:
    with _AI_ANALYSIS_THREAD_LOCK:
        current = _AI_ANALYSIS_THREADS.get(document_id)
        if current is None:
            return
        current_thread = current.get("thread")
        if thread is not None and current_thread is not thread:
            return
        _AI_ANALYSIS_THREADS.pop(document_id, None)


@app.get("/api/runtime/ai-provider-setup")
def get_runtime_ai_provider_setup_route() -> dict[str, Any]:
    return _build_runtime_ai_provider_setup_response()


@app.post("/api/runtime/ai-provider-setup")
def update_runtime_ai_provider_setup_route(payload: RuntimeAIProviderSetupRequest) -> dict[str, Any]:
    payload_dict = payload.model_dump(exclude_none=True)
    _validate_runtime_ai_provider_setup_payload(payload_dict)
    apply_runtime_ai_provider_setup(payload_dict)
    return _build_runtime_ai_provider_setup_response()


@app.post("/api/runtime/ai-provider-setup/azure-rbac-probe")
def probe_runtime_azure_rbac_route() -> dict[str, Any]:
    return probe_runtime_azure_rbac()


def _build_runtime_ai_provider_setup_response() -> dict[str, Any]:
    payload = build_runtime_ai_provider_setup()
    runtime_checks = {
        config.profile_id: build_ai_profile_check(config)
        for config in list_ai_copilot_profiles()
    }
    payload["profiles"] = [
        _merge_ai_profile_runtime_state(dict(profile), runtime_checks.get(str(profile.get("id") or "").strip(), {}))
        for profile in payload.get("profiles") or []
    ]
    for profile_id in ("azure", "aliyun"):
        section = payload.get(profile_id)
        if isinstance(section, dict):
            payload[profile_id] = _merge_ai_profile_runtime_state(dict(section), runtime_checks.get(profile_id, {}))
    return payload


def _validate_runtime_ai_provider_setup_payload(payload: dict[str, Any]) -> None:
    aliyun_payload = payload.get("aliyun") or {}
    _validate_ascii_runtime_secret(
        "DashScope API Key",
        aliyun_payload.get("api_key"),
    )
    _validate_ascii_runtime_secret(
        "Qdrant API Key",
        aliyun_payload.get("qdrant_api_key"),
    )


def _validate_ascii_runtime_secret(label: str, value: Any) -> None:
    text = str(value or "").strip()
    if not text:
        return
    if is_ascii_runtime_secret(text):
        return
    raise HTTPException(
        status_code=422,
        detail=f"{label} contains non-ASCII characters. Re-enter the raw key without Chinese punctuation or full-width characters.",
    )


def _merge_ai_profile_runtime_state(
    profile_payload: dict[str, Any],
    runtime_check: dict[str, Any] | None,
) -> dict[str, Any]:
    check = runtime_check or {}
    runtime_missing = [str(item).strip() for item in check.get("missing") or [] if str(item).strip()]
    configured_missing = [str(item).strip() for item in profile_payload.get("missing") or [] if str(item).strip()]
    return {
        **profile_payload,
        "ready": bool(check.get("runnable", profile_payload.get("ready"))),
        "runtime_status": str(check.get("status") or ""),
        "runtime_detail": str(check.get("detail") or ""),
        "runtime_missing": runtime_missing,
        "missing": runtime_missing or configured_missing,
    }


def _attach_knowledge_syncs(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    attached: list[dict[str, Any]] = []
    for document in documents:
        current = dict(document)
        current["retrieval_syncs"] = db.list_knowledge_document_syncs(str(current.get("id") or ""))
        attached.append(current)
    return attached


def _sync_knowledge_document_targets(
    project_id: str,
    document: dict[str, Any],
    *,
    profile_ids: list[str] | None = None,
) -> dict[str, Any]:
    sync = sync_knowledge_document_index(project_id, document, profile_ids=profile_ids)
    for target in (sync.get("targets") or {}).values():
        db.upsert_knowledge_document_sync(
            str(document.get("id") or ""),
            profile_id=str(target.get("profile_id") or "").strip(),
            provider=str(target.get("provider") or "").strip(),
            retrieval_backend=str(target.get("retrieval_backend") or "").strip(),
            status=str(target.get("status") or "PENDING").strip().upper() or "PENDING",
            error_message=str(target.get("error") or ""),
            synced_at=str(target.get("synced_at") or ""),
            chunk_count=int(target.get("chunk_count") or 0),
            payload_path=str(target.get("payload_path") or ""),
        )
    updated = db.update_knowledge_document_retrieval_state(
        str(document.get("id") or ""),
        retrieval_sync_status=str(sync.get("status") or "READY"),
        retrieval_sync_error=str(sync.get("error") or ""),
        retrieval_synced_at=str(sync.get("synced_at") or ""),
    ) or document
    updated["retrieval_syncs"] = db.list_knowledge_document_syncs(str(document.get("id") or ""))
    return updated


def _ensure_profile_knowledge_syncs(
    project_id: str,
    knowledge_docs: list[dict[str, Any]],
    ai_config: Any,
) -> list[dict[str, Any]]:
    refreshed_docs: list[dict[str, Any]] = []
    pending_files: list[str] = []
    for document in _attach_knowledge_syncs(knowledge_docs):
        sync_state = next(
            (
                item
                for item in (document.get("retrieval_syncs") or [])
                if str(item.get("profile_id") or "").strip() == ai_config.profile_id
            ),
            None,
        )
        if not sync_state or str(sync_state.get("status") or "").strip().upper() != "READY":
            document = _sync_knowledge_document_targets(project_id, document, profile_ids=[ai_config.profile_id])
            sync_state = next(
                (
                    item
                    for item in (document.get("retrieval_syncs") or [])
                    if str(item.get("profile_id") or "").strip() == ai_config.profile_id
                ),
                None,
            )
        if sync_state and str(sync_state.get("status") or "").strip().upper() == "READY":
            refreshed_docs.append(document)
            continue
        pending_files.append(str(document.get("file_name") or document.get("id") or "knowledge-document"))
        refreshed_docs.append(document)
    if pending_files:
        names = ", ".join(pending_files)
        raise HTTPException(
            status_code=409,
            detail=f"Knowledge sync for {ai_config.display_name} is incomplete: {names}",
        )
    return refreshed_docs


def _build_ai_profile_catalog(knowledge_docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    profiles = list_ai_copilot_profiles()
    default_profile_id = load_ai_copilot_config().profile_id
    runtime_checks = {
        config.profile_id: build_ai_profile_check(config)
        for config in profiles
    }
    sync_index: dict[tuple[str, str], dict[str, Any]] = {}
    for document in knowledge_docs:
        for sync in document.get("retrieval_syncs") or []:
            sync_index[(str(document.get("id") or ""), str(sync.get("profile_id") or ""))] = sync

    profile_payloads: list[dict[str, Any]] = []
    for config in profiles:
        ready_count = 0
        error_count = 0
        for document in knowledge_docs:
            sync = sync_index.get((str(document.get("id") or ""), config.profile_id))
            state = str((sync or {}).get("status") or "").strip().upper()
            if state == "READY":
                ready_count += 1
            elif state == "ERROR":
                error_count += 1
        knowledge_total = len(knowledge_docs)
        if knowledge_total <= 0:
            sync_status = "NOT_REQUIRED"
        elif ready_count == knowledge_total:
            sync_status = "READY"
        elif error_count == knowledge_total:
            sync_status = "ERROR"
        elif ready_count == 0 and error_count == 0:
            sync_status = "PENDING"
        else:
            sync_status = "PARTIAL"
        runtime_check = runtime_checks.get(config.profile_id, {})
        profile_payloads.append(
            _merge_ai_profile_runtime_state(
                {
                    **serialize_ai_profile(config),
                    "default": config.profile_id == default_profile_id,
                    "missing": get_ai_profile_missing_config(config),
                    "knowledge_sync": {
                        "status": sync_status,
                        "ready_count": ready_count,
                        "total_count": knowledge_total,
                    },
                },
                runtime_check,
            )
        )
    return profile_payloads


def _default_knowledge_sync_profile_ids() -> list[str]:
    return [load_ai_copilot_config().profile_id]


def _has_live_ai_analysis_thread(document_id: str) -> bool:
    thread = _get_ai_analysis_thread(document_id)
    return bool(thread and thread.is_alive())


def _raise_if_ai_analysis_cancel_requested(document_id: str, job_id: str) -> None:
    if _is_ai_analysis_cancel_requested(document_id, job_id):
        raise AIAnalysisCancelledError("AI analysis cancelled by user.")


def _finalize_orphaned_ai_analysis_job(document_id: str) -> dict[str, Any] | None:
    job = db.get_running_ai_analysis_job(document_id)
    if not job:
        return None
    if _has_live_ai_analysis_thread(document_id):
        return job
    timestamp = datetime.now(timezone.utc).isoformat()
    status = str(job.get("status") or "").strip().upper()
    cancelled = status == "CANCEL_REQUESTED"
    updated = db.update_ai_analysis_job(
        str(job.get("id") or ""),
        status="CANCELLED" if cancelled else "FAILED",
        current_stage="cancelled" if cancelled else "failed",
        last_message="AI analysis was stopped." if cancelled else "AI analysis worker stopped unexpectedly.",
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
        message="AI 分析已停止。" if cancelled else "AI analysis worker is not active. Please retry.",
        payload={"reason": "cancelled_by_user" if cancelled else "orphaned_worker"},
    )
    db.update_sor_document_ai_state(document_id, ai_status="STALE", workflow_stage="EXTRACTION_LOCKED")
    logger.warning(
        "Finalized orphaned AI analysis job %s for document %s.",
        str(job.get("id") or ""),
        document_id,
    )
    return updated or job


@app.get("/")
def home_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "home.html")


@app.get("/workspace")
def workspace_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/home")
def home_alias_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "home.html")


@app.get("/phase0-review")
def phase0_review_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "phase0-review.html")


@app.get("/extraction-review")
def extraction_review_page() -> RedirectResponse:
    return RedirectResponse(url="/workspace?stage=review", status_code=307)


@app.get("/project-progress")
def project_progress_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "progress-dashboard.html")


@app.get("/api/health")
@app.get("/api/readiness")
def health() -> JSONResponse:
    return _build_health_response()


@app.post("/api/projects")
def create_project(payload: CreateProjectRequest) -> dict:
    return db.create_project(name=payload.name.strip(), customer_name=payload.customer_name.strip())


@app.get("/api/projects")
def list_projects() -> list[dict]:
    return db.list_projects()


@app.post("/api/projects/{project_id}/sor-documents")
async def upload_sor_document(project_id: str, file: UploadFile = File(...)) -> dict:
    _require_project(project_id)
    suffix = Path(file.filename or "").suffix or ".txt"
    file_name = file.filename or f"sor_{uuid.uuid4().hex}{suffix}"
    target = UPLOADS_DIR / f"{uuid.uuid4().hex}_{_safe_name(file_name)}"
    contents = await file.read()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents)

    extracted_text, _, file_type = parse_document(str(target))
    document = db.add_sor_document(
        project_id=project_id,
        file_name=file_name,
        file_type=file_type,
        file_path=str(target),
        extracted_text=extracted_text,
    )
    request_preview_refresh(document, force=True)
    return document


@app.get("/api/projects/{project_id}/sor-documents")
def list_sor_documents(project_id: str) -> list[dict]:
    _require_project(project_id)
    return db.list_sor_documents(project_id)


@app.get("/api/projects/{project_id}/sor-documents/{document_id}/extraction-preview")
def get_sor_extraction_preview(project_id: str, document_id: str, response: Response) -> dict:
    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    preview, preview_meta = load_cached_preview(document)
    payload = {
        "project_id": project_id,
        "document_id": document_id,
        "file_name": document["file_name"],
        **preview_meta,
    }
    requirements = db.list_requirements(project_id, document_id)
    if preview is not None:
        preview = _attach_table_asset_urls_to_preview(preview, project_id=project_id, document_id=document_id)
        payload.update(preview)
        payload["review_entries"] = _attach_table_asset_urls_to_rows(
            _overlay_review_entries(
            preview.get("complete_requirement_entries"),
            requirements,
            prefer_raw=False,
            ),
            project_id=project_id,
            document_id=document_id,
        )
        payload["review_entries_raw"] = _attach_table_asset_urls_to_rows(
            _overlay_review_entries(
            preview.get("complete_requirement_entries_raw") or preview.get("complete_requirement_entries"),
            requirements,
            prefer_raw=True,
            ),
            project_id=project_id,
            document_id=document_id,
        )
    return payload


@app.get("/api/projects/{project_id}/sor-documents/{document_id}/table-assets/{table_id}")
def get_document_table_asset(project_id: str, document_id: str, table_id: str) -> dict:
    _require_project(project_id)
    asset = _resolve_document_table_asset(project_id, document_id, table_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Table asset not found.")
    return prepare_table_asset_urls(asset, project_id=project_id, document_id=document_id)


@app.get("/api/projects/{project_id}/sor-documents/{document_id}/table-assets/{table_id}/snapshot")
def get_document_table_asset_snapshot(project_id: str, document_id: str, table_id: str) -> FileResponse:
    _require_project(project_id)
    asset = _resolve_document_table_asset(project_id, document_id, table_id, require_snapshot=True)
    if asset is None:
        raise HTTPException(status_code=404, detail="Table asset not found.")
    snapshot_path = Path(str(asset.get("snapshot_path") or "").strip())
    if not snapshot_path.exists():
        raise HTTPException(status_code=404, detail="Table snapshot not found.")
    return FileResponse(
        snapshot_path,
        media_type="image/png",
        filename=f"{table_id}.png",
        headers=_ASSET_RESPONSE_HEADERS,
    )


@app.get("/api/projects/{project_id}/sor-documents/{document_id}/table-assets/{table_id}/source-snapshot")
def get_document_table_asset_source_snapshot(project_id: str, document_id: str, table_id: str) -> FileResponse:
    _require_project(project_id)
    asset = _resolve_document_table_asset(project_id, document_id, table_id, require_source=True)
    if asset is None:
        raise HTTPException(status_code=404, detail="Table asset not found.")
    snapshot_path = Path(str(asset.get("source_snapshot_path") or "").strip())
    if not snapshot_path.exists():
        raise HTTPException(status_code=404, detail="Table source snapshot not found.")
    return FileResponse(
        snapshot_path,
        media_type="image/png",
        filename=f"{table_id}_source.png",
        headers=_ASSET_RESPONSE_HEADERS,
    )


@app.post("/api/projects/{project_id}/sor-documents/{document_id}/refresh-preview")
def refresh_document_preview(project_id: str, document_id: str) -> dict:
    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")
    refresh = request_preview_refresh(document, force=True)
    current_document = db.get_sor_document(document_id) or document
    return {
        "project_id": project_id,
        "document_id": document_id,
        "accepted": bool(refresh.get("accepted")),
        **refresh,
        "document": current_document,
    }


@app.get("/api/projects/{project_id}/sor-documents/{document_id}/workspace")
def get_document_workspace(project_id: str, document_id: str, include_preview: bool = False) -> dict:
    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")
    document = db.backfill_sor_document_runtime(document_id) or document
    requirements = db.list_requirements(project_id, document_id)
    requirements = _enrich_requirements_with_preview_context(requirements, str(document.get("file_path") or ""))
    knowledge_docs = _list_project_knowledge_documents(project_id)
    preview, preview_meta = load_cached_preview(document)
    workspace = _build_document_workspace(
        project=project,
        document=document,
        requirements=requirements,
        knowledge_docs_count=len(knowledge_docs),
        preview=preview,
        preview_meta=preview_meta,
    )
    return workspace if include_preview else _strip_workspace_preview(workspace)


@app.get("/api/projects/{project_id}/sor-documents/{document_id}/file")
def get_sor_document_file(project_id: str, document_id: str) -> FileResponse:
    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")

    path = Path(str(document.get("file_path") or ""))
    if not path.exists():
        raise HTTPException(status_code=404, detail="SOR source file not found.")

    file_name = str(document.get("file_name") or path.name or "sor_source")
    suffix = path.suffix.lower()
    media_type = "application/pdf" if suffix == ".pdf" else "application/octet-stream"
    if media_type == "application/pdf":
        inline_name = quote(file_name)
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Content-Disposition": f"inline; filename*=utf-8''{inline_name}"},
        )
    return FileResponse(path, filename=file_name, media_type=media_type)


def _normalize_knowledge_doc_type(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"REFERENCE", "STANDARD", "SPEC", "REGULATION", "CASE"}:
        return normalized
    return "REFERENCE"


def _normalize_knowledge_doc_status(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"ACTIVE", "DRAFT", "SUPERSEDED", "ARCHIVED"}:
        return normalized
    return "ACTIVE"


def _normalize_optional_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise HTTPException(status_code=422, detail="Knowledge document dates must use YYYY-MM-DD format.")
    return text


def _normalize_knowledge_document_file_name(file_name: str) -> str:
    base_name = Path(str(file_name or "")).name
    collapsed = re.sub(r"\s+", " ", base_name).strip()
    return collapsed.casefold()


def _dedupe_knowledge_documents_by_file_name(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for document in documents or []:
        normalized_name = _normalize_knowledge_document_file_name(str(document.get("file_name") or ""))
        dedupe_key = normalized_name or str(document.get("id") or "")
        if dedupe_key in seen_names:
            continue
        seen_names.add(dedupe_key)
        deduped.append(document)
    return deduped


def _list_project_knowledge_documents(project_id: str) -> list[dict[str, Any]]:
    return _dedupe_knowledge_documents_by_file_name(db.list_knowledge_documents(project_id))


def _find_duplicate_knowledge_document(project_id: str, file_name: str) -> dict[str, Any] | None:
    normalized_name = _normalize_knowledge_document_file_name(file_name)
    if not normalized_name:
        return None
    for document in db.list_knowledge_documents(project_id):
        if _normalize_knowledge_document_file_name(str(document.get("file_name") or "")) == normalized_name:
            return document
    return None


def _list_matching_knowledge_documents(project_id: str, file_name: str) -> list[dict[str, Any]]:
    normalized_name = _normalize_knowledge_document_file_name(file_name)
    if not normalized_name:
        return []
    return [
        document
        for document in db.list_knowledge_documents(project_id)
        if _normalize_knowledge_document_file_name(str(document.get("file_name") or "")) == normalized_name
    ]


def _collect_knowledge_document_cleanup_paths(documents: list[dict[str, Any]]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for document in documents or []:
        raw_paths = [str(document.get("file_path") or "").strip()]
        raw_paths.extend(
            str(sync.get("payload_path") or "").strip()
            for sync in document.get("retrieval_syncs") or []
        )
        fallback_payload = INDEX_PAYLOAD_DIR / f"{str(document.get('id') or '').strip()}.json"
        raw_paths.append(str(fallback_payload))
        for raw_path in raw_paths:
            if not raw_path:
                continue
            normalized_path = str(Path(raw_path))
            if normalized_path in seen:
                continue
            seen.add(normalized_path)
            paths.append(Path(raw_path))
    return paths


def _cleanup_knowledge_document_files(paths: list[Path]) -> tuple[list[str], list[str]]:
    deleted_paths: list[str] = []
    cleanup_errors: list[str] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            path.unlink()
            deleted_paths.append(str(path))
        except OSError as exc:
            logger.warning("Failed to delete knowledge document artifact %s: %s", path, exc)
            cleanup_errors.append(f"{path}: {exc}")
    return deleted_paths, cleanup_errors


@app.post("/api/projects/{project_id}/knowledge-documents")
async def upload_knowledge_document(
    project_id: str,
    file: UploadFile = File(...),
    doc_type: str = Form("REFERENCE"),
    doc_version: str = Form(""),
    status: str = Form("ACTIVE"),
    effective_from: str = Form(""),
    effective_to: str = Form(""),
) -> dict:
    _require_project(project_id)
    default_profile = load_ai_copilot_config()
    if not default_profile.retrieval_runtime_ready:
        missing = ", ".join(get_ai_profile_missing_config(default_profile))
        raise HTTPException(
            status_code=409,
            detail=(
                f"{default_profile.display_name} knowledge sync is not ready yet. "
                f"Complete AI path setup before uploading knowledge documents: {missing}"
            ),
        )
    suffix = Path(file.filename or "").suffix or ".txt"
    file_name = file.filename or f"knowledge_{uuid.uuid4().hex}{suffix}"
    duplicate = _find_duplicate_knowledge_document(project_id, file_name)
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Knowledge document '{file_name}' already exists in this project.",
        )
    target = KNOWLEDGE_DIR / f"{uuid.uuid4().hex}_{_safe_name(file_name)}"
    contents = await file.read()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents)

    extracted_text, _, file_type = parse_document(str(target))
    document = db.add_knowledge_document(
        project_id=project_id,
        file_name=file_name,
        file_type=file_type,
        file_path=str(target),
        extracted_text=extracted_text,
        doc_type=_normalize_knowledge_doc_type(doc_type),
        doc_version=str(doc_version or "").strip(),
        status=_normalize_knowledge_doc_status(status),
        effective_from=_normalize_optional_date(effective_from),
        effective_to=_normalize_optional_date(effective_to),
    )
    document = _sync_knowledge_document_targets(
        project_id,
        document,
        profile_ids=_default_knowledge_sync_profile_ids(),
    )
    project = db.bump_project_knowledge_base_version(project_id)
    return {
        **document,
        "knowledge_base_version": int(project.get("knowledge_base_version") or 0),
        "retrieval_chunk_count": max(
            [int(item.get("chunk_count") or 0) for item in document.get("retrieval_syncs") or []] or [0]
        ),
        "retrieval_payload_path": str(
            next(
                (
                    item.get("payload_path")
                    for item in document.get("retrieval_syncs") or []
                    if str(item.get("payload_path") or "").strip()
                ),
                "",
            )
        ),
    }


@app.get("/api/projects/{project_id}/knowledge-documents")
def list_knowledge_documents(project_id: str) -> list[dict]:
    _require_project(project_id)
    return _attach_knowledge_syncs(_list_project_knowledge_documents(project_id))


@app.delete("/api/projects/{project_id}/knowledge-documents/{document_id}")
def delete_knowledge_document(project_id: str, document_id: str) -> dict:
    _require_project(project_id)
    target = next(
        (
            item
            for item in db.list_knowledge_documents(project_id)
            if str(item.get("id") or "").strip() == document_id
        ),
        None,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Knowledge document not found for this project.")

    related_documents = _list_matching_knowledge_documents(project_id, str(target.get("file_name") or "")) or [target]
    related_documents = _attach_knowledge_syncs(related_documents)
    cleanup_paths = _collect_knowledge_document_cleanup_paths(related_documents)

    removed_document_ids: list[str] = []
    for document in related_documents:
        removed = db.delete_knowledge_document(str(document.get("id") or ""))
        if removed is not None:
            removed_document_ids.append(str(removed.get("id") or ""))

    project = db.bump_project_knowledge_base_version(project_id)
    deleted_paths, cleanup_errors = _cleanup_knowledge_document_files(cleanup_paths)
    return {
        "file_name": str(target.get("file_name") or ""),
        "removed_document_id": document_id,
        "removed_document_ids": removed_document_ids,
        "removed_count": len(removed_document_ids),
        "knowledge_base_version": int(project.get("knowledge_base_version") or 0),
        "deleted_paths": deleted_paths,
        "cleanup_errors": cleanup_errors,
    }


@app.post("/api/projects/{project_id}/knowledge-documents/{document_id}/resync")
def resync_knowledge_document(project_id: str, document_id: str) -> dict:
    _require_project(project_id)
    document = next(
        (
            item
            for item in _list_project_knowledge_documents(project_id)
            if str(item.get("id") or "").strip() == document_id
        ),
        None,
    )
    if not document:
        raise HTTPException(status_code=404, detail="Knowledge document not found for this project.")
    refreshed = _sync_knowledge_document_targets(
        project_id,
        document,
        profile_ids=_default_knowledge_sync_profile_ids(),
    )
    return refreshed


@app.post("/api/projects/{project_id}/sor-documents/{document_id}/lock-extraction")
def lock_document_extraction(project_id: str, document_id: str) -> dict:
    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")
    document = db.backfill_sor_document_runtime(document_id) or document
    if int(document.get("requirement_set_version") or 0) <= 0:
        raise HTTPException(status_code=409, detail="Requirement set is empty. Analyze or review the document first.")
    locked = db.lock_sor_document_extraction(document_id)
    requirements = db.list_requirements(project_id, document_id)
    requirements = _enrich_requirements_with_preview_context(requirements, str(document.get("file_path") or ""))
    knowledge_docs = _list_project_knowledge_documents(project_id)
    preview, preview_meta = load_cached_preview(locked)
    workspace = _build_document_workspace(
        project=project,
        document=locked,
        requirements=requirements,
        knowledge_docs_count=len(knowledge_docs),
        preview=preview,
        preview_meta=preview_meta,
    )
    return _strip_workspace_preview(workspace)


@app.post("/api/projects/{project_id}/sor-documents/{document_id}/run-ai-analysis")
def run_document_ai_analysis(project_id: str, document_id: str) -> dict:
    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")
    document = db.backfill_sor_document_runtime(document_id) or document
    if not str(document.get("extraction_locked_at") or "").strip():
        raise HTTPException(status_code=409, detail="Lock the reviewed requirement set before running AI analysis.")

    requirements = db.list_requirements(project_id, document_id)
    eligible_rows = [row for row in requirements if _is_ai_eligible_requirement(row)]
    if not eligible_rows:
        raise HTTPException(status_code=422, detail="No eligible requirement rows available for AI analysis.")

    knowledge_docs = _list_project_knowledge_documents(project_id)
    ai_config = load_ai_copilot_config()
    _ensure_profile_knowledge_syncs(project_id, knowledge_docs, ai_config)
    db.update_sor_document_ai_state(document_id, ai_status="RUNNING", workflow_stage="AI_ANALYZING")
    analyzed_count = 0
    info_count = 0
    screened_count = 0
    try:
        for row in eligible_rows:
            display_text = str(row.get("display_text") or row.get("requirement_text") or "").strip()
            if not display_text:
                continue
            result = run_ai_copilot_assessment(row, knowledge_docs, config=ai_config)
            assessment = result["assessment"]
            db.upsert_assessment(
                requirement_id=str(row.get("id") or ""),
                profile_id=str(assessment.get("profile_id") or ai_config.profile_id),
                provider=str(assessment.get("provider") or ai_config.provider),
                ai_decision=assessment["ai_decision"],
                ai_reason=assessment["ai_reason"],
                ai_confidence=float(assessment["ai_confidence"]),
                assessment_mode=str(assessment.get("assessment_mode") or ""),
                retrieval_backend=str(assessment.get("retrieval_backend") or ai_config.retrieval_backend),
                analysis_label=str(assessment.get("analysis_label") or "REQUIREMENT"),
                analysis_basis=str(assessment.get("analysis_basis") or ""),
                normalized_requirement=str(assessment.get("normalized_requirement") or ""),
                risk_level=str(assessment.get("risk_level") or ""),
                clarification_question=str(assessment.get("clarification_question") or ""),
                model_name=assessment["model_name"],
                prompt_version=assessment["prompt_version"],
            )
            db.replace_evidences(requirement_id=str(row.get("id") or ""), evidences=result["evidences"])
            _record_assessment_runs(str(row.get("id") or ""), result.get("runs") or [])
            if _is_info_only_requirement(assessment):
                info_count += 1
            elif str(assessment.get("ai_decision") or "").strip():
                analyzed_count += 1
            else:
                screened_count += 1
    except Exception:
        db.update_sor_document_ai_state(document_id, ai_status="STALE", workflow_stage="EXTRACTION_LOCKED")
        raise

    updated_document = db.update_sor_document_ai_state(
        document_id,
        ai_status="READY",
        ai_assessed_at=datetime.now(timezone.utc).isoformat(),
        ai_requirement_set_version=int(document.get("requirement_set_version") or 0),
        ai_knowledge_base_version=int(project.get("knowledge_base_version") or 0),
        workflow_stage="AI_READY",
    )
    requirements = db.list_requirements(project_id, document_id)
    requirements = _enrich_requirements_with_preview_context(requirements, str(document.get("file_path") or ""))
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


@app.post("/api/projects/{project_id}/analyze")
def analyze_document(project_id: str, payload: AnalyzeRequest) -> dict:
    project = _require_project(project_id)
    document = db.get_sor_document(payload.document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")

    preview = build_extraction_preview(
        document["file_path"],
        document_id=document["id"],
        snapshot_root=reset_document_snapshot_root(document["id"]),
    )
    extracted = _build_analysis_rows_from_preview(preview)
    if not extracted:
        raise HTTPException(status_code=422, detail="No atomic requirements extracted from document.")

    inserted = _persist_requirements(
        project_id=project_id,
        sor_document_id=payload.document_id,
        rows=extracted,
        knowledge_docs=[],
        dedupe_version=str(preview.get("summary", {}).get("dedupe_version") or ""),
    )
    document_state = db.increment_sor_document_requirement_set_version(payload.document_id, "EXTRACTED")
    document_state = persist_preview_cache(document_state, preview) or document_state
    knowledge_docs = _list_project_knowledge_documents(project_id)

    return {
        "project_id": project_id,
        "document_id": payload.document_id,
        "requirements_generated": inserted,
        "knowledge_docs_used": 0,
        "knowledge_docs_available": len(knowledge_docs),
        "knowledge_base_version": int(project.get("knowledge_base_version") or 0),
        "workflow_stage": str(document_state.get("workflow_stage") or "EXTRACTED"),
        "requirement_set_version": int(document_state.get("requirement_set_version") or 0),
        "ai_status": str(document_state.get("ai_status") or "NOT_RUN"),
        "ai_triggered": False,
        "dedupe_version": str(preview.get("summary", {}).get("dedupe_version") or ""),
        "dedup_exact_count": int(preview.get("summary", {}).get("dedup_exact_count") or 0),
        "dedup_prefix_count": int(preview.get("summary", {}).get("dedup_prefix_count") or 0),
        "hybrid_entries_count": int(preview.get("summary", {}).get("hybrid_entries_count") or 0),
    }


@app.post("/api/projects/{project_id}/sor-documents/{document_id}/apply-reviewed-extraction")
def apply_reviewed_extraction(project_id: str, document_id: str, payload: ApplyReviewedExtractionRequest) -> dict:
    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")

    reviewed_rows = _build_analysis_rows_from_review_payload(payload.entries)
    if not reviewed_rows:
        raise HTTPException(status_code=422, detail="No reviewed requirement rows to apply.")

    inserted = _persist_requirements(
        project_id=project_id,
        sor_document_id=document_id,
        rows=reviewed_rows,
        knowledge_docs=[],
        dedupe_version=_detect_dedupe_version(reviewed_rows),
    )
    document_state = db.increment_sor_document_requirement_set_version(document_id, "REVIEWING")
    knowledge_docs = _list_project_knowledge_documents(project_id)
    return {
        "project_id": project_id,
        "document_id": document_id,
        "requirements_generated": inserted,
        "knowledge_docs_used": 0,
        "knowledge_docs_available": len(knowledge_docs),
        "knowledge_base_version": int(project.get("knowledge_base_version") or 0),
        "workflow_stage": str(document_state.get("workflow_stage") or "REVIEWING"),
        "requirement_set_version": int(document_state.get("requirement_set_version") or 0),
        "ai_status": str(document_state.get("ai_status") or "NOT_RUN"),
        "ai_triggered": False,
        "dedupe_version": _detect_dedupe_version(reviewed_rows),
    }


@app.post("/api/projects/{project_id}/sor-documents/{document_id}/review-entry")
def save_review_entry(project_id: str, document_id: str, payload: ReviewedRequirementEntry) -> dict:
    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")

    current_requirements = db.list_requirements(project_id, document_id)
    normalized = _normalize_review_entry_payload(payload)
    if is_complex_table_row(normalized):
        normalized = normalize_preserved_entry_table_assets(normalized)
    else:
        normalized, _ = enrich_entry_with_table_assets(
            normalized,
            document_id=document_id,
            snapshot_root=_table_snapshot_root(document_id),
            source_file_path=str(document.get("file_path") or ""),
        )
    matched = _find_requirement_for_review_entry(current_requirements, normalized)
    action = "noop"
    persisted_row: dict | None = matched

    if normalized["keep"]:
        if matched:
            updated_row = db.update_requirement_entry(
                str(matched.get("id") or ""),
                source_page=normalized.get("source_page"),
                source_anchor=str(normalized.get("source_anchor") or "Unknown"),
                requirement_text=str(normalized.get("display_text") or ""),
                requirement_type=str(normalized.get("requirement_type") or "FUNCTIONAL"),
                priority=str(normalized.get("priority") or "MEDIUM"),
                entry_kind=str(normalized.get("entry_kind") or "REQUIREMENT"),
                text_relation=str(normalized.get("text_relation") or "distinct"),
                is_deduped=bool(normalized.get("is_deduped")),
                parent_section_code=str(normalized.get("parent_section_code") or ""),
                parent_section_title=str(normalized.get("parent_section_title") or ""),
                parent_section_label=str(normalized.get("parent_section_label") or ""),
                heading_text=str(normalized.get("heading_text") or ""),
                raw_text=str(normalized.get("raw_text") or ""),
                display_text=str(normalized.get("display_text") or ""),
                merged_from_req_codes=_json_dumps_list(normalized.get("merged_from_req_codes")),
                merged_from_entry_ids=_json_dumps_list(normalized.get("merged_from_entry_ids")),
                dedupe_version=str(normalized.get("dedupe_version") or ""),
                table_refs=_json_dumps(normalized.get("table_refs") or []),
                content_blocks=_json_dumps(normalized.get("content_blocks") or []),
                has_table_asset=bool(normalized.get("has_table_asset")),
                table_render_mode=str(normalized.get("table_render_mode") or ""),
            )
            persisted_row = db.get_requirement(str(updated_row.get("id") or "")) or matched
            action = "updated"
        else:
            used_req_codes = {
                str(item.get("req_code") or "").strip()
                for item in current_requirements
                if str(item.get("req_code") or "").strip()
            }
            base_req_code = str(normalized.get("req_code") or normalized.get("entry_id") or "").strip() or (
                f"REQ{len(current_requirements) + 1:04d}"
            )
            req_code = _ensure_unique_req_code(base_req_code, used_req_codes)
            inserted_row = db.insert_requirement(
                project_id=project_id,
                sor_document_id=document_id,
                req_code=req_code,
                source_page=normalized.get("source_page"),
                source_anchor=str(normalized.get("source_anchor") or "Unknown"),
                requirement_text=str(normalized.get("display_text") or ""),
                requirement_type=str(normalized.get("requirement_type") or "FUNCTIONAL"),
                priority=str(normalized.get("priority") or "MEDIUM"),
                status="DRAFT",
                entry_kind=str(normalized.get("entry_kind") or "REQUIREMENT"),
                text_relation=str(normalized.get("text_relation") or "distinct"),
                is_deduped=bool(normalized.get("is_deduped")),
                parent_section_code=str(normalized.get("parent_section_code") or ""),
                parent_section_title=str(normalized.get("parent_section_title") or ""),
                parent_section_label=str(normalized.get("parent_section_label") or ""),
                heading_text=str(normalized.get("heading_text") or ""),
                raw_text=str(normalized.get("raw_text") or ""),
                display_text=str(normalized.get("display_text") or ""),
                merged_from_req_codes=_json_dumps_list(normalized.get("merged_from_req_codes")),
                merged_from_entry_ids=_json_dumps_list(normalized.get("merged_from_entry_ids")),
                dedupe_version=str(normalized.get("dedupe_version") or ""),
                table_refs=_json_dumps(normalized.get("table_refs") or []),
                content_blocks=_json_dumps(normalized.get("content_blocks") or []),
                has_table_asset=bool(normalized.get("has_table_asset")),
                table_render_mode=str(normalized.get("table_render_mode") or ""),
            )
            persisted_row = db.get_requirement(str(inserted_row.get("id") or "")) or inserted_row
            action = "inserted"
    elif matched:
        db.delete_requirement(str(matched.get("id") or ""))
        persisted_row = matched
        action = "removed"

    if action != "noop":
        document = db.increment_sor_document_requirement_set_version(document_id, "REVIEWING")
        _sync_document_table_assets(project_id, document_id)
    else:
        document = db.backfill_sor_document_runtime(document_id) or document

    requirements = db.list_requirements(project_id, document_id)
    requirements = _enrich_requirements_with_preview_context(requirements, str(document.get("file_path") or ""))
    knowledge_docs = db.list_knowledge_documents(project_id)
    preview, preview_meta = load_cached_preview(document)
    workspace = _build_document_workspace(
        project=project,
        document=document,
        requirements=requirements,
        knowledge_docs_count=len(knowledge_docs),
        preview=preview,
        preview_meta=preview_meta,
    )
    response_workspace = _strip_workspace_preview(workspace)
    saved_entry = prepare_table_ref_urls(
        _build_saved_review_entry(normalized, persisted_row, keep=action != "removed"),
        project_id=project_id,
        document_id=document_id,
    )
    if action != "noop" and not preview_meta.get("preview_cache_hit"):
        request_preview_refresh(document, force=False)
    return {
        **response_workspace,
        "saved_entry": saved_entry,
        "review_save": {
            "action": action,
            "entry_id": normalized.get("entry_id"),
            "req_code": saved_entry.get("req_code"),
        },
    }


@app.get("/api/projects/{project_id}/requirements")
def list_requirements(project_id: str, document_id: str | None = None) -> list[dict]:
    _require_project(project_id)
    document = None
    if document_id:
        document = db.get_sor_document(document_id)
        if not document or document["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="SOR document not found for this project.")
    rows = db.list_requirements(project_id, document_id)
    if document:
        rows = _enrich_requirements_with_preview_context(rows, str(document.get("file_path") or ""))
        rows = _attach_table_asset_urls_to_rows(rows, project_id=project_id, document_id=document_id)
    return _present_requirements(rows)


def _prepare_document_ai_analysis_request(
    project_id: str,
    document_id: str,
    profile_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], Any]:
    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")
    document = db.backfill_sor_document_runtime(document_id) or document
    if not str(document.get("extraction_locked_at") or "").strip():
        raise HTTPException(status_code=409, detail="Lock the reviewed requirement set before running AI analysis.")

    requirements = db.list_requirements(project_id, document_id)
    requirements = _enrich_requirements_with_preview_context(requirements, str(document.get("file_path") or ""))
    preview, _ = load_cached_preview(document)
    presented_requirements, _order_diagnostics = _prepare_requirements_for_display(requirements, preview=preview)
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
    eligible_rows = [row for row in requirements if _is_ai_eligible_requirement(row)]
    if not eligible_rows:
        raise HTTPException(status_code=422, detail="No eligible requirement rows available for AI analysis.")
    ai_config = load_ai_copilot_config(profile_id)
    ensure_ai_profile_runnable(ai_config)
    knowledge_docs = _ensure_profile_knowledge_syncs(
        project_id,
        _list_project_knowledge_documents(project_id),
        ai_config,
    )
    return project, document, requirements, eligible_rows, knowledge_docs, ai_config


def _serialize_ai_analysis_document(document: dict[str, Any] | None) -> dict[str, Any]:
    current = document or {}
    return {
        "id": current.get("id"),
        "workflow_stage": str(current.get("workflow_stage") or "UPLOADED"),
        "ai_status": str(current.get("ai_status") or "NOT_RUN"),
        "ai_assessed_at": str(current.get("ai_assessed_at") or ""),
        "requirement_set_version": int(current.get("requirement_set_version") or 0),
        "ai_requirement_set_version": int(current.get("ai_requirement_set_version") or 0),
        "ai_knowledge_base_version": int(current.get("ai_knowledge_base_version") or 0),
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


def _build_ai_analysis_status_payload(
    project_id: str,
    document_id: str,
    *,
    job: dict[str, Any] | None = None,
    event_limit: int = 40,
) -> dict[str, Any]:
    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")
    latest_job = job or _finalize_orphaned_ai_analysis_job(document_id) or db.get_latest_ai_analysis_job(document_id)
    job_payload = dict(latest_job) if latest_job else None
    if job_payload:
        job_payload["has_live_worker"] = _has_live_ai_analysis_thread(document_id)
    refreshed_document = db.backfill_sor_document_runtime(document_id) or document
    events = db.list_ai_analysis_events(str(job_payload.get("id") or ""), limit=event_limit) if job_payload else []
    knowledge_docs = _attach_knowledge_syncs(_list_project_knowledge_documents(project_id))
    return {
        "job": job_payload,
        "events": events,
        "document": _serialize_ai_analysis_document(refreshed_document),
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
                "legacy_started_emitted": False,
            },
        )
        buffer["content"] = str(buffer.get("content") or "") + delta
        if str(payload.get("mode") or "").strip():
            buffer["mode"] = str(payload.get("mode") or "").strip()
        if str(payload.get("model_name") or "").strip():
            buffer["model_name"] = str(payload.get("model_name") or "").strip()
        self._emit_stream_if_needed(stage=stage, stream_kind=stream_kind, buffer=buffer, force=int(buffer.get("sequence") or 0) == 0)

    def flush(self) -> None:
        self._raise_if_cancelled()
        for (stage, stream_kind), buffer in self._stream_buffers.items():
            self._emit_stream_if_needed(stage=stage, stream_kind=stream_kind, buffer=buffer, force=True)

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

    def _emit_stage_update(self, *, stage: str, mode: str, model_name: str, message: str) -> None:
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
            if last_emit_at and (now - last_emit_at) < _AI_REASONING_EVENT_FLUSH_INTERVAL_SECONDS:
                return
        buffer["chars_emitted"] = total_chars
        buffer["sequence"] = int(buffer.get("sequence") or 0) + 1
        buffer["last_emit_at"] = now
        self._last_stage_key = stage
        preview_limit = _AI_REASONING_OUTPUT_PREVIEW_LIMIT if stream_kind == "output" else _AI_REASONING_EVENT_PREVIEW_LIMIT
        preview = content[-preview_limit:]
        message = self._build_stream_message(stage=stage, stream_kind=stream_kind, total_chars=total_chars)
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
        if stage == "assessment" and stream_kind == "output" and not bool(buffer.get("legacy_started_emitted")):
            buffer["legacy_started_emitted"] = True
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
            return "requirement_screening_reasoning" if stream_kind == "reasoning" else "requirement_screening_output"
        return "requirement_reasoning" if stream_kind == "reasoning" else "requirement_output"

    def _default_stage_message(self, stage: str) -> str:
        if stage == "screening":
            return f"正在筛选 {self.requirement_code} 是否进入需求分析。"
        if stage == "retrieval":
            return f"{self.requirement_code} 筛选完成，正在检索候选证据。"
        if stage == "assessment":
            return f"{self.requirement_code} 候选证据已就绪，正在生成结构化结论。"
        return ""

    def _build_stream_message(self, *, stage: str, stream_kind: str, total_chars: int) -> str:
        if stage == "screening":
            if stream_kind == "reasoning":
                return f"{self.requirement_code} 正在筛选推理（已生成 {total_chars} 字）"
            return f"{self.requirement_code} 正在生成筛选结果（已输出 {total_chars} 字）"
        if stream_kind == "reasoning":
            return f"{self.requirement_code} 正在推理（已生成 {total_chars} 字）"
        return f"{self.requirement_code} 正在生成结构化结论（已输出 {total_chars} 字）"


def _execute_document_ai_analysis(
    project: dict[str, Any],
    document: dict[str, Any],
    eligible_rows: list[dict[str, Any]],
    knowledge_docs: list[dict[str, Any]],
    *,
    ai_config: Any,
    job_id: str | None = None,
) -> dict[str, Any]:
    project_id = str(project.get("id") or "")
    document_id = str(document.get("id") or "")
    total_requirements = len(eligible_rows)
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info(
        "Starting AI analysis job %s for document %s with profile %s (%s), %s requirement(s) and %s knowledge document(s).",
        job_id or "sync",
        document_id,
        ai_config.profile_id,
        ai_config.provider,
        total_requirements,
        len(knowledge_docs),
    )
    db.update_sor_document_ai_state(document_id, ai_status="RUNNING", workflow_stage="AI_ANALYZING")
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
    try:
        for index, row in enumerate(eligible_rows, start=1):
            if job_id:
                _raise_if_ai_analysis_cancel_requested(document_id, job_id)
            display_text = str(row.get("display_text") or row.get("requirement_text") or "").strip()
            if not display_text:
                continue
            requirement_id = str(row.get("id") or "")
            requirement_code = str(row.get("display_req_code") or row.get("req_code") or requirement_id)
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
                    cancel_checker=(lambda: _is_ai_analysis_cancel_requested(document_id, str(job_id or ""))),
                )
                if job_id
                else None
            )
            try:
                result = run_ai_copilot_assessment(
                    row,
                    knowledge_docs,
                    config=ai_config,
                    progress_callback=reasoning_emitter.handle if reasoning_emitter else None,
                )
            finally:
                if reasoning_emitter and not (job_id and _is_ai_analysis_cancel_requested(document_id, job_id)):
                    reasoning_emitter.flush()
            if job_id:
                _raise_if_ai_analysis_cancel_requested(document_id, job_id)
            assessment = result["assessment"]
            db.upsert_assessment(
                requirement_id=requirement_id,
                profile_id=str(assessment.get("profile_id") or ai_config.profile_id),
                provider=str(assessment.get("provider") or ai_config.provider),
                ai_decision=assessment["ai_decision"],
                ai_reason=assessment["ai_reason"],
                ai_confidence=float(assessment["ai_confidence"]),
                assessment_mode=str(assessment.get("assessment_mode") or ""),
                retrieval_backend=str(assessment.get("retrieval_backend") or ai_config.retrieval_backend),
                analysis_label=str(assessment.get("analysis_label") or "REQUIREMENT"),
                analysis_basis=str(assessment.get("analysis_basis") or ""),
                normalized_requirement=str(assessment.get("normalized_requirement") or ""),
                risk_level=str(assessment.get("risk_level") or ""),
                clarification_question=str(assessment.get("clarification_question") or ""),
                model_name=assessment["model_name"],
                prompt_version=assessment["prompt_version"],
            )
            db.replace_evidences(requirement_id=requirement_id, evidences=result["evidences"])
            _record_assessment_runs(requirement_id, result.get("runs") or [])

            analysis_label = str(assessment.get("analysis_label") or "").strip().upper()
            ai_decision = str(assessment.get("ai_decision") or "").strip().upper()
            assessment_mode = str(assessment.get("assessment_mode") or "").strip().lower()
            if _is_info_only_requirement(assessment):
                info_count += 1
                result_label = "INFO"
            elif assessment_mode == "screening_failed":
                screened_count += 1
                result_label = "筛选失败"
            elif assessment_mode == "analysis_failed":
                screened_count += 1
                result_label = "分析失败"
            elif ai_decision:
                analyzed_count += 1
                result_label = ai_decision
            else:
                screened_count += 1
                result_label = "待分析"

            fallback_reasons = [
                str(run.get("fallback_reason") or "").strip()
                for run in (result.get("runs") or [])
                if str(run.get("fallback_reason") or "").strip()
            ]
            validation_statuses = sorted(
                {
                    str(run.get("validation_status") or "").strip()
                    for run in (result.get("runs") or [])
                    if str(run.get("validation_status") or "").strip()
                }
            )
            event_payload = {
                "profile_id": ai_config.profile_id,
                "provider": ai_config.provider,
                "analysis_label": analysis_label,
                "ai_decision": ai_decision,
                "assessment_mode": assessment_mode,
                "evidence_count": len(result.get("evidences") or []),
                "fallback_reasons": fallback_reasons,
                "validation_statuses": validation_statuses,
            }
            if assessment_mode in {"screening_failed", "analysis_failed"}:
                event_message = f"完成 {requirement_code}：{result_label}。"
            else:
                event_message = f"完成 {requirement_code}：{result_label}，证据 {len(result.get('evidences') or [])} 条。"
            if job_id:
                db.update_ai_analysis_job(
                    job_id,
                    completed_requirements=index,
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
    except AIAnalysisCancelledError:
        logger.info(
            "AI analysis execution cancelled for job %s on document %s.",
            job_id or "sync",
            document_id,
        )
        db.update_sor_document_ai_state(document_id, ai_status="STALE", workflow_stage="EXTRACTION_LOCKED")
        if job_id:
            finished_at = datetime.now(timezone.utc).isoformat()
            completion_message = (
                f"AI 分析已停止：已完成 {analyzed_count + info_count + screened_count}/{total_requirements} 条。"
            )
            db.update_ai_analysis_job(
                job_id,
                status="CANCELLED",
                completed_requirements=analyzed_count + info_count + screened_count,
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
                    "completed_requirements": analyzed_count + info_count + screened_count,
                    "total_requirements": total_requirements,
                },
            )
        updated_document = db.backfill_sor_document_runtime(document_id) or db.get_sor_document(document_id) or document
        requirements = db.list_requirements(project_id, document_id)
        requirements = _enrich_requirements_with_preview_context(requirements, str(document.get("file_path") or ""))
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
        db.update_sor_document_ai_state(document_id, ai_status="STALE", workflow_stage="EXTRACTION_LOCKED")
        if job_id:
            finished_at = datetime.now(timezone.utc).isoformat()
            db.update_ai_analysis_job(
                job_id,
                status="FAILED",
                current_stage="failed",
                last_message=f"AI 分析失败：{exc}",
                error_message=str(exc),
                finished_at=finished_at,
                current_requirement_id="",
                current_requirement_code="",
            )
            db.add_ai_analysis_event(
                job_id=job_id,
                sor_document_id=document_id,
                profile_id=ai_config.profile_id,
                provider=ai_config.provider,
                event_type="job_failed",
                stage="failed",
                message=f"AI 分析失败：{exc}",
                payload={"reason": str(exc)},
            )
        raise

    updated_document = db.update_sor_document_ai_state(
        document_id,
        ai_status="READY",
        ai_assessed_at=datetime.now(timezone.utc).isoformat(),
        ai_requirement_set_version=int(document.get("requirement_set_version") or 0),
        ai_knowledge_base_version=int(project.get("knowledge_base_version") or 0),
        workflow_stage="AI_READY",
    )
    requirements = db.list_requirements(project_id, document_id)
    requirements = _enrich_requirements_with_preview_context(requirements, str(document.get("file_path") or ""))
    preview, preview_meta = load_cached_preview(updated_document)
    workspace = _build_document_workspace(
        project=project,
        document=updated_document,
        requirements=requirements,
        knowledge_docs_count=len(knowledge_docs),
        preview=preview,
        preview_meta=preview_meta,
    )
    if job_id:
        completion_message = (
            f"AI 分析完成：判定 {analyzed_count} 条，标记 INFO {info_count} 条，待分析 {screened_count} 条，知识文档 {len(knowledge_docs)} 份。"
        )
        db.update_ai_analysis_job(
            job_id,
            status="COMPLETED",
            completed_requirements=total_requirements,
            analyzed_requirements=analyzed_count,
            info_requirements=info_count,
            screened_requirements=screened_count,
            current_requirement_id="",
            current_requirement_code="",
            current_requirement_index=total_requirements,
            current_stage="completed",
            last_message=completion_message,
            error_message="",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add_ai_analysis_event(
            job_id=job_id,
            sor_document_id=document_id,
            profile_id=ai_config.profile_id,
            provider=ai_config.provider,
            event_type="job_completed",
            stage="completed",
            message=completion_message,
            payload={
                "profile_id": ai_config.profile_id,
                "provider": ai_config.provider,
                "analyzed_requirements": analyzed_count,
                "info_requirements": info_count,
                "screened_requirements": screened_count,
                "knowledge_docs_used": len(knowledge_docs),
            },
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
    }


def _run_document_ai_analysis_worker(job_id: str, project_id: str, document_id: str) -> None:
    try:
        project, document, _requirements, eligible_rows, knowledge_docs, ai_config = _prepare_document_ai_analysis_request(
            project_id,
            document_id,
            profile_id=str((db.get_ai_analysis_job(job_id) or {}).get("profile_id") or ""),
        )
        _execute_document_ai_analysis(
            project,
            document,
            eligible_rows,
            knowledge_docs,
            ai_config=ai_config,
            job_id=job_id,
        )
    except Exception as exc:
        logger.exception(
            "AI analysis worker failed for job %s on document %s.",
            job_id,
            document_id,
        )
        current = db.get_ai_analysis_job(job_id) or {}
        current_status = str(current.get("status") or "").strip().upper()
        if current_status not in {"FAILED", "COMPLETED", "CANCELLED"}:
            finished_at = datetime.now(timezone.utc).isoformat()
            cancelled = current_status == "CANCEL_REQUESTED" or isinstance(exc, AIAnalysisCancelledError)
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
            try:
                db.update_sor_document_ai_state(document_id, ai_status="STALE", workflow_stage="EXTRACTION_LOCKED")
            except Exception:
                pass
    finally:
        _clear_ai_analysis_thread(document_id)


@app.post("/api/projects/{project_id}/sor-documents/{document_id}/run-ai-analysis/start")
def start_document_ai_analysis(
    project_id: str,
    document_id: str,
    payload: RunAIAnalysisRequest | None = None,
) -> dict:
    _finalize_orphaned_ai_analysis_job(document_id)
    requested_profile_id = str((payload.profile_id if payload else "") or "").strip().lower() or None
    project, document, _requirements, eligible_rows, _knowledge_docs, ai_config = _prepare_document_ai_analysis_request(
        project_id,
        document_id,
        requested_profile_id,
    )
    existing = db.get_running_ai_analysis_job(document_id)
    if existing and _has_live_ai_analysis_thread(document_id):
        return _build_ai_analysis_status_payload(project_id, document_id, job=existing)

    queued_message = f"AI 分析已排队，共 {len(eligible_rows)} 条待分析需求。"
    job = db.create_ai_analysis_job(
        project_id=str(project.get("id") or ""),
        sor_document_id=str(document.get("id") or ""),
        profile_id=ai_config.profile_id,
        provider=ai_config.provider,
        assessment_mode=ai_config.assessment_mode,
        retrieval_backend=ai_config.retrieval_backend,
        chat_model=ai_config.chat_model,
        embedding_model=ai_config.embedding_model,
        total_requirements=len(eligible_rows),
        current_stage="queued",
        last_message=queued_message,
    )
    db.add_ai_analysis_event(
        job_id=str(job.get("id") or ""),
        sor_document_id=document_id,
        profile_id=ai_config.profile_id,
        provider=ai_config.provider,
        event_type="job_created",
        stage="queued",
        message=queued_message,
        payload={
            "profile_id": ai_config.profile_id,
            "provider": ai_config.provider,
            "total_requirements": len(eligible_rows),
        },
    )
    db.update_sor_document_ai_state(document_id, ai_status="RUNNING", workflow_stage="AI_ANALYZING")
    thread = Thread(
        target=_run_document_ai_analysis_worker,
        args=(str(job.get("id") or ""), project_id, document_id),
        name=f"ai-analysis-{document_id[:8]}",
        daemon=True,
    )
    _register_ai_analysis_thread(document_id, thread, job_id=str(job.get("id") or ""))
    thread.start()
    logger.info(
        "Queued AI analysis job %s for document %s using profile %s with %s requirement(s).",
        str(job.get("id") or ""),
        document_id,
        ai_config.profile_id,
        len(eligible_rows),
    )
    return _build_ai_analysis_status_payload(project_id, document_id)


@app.post("/api/projects/{project_id}/sor-documents/{document_id}/run-ai-analysis/stop")
def stop_document_ai_analysis(project_id: str, document_id: str) -> dict:
    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="SOR document not found for this project.")
    _finalize_orphaned_ai_analysis_job(document_id)
    job = db.get_running_ai_analysis_job(document_id)
    if not job:
        return _build_ai_analysis_status_payload(project_id, document_id)
    job_id = str(job.get("id") or "")
    status = str(job.get("status") or "").strip().upper()
    if status == "CANCEL_REQUESTED":
        return _build_ai_analysis_status_payload(project_id, document_id, job=job)
    if not _has_live_ai_analysis_thread(document_id):
        finalized = _finalize_orphaned_ai_analysis_job(document_id)
        return _build_ai_analysis_status_payload(project_id, document_id, job=finalized or job)
    _request_ai_analysis_cancel(document_id, job_id)
    message = "已请求停止当前 AI 分析，正在等待当前调用安全中断。"
    updated = db.update_ai_analysis_job(
        job_id,
        status="CANCEL_REQUESTED",
        current_stage="cancelling",
        last_message=message,
        error_message="",
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
    return _build_ai_analysis_status_payload(project_id, document_id, job=updated)


@app.get("/api/projects/{project_id}/sor-documents/{document_id}/ai-analysis-status")
def get_document_ai_analysis_status(project_id: str, document_id: str, event_limit: int = 40) -> dict:
    return _build_ai_analysis_status_payload(project_id, document_id, event_limit=max(10, min(event_limit, 200)))


@app.get("/api/requirements/{requirement_id}/analysis-detail")
def get_requirement_analysis_detail(requirement_id: str) -> dict:
    requirement = db.get_requirement(requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found.")
    presented_requirement = requirement
    project_id = str(requirement.get("project_id") or "").strip()
    document_id = str(requirement.get("sor_document_id") or "").strip()
    if project_id and document_id:
        document = db.get_sor_document(document_id)
        if document and document.get("project_id") == project_id:
            requirements = db.list_requirements(project_id, document_id)
            requirements = _enrich_requirements_with_preview_context(requirements, str(document.get("file_path") or ""))
            preview, _ = load_cached_preview(document)
            presented_requirements, _order_diagnostics = _prepare_requirements_for_display(requirements, preview=preview)
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
    runs = [_serialize_assessment_run_for_ui(item) for item in db.list_assessment_runs(requirement_id)]
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


@app.post("/api/projects/{project_id}/requirements/batch-decision")
def batch_update_decisions(project_id: str, payload: BatchDecisionRequest) -> dict:
    _require_project(project_id)
    if payload.document_id:
        document = db.get_sor_document(payload.document_id)
        if not document or document["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="SOR document not found for this project.")
    rows = db.list_requirements(project_id, payload.document_id)
    section_prefix = str(payload.section_code_prefix or "").strip()
    keyword = str(payload.keyword or "").strip().lower()

    updated = 0
    for row in rows:
        if _is_info_only_requirement(row):
            continue
        if payload.only_without_final and row.get("final_decision"):
            continue
        section_code = _extract_section_code_from_anchor(str(row.get("source_anchor") or ""))
        if section_prefix and not section_code.startswith(section_prefix):
            continue
        text = str(row.get("display_text") or row.get("requirement_text") or "")
        if keyword and keyword not in text.lower():
            continue

        reason = _render_reason_template(
            template=payload.reason_template,
            row=row,
            section_code=section_code,
        )
        db.update_final_decision(
            requirement_id=str(row.get("id")),
            final_decision=payload.final_decision,
            engineer_reason=reason,
        )
        updated += 1

    return {
        "project_id": project_id,
        "updated_count": updated,
        "filters": {
            "document_id": payload.document_id,
            "section_code_prefix": section_prefix,
            "keyword": keyword,
            "only_without_final": payload.only_without_final,
        },
    }


@app.post("/api/requirements/{requirement_id}/decision")
def update_decision(requirement_id: str, payload: DecisionRequest) -> dict:
    requirement = db.get_requirement(requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found.")
    if _is_info_only_requirement(requirement):
        raise HTTPException(status_code=409, detail="INFO rows do not require final decision.")
    assessment = db.update_final_decision(
        requirement_id=requirement_id,
        final_decision=payload.final_decision,
        engineer_reason=payload.engineer_reason.strip(),
    )
    return {"requirement_id": requirement_id, "assessment": assessment}


@app.post("/api/requirements/{requirement_id}/analysis-label")
def override_analysis_label(requirement_id: str, payload: AnalysisLabelOverrideRequest) -> dict:
    requirement = db.get_requirement(requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found.")
    if not _is_ai_eligible_requirement(requirement):
        raise HTTPException(status_code=409, detail="Only requirement-like rows can be reclassified for AI analysis.")

    target_label = str(payload.analysis_label or "").strip().upper()
    if target_label != "REQUIREMENT":
        raise HTTPException(status_code=422, detail="Only REQUIREMENT override is currently supported.")

    reason = str(payload.reason or "").strip() or "人工覆盖：该条目应作为真实需求继续分析。"
    project_id = str(requirement.get("project_id") or "")
    knowledge_docs = _list_project_knowledge_documents(project_id)
    current_assessment = db.get_assessment(requirement_id) or {}
    selected_profile_id = str(current_assessment.get("profile_id") or "").strip() or None
    ai_config = load_ai_copilot_config(selected_profile_id)
    _ensure_profile_knowledge_syncs(project_id, knowledge_docs, ai_config)
    result = run_ai_copilot_assessment(
        requirement,
        knowledge_docs if payload.rerun_assessment else [],
        config=ai_config,
        forced_analysis_label="REQUIREMENT",
        forced_analysis_basis=reason,
    )
    assessment = result["assessment"]
    saved = db.upsert_assessment(
        requirement_id=requirement_id,
        profile_id=str(assessment.get("profile_id") or ai_config.profile_id),
        provider=str(assessment.get("provider") or ai_config.provider),
        ai_decision=assessment["ai_decision"],
        ai_reason=assessment["ai_reason"],
        ai_confidence=float(assessment["ai_confidence"]),
        assessment_mode=str(assessment.get("assessment_mode") or ""),
        retrieval_backend=str(assessment.get("retrieval_backend") or ai_config.retrieval_backend),
        analysis_label=str(assessment.get("analysis_label") or "REQUIREMENT"),
        analysis_basis=str(assessment.get("analysis_basis") or reason),
        normalized_requirement=str(assessment.get("normalized_requirement") or ""),
        risk_level=str(assessment.get("risk_level") or ""),
        clarification_question=str(assessment.get("clarification_question") or ""),
        model_name=assessment["model_name"],
        prompt_version=assessment["prompt_version"],
    )
    db.replace_evidences(requirement_id=requirement_id, evidences=result["evidences"])
    _record_assessment_runs(requirement_id, result.get("runs") or [])
    return {
        "requirement_id": requirement_id,
        "assessment": saved,
        "knowledge_docs_used": len(knowledge_docs),
        "assessment_runs_recorded": len(result.get("runs") or []),
    }


@app.get("/api/requirements/{requirement_id}/review-logs")
def get_review_logs(requirement_id: str) -> list[dict]:
    requirement = db.get_requirement(requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found.")
    return db.list_review_logs(requirement_id)


@app.post("/api/projects/{project_id}/export")
def export_project(project_id: str, payload: ExportRequest | None = None) -> dict:
    project = _require_project(project_id)
    document_id = str(payload.document_id).strip() if payload and payload.document_id else ""
    document: dict | None = None
    if document_id:
        document = db.get_sor_document(document_id)
        if not document or document["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="SOR document not found for this project.")
        _require_locked_extraction_for_export(project_id, document=document)
    requirements = db.list_requirements(project_id, document_id or None)
    if not document_id:
        _require_locked_extraction_for_export(project_id, requirements=requirements)
    if not requirements:
        raise HTTPException(status_code=422, detail="No requirement data to export.")
    if document_id:
        requirements = _enrich_requirements_with_preview_context(requirements, str(document.get("file_path") or ""))
        requirements = _build_document_workspace(
            project=project,
            document=document,
            requirements=requirements,
            knowledge_docs_count=len(_list_project_knowledge_documents(project_id)),
            preview=None,
            preview_meta=get_preview_cache_state(document),
        )["requirements"]
    else:
        requirements = _present_requirements(requirements, preview=None)

    version_label = str(payload.version_label).strip() if payload and payload.version_label else ""
    compare_export_id = str(payload.compare_export_id).strip() if payload and payload.compare_export_id else ""
    compare_requirements: list[dict] | None = None
    compare_version_label = ""
    if compare_export_id:
        compare_job = db.get_export_job(compare_export_id)
        if not compare_job or compare_job.get("project_id") != project_id:
            raise HTTPException(status_code=404, detail="Compare export version not found for this project.")
        compare_requirements = _parse_snapshot_json(compare_job.get("snapshot_json"))
        compare_version_label = str(compare_job.get("version_label") or compare_job.get("file_name") or "")

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    file_name, file_path = export_compliance_matrix(
        project_name=project["name"],
        customer_name=project["customer_name"],
        source_file_name=str(document.get("file_name") or "") if document else "",
        requirements=requirements,
        export_dir=EXPORTS_DIR,
        version_label=version_label,
        compare_requirements=compare_requirements,
        compare_version_label=compare_version_label,
    )
    try:
        job = db.create_export_job(
            project_id=project_id,
            file_name=file_name,
            file_path=file_path,
            version_label=version_label,
            compare_export_id=compare_export_id,
            snapshot_json=_dump_snapshot_json(requirements),
        )
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise HTTPException(status_code=503, detail="Export storage is busy. Please retry.") from exc
        raise
    return {
        "export_id": job["id"],
        "file_name": file_name,
        "download_url": f"/api/exports/{file_name}",
        "version_label": version_label,
        "compare_export_id": compare_export_id,
        "document_id": document_id,
    }


@app.get("/api/exports/{file_name}")
def download_export(file_name: str) -> FileResponse:
    safe = _safe_name(file_name)
    path = EXPORTS_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="Export file not found.")
    return FileResponse(path, filename=safe, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/projects/{project_id}/exports")
def list_project_exports(project_id: str) -> list[dict]:
    _require_project(project_id)
    return db.list_export_jobs(project_id)


@app.get("/api/phase0/review")
def get_phase0_review(top_pages: int = 30, include_done: bool = False, phase0_dir: str | None = None) -> dict:
    try:
        return load_review_dataset(top_pages=top_pages, include_done=include_done, phase0_dir=phase0_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/phase0/items/batch-label")
def batch_update_phase0_items(payload: Phase0ItemBatchUpdateRequest, phase0_dir: str | None = None) -> dict:
    try:
        result = update_item_labels([item.model_dump() for item in payload.updates], phase0_dir=phase0_dir)
        return {"ok": True, **result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/phase0/pages/batch-label")
def batch_update_phase0_pages(payload: Phase0PageBatchUpdateRequest, phase0_dir: str | None = None) -> dict:
    try:
        result = update_page_labels([item.model_dump() for item in payload.updates], phase0_dir=phase0_dir)
        return {"ok": True, **result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/phase0/report/refresh")
def refresh_phase0_report(phase0_dir: str | None = None) -> dict:
    try:
        result = refresh_baseline_report(phase0_dir=phase0_dir)
        return {"ok": True, **result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except subprocess.CalledProcessError as exc:  # type: ignore[name-defined]
        raise HTTPException(
            status_code=500,
            detail=f"Failed to refresh report: {exc.stderr or exc.stdout or exc}",
        ) from exc


@app.get("/api/iterations/rounds")
def get_iteration_rounds(limit: int = 40) -> dict:
    return {"rounds": list_iteration_rounds(limit=limit)}


@app.get("/api/iterations/diff")
def get_iteration_round_diff(round_a: str, round_b: str, limit: int = 160) -> dict:
    if not round_a.strip() or not round_b.strip():
        raise HTTPException(status_code=422, detail="round_a and round_b are required.")
    return build_round_diff(round_a=round_a.strip(), round_b=round_b.strip(), limit=max(20, min(limit, 800)))


@app.get("/api/iterations/error-closure")
def get_iteration_error_closure(round_id: str, top_n: int = 20) -> dict:
    if not round_id.strip():
        raise HTTPException(status_code=422, detail="round_id is required.")
    return build_error_closure(round_id=round_id.strip(), top_n=max(5, min(top_n, 80)))


@app.get("/api/iterations/high-risk-queue")
def get_iteration_high_risk_queue(round_id: str, limit: int = 80) -> dict:
    if not round_id.strip():
        raise HTTPException(status_code=422, detail="round_id is required.")
    return load_high_risk_queue(round_id=round_id.strip(), limit=max(10, min(limit, 500)))


@app.get("/api/progress/snapshot")
def get_progress_snapshot(limit_rounds: int = 60, limit_events: int = 200) -> dict:
    return load_progress_snapshot(limit_rounds=limit_rounds, limit_events=limit_events)


@app.get("/api/progress/branches")
def get_progress_branches() -> dict:
    return {"branches": load_branch_snapshots()}


@app.get("/api/progress/gates")
def get_progress_gates() -> dict:
    return {"gates": load_gate_statuses()}


@app.post("/api/progress/events")
def create_progress_event(payload: ProgressEventCreateRequest) -> dict:
    try:
        event = add_progress_event(
            title=payload.title,
            detail=payload.detail,
            category=payload.category,
            status=payload.status,
            event_time=payload.event_time,
            track_id=payload.track_id,
            branch_name=payload.branch_name,
            event_type=payload.event_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "event": event}


@app.post("/api/progress/plan")
def update_progress_plan(payload: ProgressPlanUpdateRequest) -> dict:
    result = replace_plan_items([item.model_dump() for item in payload.items])
    return {"ok": True, **result}


@app.post("/api/progress/checkpoint")
def create_progress_checkpoint(payload: ProgressCheckpointRequest) -> dict:
    try:
        result = update_progress_checkpoint(
            track_id=payload.track_id,
            progress_pct=payload.progress_pct,
            status=payload.status,
            summary=payload.summary,
            branch_name=payload.branch_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


def _require_project(project_id: str) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


def _safe_name(file_name: str) -> str:
    return "".join(ch for ch in file_name if ch.isalnum() or ch in {".", "_", "-"}).strip() or "file"


def _extract_section_code_from_anchor(anchor: str) -> str:
    import re

    match = re.search(r"Section\s+([0-9]+(?:\.[0-9]+){0,8})", anchor or "", re.I)
    return match.group(1) if match else ""


def _render_reason_template(template: str, row: dict, section_code: str) -> str:
    text = str(template or "").strip()
    if not text:
        return str(row.get("engineer_reason") or "").strip()
    excerpt = str(row.get("display_text") or row.get("requirement_text") or "").strip().replace("\n", " ")
    excerpt = excerpt[:120]
    replacements = {
        "{req_code}": str(row.get("req_code") or ""),
        "{section_code}": section_code,
        "{requirement_type}": str(row.get("requirement_type") or ""),
        "{priority}": str(row.get("priority") or ""),
        "{excerpt}": excerpt,
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _dump_snapshot_json(requirements: list[dict]) -> str:
    snapshot: list[dict] = []
    for row in requirements:
        snapshot.append(
            {
                "id": row.get("id"),
                "req_code": row.get("req_code"),
                "display_req_code": row.get("display_req_code"),
                "source_page": row.get("source_page"),
                "source_anchor": row.get("source_anchor"),
                "requirement_text": row.get("requirement_text"),
                "display_text": row.get("display_text"),
                "raw_text": row.get("raw_text"),
                "heading_text": row.get("heading_text"),
                "parent_section_code": row.get("parent_section_code"),
                "parent_section_title": row.get("parent_section_title"),
                "parent_section_label": row.get("parent_section_label"),
                "entry_kind": row.get("entry_kind"),
                "text_relation": row.get("text_relation"),
                "is_deduped": row.get("is_deduped"),
                "merged_from_req_codes": row.get("merged_from_req_codes"),
                "merged_from_entry_ids": row.get("merged_from_entry_ids"),
                "dedupe_version": row.get("dedupe_version"),
                "table_refs": row.get("table_refs", []),
                "content_blocks": row.get("content_blocks", []),
                "has_table_asset": row.get("has_table_asset"),
                "table_render_mode": row.get("table_render_mode"),
                "complex_table_mode": row.get("complex_table_mode"),
                "complex_zone_id": row.get("complex_zone_id"),
                "complex_family": row.get("complex_family"),
                "complex_role": row.get("complex_role"),
                "complex_group_label": row.get("complex_group_label"),
                "complex_split_level": row.get("complex_split_level"),
                "complex_warnings": row.get("complex_warnings", []),
                "complex_row_policy": row.get("complex_row_policy"),
                "requirement_type": row.get("requirement_type"),
                "priority": row.get("priority"),
                "ai_decision": row.get("ai_decision"),
                "ai_reason": row.get("ai_reason"),
                "ai_confidence": row.get("ai_confidence"),
                "assessment_mode": row.get("assessment_mode"),
                "analysis_label": row.get("analysis_label"),
                "analysis_basis": row.get("analysis_basis"),
                "normalized_requirement": row.get("normalized_requirement"),
                "risk_level": row.get("risk_level"),
                "clarification_question": row.get("clarification_question"),
                "final_decision": row.get("final_decision"),
                "engineer_reason": row.get("engineer_reason"),
                "status": row.get("status"),
                "evidences": row.get("evidences", []),
            }
        )
    return json.dumps(snapshot, ensure_ascii=False)


def _parse_snapshot_json(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _build_analysis_rows_from_preview(preview: dict) -> list[dict]:
    rows = preview.get("complete_requirement_entries") if isinstance(preview, dict) else []
    if not isinstance(rows, list):
        return []
    normalized: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if is_complex_review_only_row(row):
            continue
        entry_kind = str(row.get("entry_kind") or "REQUIREMENT").strip().upper()
        if entry_kind not in {"REQUIREMENT", "HYBRID"}:
            continue
        text = str(row.get("display_text") or row.get("requirement_text") or "").strip()
        if not text:
            continue
        base = {
            **row,
            "entry_kind": entry_kind,
            "section_code": str(row.get("section_code") or "").strip(),
            "section_title": str(row.get("section_title") or "").strip(),
            "section_label": str(row.get("section_label") or "").strip(),
            "section_key": str(row.get("section_key") or "").strip(),
            "section_instance_key": str(row.get("section_instance_key") or "").strip(),
            "display_text": text,
            "raw_text": str(row.get("raw_text") or row.get("requirement_text") or text).strip(),
            "text_relation": str(row.get("text_relation") or "distinct").strip() or "distinct",
            "is_deduped": bool(row.get("is_deduped")),
            "parent_section_code": str(row.get("parent_section_code") or "").strip(),
            "parent_section_title": str(row.get("parent_section_title") or "").strip(),
            "parent_section_label": str(row.get("parent_section_label") or "").strip(),
            "owner_section_code": str(row.get("owner_section_code") or "").strip(),
            "owner_section_title": str(row.get("owner_section_title") or "").strip(),
            "owner_section_label": str(row.get("owner_section_label") or "").strip(),
            "structural_parent_section_code": str(row.get("structural_parent_section_code") or "").strip(),
            "structural_parent_section_key": str(row.get("structural_parent_section_key") or "").strip(),
            "structural_parent_section_title": str(row.get("structural_parent_section_title") or "").strip(),
            "structural_parent_section_label": str(row.get("structural_parent_section_label") or "").strip(),
            "merged_from_req_codes": row.get("merged_from_req_codes") or [],
            "merged_from_entry_ids": row.get("merged_from_entry_ids") or [],
            "table_refs": row.get("table_refs") or [],
            "content_blocks": row.get("content_blocks") or [],
            "has_table_asset": bool(row.get("has_table_asset")),
            "table_render_mode": str(row.get("table_render_mode") or "").strip(),
            **_copy_complex_row_fields(row),
        }
        normalized.append(hydrate_complex_row_metadata(_refresh_enriched_entry_text_fields(base)))
    return normalized


def _build_analysis_rows_from_review_payload(entries: list[ReviewedRequirementEntry]) -> list[dict]:
    normalized: list[dict] = []
    for item in entries:
        row = item.model_dump()
        if not bool(row.get("keep", True)):
            continue
        if is_complex_review_only_row(row):
            continue
        entry_kind = str(row.get("entry_kind") or "REQUIREMENT").strip().upper()
        if entry_kind not in {"REQUIREMENT", "HYBRID", "HEADING"}:
            continue
        display_text = str(row.get("display_text") or row.get("requirement_text") or "").strip()
        raw_text = str(row.get("raw_text") or row.get("requirement_text") or display_text).strip()
        preferred_text = raw_text or display_text
        if not preferred_text:
            continue
        normalized.append(
            _refresh_enriched_entry_text_fields(
                {
                "entry_id": str(row.get("entry_id") or row.get("req_code") or ""),
                "req_code": str(row.get("req_code") or "").strip(),
                "entry_kind": entry_kind,
                "section_code": str(row.get("section_code") or "").strip(),
                "section_title": str(row.get("section_title") or "").strip(),
                "section_label": str(row.get("section_label") or "").strip(),
                "section_key": str(row.get("section_key") or "").strip(),
                "section_instance_key": str(row.get("section_instance_key") or "").strip(),
                "source_page": row.get("source_page"),
                "source_page_end": row.get("source_page_end"),
                "source_anchor": str(row.get("source_anchor") or "Unknown"),
                "display_text": preferred_text,
                "raw_text": preferred_text,
                "requirement_type": str(row.get("requirement_type") or "FUNCTIONAL").strip() or "FUNCTIONAL",
                "priority": str(row.get("priority") or "MEDIUM").strip() or "MEDIUM",
                "text_relation": str(row.get("text_relation") or "distinct").strip() or "distinct",
                "is_deduped": bool(row.get("is_deduped")),
                "heading_text": str(row.get("heading_text") or "").strip(),
                "parent_section_code": str(row.get("parent_section_code") or "").strip(),
                "parent_section_title": str(row.get("parent_section_title") or "").strip(),
                "parent_section_label": str(row.get("parent_section_label") or "").strip(),
                "owner_section_code": str(row.get("owner_section_code") or "").strip(),
                "owner_section_title": str(row.get("owner_section_title") or "").strip(),
                "owner_section_label": str(row.get("owner_section_label") or "").strip(),
                "structural_parent_section_code": str(row.get("structural_parent_section_code") or "").strip(),
                "structural_parent_section_key": str(row.get("structural_parent_section_key") or "").strip(),
                "structural_parent_section_title": str(row.get("structural_parent_section_title") or "").strip(),
                "structural_parent_section_label": str(row.get("structural_parent_section_label") or "").strip(),
                "merged_from_req_codes": row.get("merged_from_req_codes") or [],
                "merged_from_entry_ids": row.get("merged_from_entry_ids") or [],
                "table_refs": row.get("table_refs") or [],
                "content_blocks": row.get("content_blocks") or [],
                "has_table_asset": bool(row.get("has_table_asset")),
                "table_render_mode": str(row.get("table_render_mode") or "").strip(),
                "dedupe_version": str(row.get("dedupe_version") or "heading_requirement_v1"),
                **_copy_complex_row_fields(row),
                }
            )
        )
    return normalized


def _persist_requirements(
    *,
    project_id: str,
    sor_document_id: str,
    rows: list[dict],
    knowledge_docs: list[dict],
    dedupe_version: str = "",
) -> int:
    materialized_rows, table_assets = _materialize_document_table_assets(rows, sor_document_id=sor_document_id)
    db.clear_requirements_for_document(sor_document_id)
    used_req_codes: set[str] = set()
    inserted = 0
    for index, item in enumerate(materialized_rows, start=1):
        base_req_code = str(item.get("req_code") or "").strip() or f"REQ{index:04d}"
        req_code = _ensure_unique_req_code(base_req_code, used_req_codes)
        used_req_codes.add(req_code)
        display_text = str(item.get("display_text") or item.get("requirement_text") or "").strip()
        raw_text = str(item.get("raw_text") or display_text).strip()
        heading_text = str(item.get("heading_text") or "").strip()
        merged_req_codes = _json_dumps_list(item.get("merged_from_req_codes"))
        merged_entry_ids = _json_dumps_list(item.get("merged_from_entry_ids"))
        table_refs = _json_dumps(item.get("table_refs") or [])
        content_blocks = _json_dumps(item.get("content_blocks") or [])
        req = db.insert_requirement(
            project_id=project_id,
            sor_document_id=sor_document_id,
            req_code=req_code,
            source_page=item.get("source_page"),
            source_anchor=item.get("source_anchor") or "Unknown",
            requirement_text=display_text,
            requirement_type=str(item.get("requirement_type") or "FUNCTIONAL").strip() or "FUNCTIONAL",
            priority=str(item.get("priority") or "MEDIUM").strip() or "MEDIUM",
            status="DRAFT",
            entry_kind=str(item.get("entry_kind") or "REQUIREMENT").strip().upper() or "REQUIREMENT",
            text_relation=str(item.get("text_relation") or "distinct").strip() or "distinct",
            is_deduped=bool(item.get("is_deduped")),
            parent_section_code=str(item.get("parent_section_code") or "").strip(),
            parent_section_title=str(item.get("parent_section_title") or "").strip(),
            parent_section_label=str(item.get("parent_section_label") or "").strip(),
            heading_text=heading_text,
            raw_text=raw_text,
            display_text=display_text,
            merged_from_req_codes=merged_req_codes,
            merged_from_entry_ids=merged_entry_ids,
            dedupe_version=str(item.get("dedupe_version") or dedupe_version or ""),
            table_refs=table_refs,
            content_blocks=content_blocks,
            has_table_asset=bool(item.get("has_table_asset")),
            table_render_mode=str(item.get("table_render_mode") or ""),
        )
        if knowledge_docs:
            ai_config = load_ai_copilot_config()
            _ensure_profile_knowledge_syncs(project_id, knowledge_docs, ai_config)
            assessment_input = {
                **req,
                "display_text": display_text,
                "requirement_text": display_text,
                "requirement_type": str(item.get("requirement_type") or "FUNCTIONAL").strip() or "FUNCTIONAL",
                "parent_section_code": str(item.get("parent_section_code") or "").strip(),
                "parent_section_title": str(item.get("parent_section_title") or "").strip(),
                "parent_section_label": str(item.get("parent_section_label") or "").strip(),
                "heading_text": heading_text,
                "content_blocks": item.get("content_blocks") or [],
                "table_refs": item.get("table_refs") or [],
            }
            result = run_ai_copilot_assessment(assessment_input, knowledge_docs, config=ai_config)
            assessment = result["assessment"]
            db.upsert_assessment(
                requirement_id=req["id"],
                profile_id=str(assessment.get("profile_id") or ai_config.profile_id),
                provider=str(assessment.get("provider") or ai_config.provider),
                ai_decision=assessment["ai_decision"],
                ai_reason=assessment["ai_reason"],
                ai_confidence=float(assessment["ai_confidence"]),
                assessment_mode=str(assessment.get("assessment_mode") or ""),
                retrieval_backend=str(assessment.get("retrieval_backend") or ai_config.retrieval_backend),
                analysis_label=str(assessment.get("analysis_label") or "REQUIREMENT"),
                analysis_basis=str(assessment.get("analysis_basis") or ""),
                normalized_requirement=str(assessment.get("normalized_requirement") or ""),
                risk_level=str(assessment.get("risk_level") or ""),
                clarification_question=str(assessment.get("clarification_question") or ""),
                model_name=assessment["model_name"],
                prompt_version=assessment["prompt_version"],
            )
            db.replace_evidences(requirement_id=req["id"], evidences=result["evidences"])
            _record_assessment_runs(req["id"], result.get("runs") or [])
        inserted += 1
    db.replace_document_table_assets(project_id, sor_document_id, table_assets)
    return inserted


def _detect_dedupe_version(rows: list[dict]) -> str:
    for row in rows:
        token = str(row.get("dedupe_version") or "").strip()
        if token:
            return token
    return ""


def _ensure_unique_req_code(base: str, used: set[str]) -> str:
    token = str(base or "").strip() or "REQ0000"
    if token not in used:
        return token
    suffix = 2
    while f"{token}-D{suffix}" in used:
        suffix += 1
    return f"{token}-D{suffix}"


def _json_dumps_list(value: object) -> str:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    elif value is None:
        items = []
    else:
        text = str(value).strip()
        items = [text] if text else []
    return json.dumps(items, ensure_ascii=False)


def _json_dumps(value: object) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def _table_snapshot_root(document_id: str) -> Path:
    token = str(document_id or "").strip() or "preview"
    path = TABLE_SNAPSHOT_DIR / "persisted" / token
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reset_table_snapshot_root(document_id: str) -> Path:
    path = _table_snapshot_root(document_id)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _materialize_document_table_assets(
    rows: list[dict],
    *,
    sor_document_id: str,
) -> tuple[list[dict], list[dict]]:
    source_file_path = ""
    copied_rows = [dict(item) for item in rows]
    document = db.get_sor_document(sor_document_id)
    if document:
        source_file_path = str(document.get("file_path") or "").strip()
        preview, _ = load_cached_preview(document)
        preview_index = _build_requirement_row_index(_build_analysis_rows_from_preview(preview or {}))
        for row in copied_rows:
            preview_row = _find_indexed_requirement_row(row, preview_index, allow_weak_identifier=True)
            if not preview_row:
                continue
            if _coerce_positive_int(row.get("source_page")) <= 0 and _coerce_positive_int(preview_row.get("source_page")) > 0:
                row["source_page"] = preview_row.get("source_page")
            if _coerce_positive_int(row.get("source_page_end")) <= 0:
                preview_end = _coerce_positive_int(preview_row.get("source_page_end"))
                preview_start = _coerce_positive_int(preview_row.get("source_page"))
                if preview_end > 0 or preview_start > 0:
                    row["source_page_end"] = preview_end or preview_start
    return enrich_entries_with_table_assets(
        copied_rows,
        document_id=sor_document_id,
        snapshot_root=_table_snapshot_root(sor_document_id),
        source_file_path=source_file_path,
    )


def _sync_document_table_assets(project_id: str, sor_document_id: str) -> None:
    _reset_table_snapshot_root(sor_document_id)
    current_rows = db.list_requirements(project_id, sor_document_id)
    materialized_rows, table_assets = _materialize_document_table_assets(current_rows, sor_document_id=sor_document_id)
    for row in materialized_rows:
        requirement_id = str(row.get("id") or "").strip()
        if not requirement_id:
            continue
        db.update_requirement_entry(
            requirement_id,
            source_page=row.get("source_page"),
            source_anchor=str(row.get("source_anchor") or "Unknown"),
            requirement_text=str(row.get("requirement_text") or row.get("display_text") or ""),
            requirement_type=str(row.get("requirement_type") or "FUNCTIONAL"),
            priority=str(row.get("priority") or "MEDIUM"),
            entry_kind=str(row.get("entry_kind") or "REQUIREMENT"),
            text_relation=str(row.get("text_relation") or "distinct"),
            is_deduped=bool(row.get("is_deduped")),
            parent_section_code=str(row.get("parent_section_code") or ""),
            parent_section_title=str(row.get("parent_section_title") or ""),
            parent_section_label=str(row.get("parent_section_label") or ""),
            heading_text=str(row.get("heading_text") or ""),
            raw_text=str(row.get("raw_text") or ""),
            display_text=str(row.get("display_text") or row.get("requirement_text") or ""),
            merged_from_req_codes=_json_dumps_list(row.get("merged_from_req_codes")),
            merged_from_entry_ids=_json_dumps_list(row.get("merged_from_entry_ids")),
            dedupe_version=str(row.get("dedupe_version") or ""),
            table_refs=_json_dumps(row.get("table_refs") or []),
            content_blocks=_json_dumps(row.get("content_blocks") or []),
            has_table_asset=bool(row.get("has_table_asset")),
            table_render_mode=str(row.get("table_render_mode") or ""),
        )
    db.replace_document_table_assets(project_id, sor_document_id, table_assets)


def _enrich_requirements_with_preview_context(rows: list[dict], file_path: str) -> list[dict]:
    if not rows or not file_path:
        return rows
    if all(
        str(row.get("parent_section_label") or "").strip()
        and str(row.get("owner_section_label") or "").strip()
        and (
            str(row.get("section_instance_key") or "").strip()
            or str(row.get("section_key") or "").strip()
            or str(row.get("section_code") or "").strip()
        )
        for row in rows
    ):
        return rows
    document = None
    for row in rows:
        document_id = str(row.get("sor_document_id") or "").strip()
        if document_id:
            document = db.get_sor_document(document_id)
            if document:
                break
    if document is None:
        return rows
    preview, _ = load_cached_preview(document)
    if preview is None:
        return rows
    preview_index = _build_requirement_row_index(_build_analysis_rows_from_preview(preview))
    enriched: list[dict] = []
    pending_updates: list[dict[str, str]] = []
    for row in rows:
        meta = _find_indexed_requirement_row(row, preview_index, allow_weak_identifier=True)
        if not meta:
            enriched.append(row)
            continue
        copied = dict(row)
        changed = False
        for key in ("parent_section_code", "parent_section_title", "parent_section_label"):
            if not str(copied.get(key) or "").strip():
                value = str(meta.get(key) or "").strip()
                if value:
                    copied[key] = value
                    changed = True
        for key in (
            "section_code",
            "section_title",
            "section_label",
            "section_key",
            "section_instance_key",
            "owner_section_code",
            "owner_section_title",
            "owner_section_label",
            "structural_parent_section_code",
            "structural_parent_section_key",
            "structural_parent_section_title",
            "structural_parent_section_label",
        ):
            if not str(copied.get(key) or "").strip():
                value = str(meta.get(key) or "").strip()
                if value:
                    copied[key] = value
                    changed = True
        if not copied.get("table_refs") and meta.get("table_refs"):
            copied["table_refs"] = meta.get("table_refs") or []
            changed = True
        if not copied.get("content_blocks") and meta.get("content_blocks"):
            copied["content_blocks"] = meta.get("content_blocks") or []
            changed = True
        if not bool(copied.get("has_table_asset")) and bool(meta.get("has_table_asset")):
            copied["has_table_asset"] = True
            changed = True
        if not str(copied.get("table_render_mode") or "").strip() and str(meta.get("table_render_mode") or "").strip():
            copied["table_render_mode"] = str(meta.get("table_render_mode") or "").strip()
            changed = True
        if not bool(copied.get("complex_table_mode")) and bool(meta.get("complex_table_mode")):
            copied.update(_copy_complex_row_fields(meta))
            changed = True
        if copied.get("table_refs") or copied.get("content_blocks"):
            refreshed = hydrate_complex_row_metadata(normalize_preserved_entry_table_assets(copied))
            refreshed = _refresh_enriched_entry_text_fields(refreshed)
            if (
                str(refreshed.get("display_text") or "").strip() != str(copied.get("display_text") or "").strip()
                or str(refreshed.get("raw_text") or "").strip() != str(copied.get("raw_text") or "").strip()
            ):
                copied = refreshed
                changed = True
        if changed and str(copied.get("id") or "").strip():
            pending_updates.append(
                {
                    "id": str(copied.get("id") or "").strip(),
                    "parent_section_code": str(copied.get("parent_section_code") or "").strip(),
                    "parent_section_title": str(copied.get("parent_section_title") or "").strip(),
                    "parent_section_label": str(copied.get("parent_section_label") or "").strip(),
                }
            )
        enriched.append(copied)
    if pending_updates:
        db.update_requirement_parent_sections(pending_updates)
    return enriched


def _load_document_preview(document: dict | None) -> dict | None:
    preview, _ = load_cached_preview(document)
    return preview


def _attach_table_asset_urls_to_rows(rows: list[dict], *, project_id: str, document_id: str) -> list[dict]:
    return [
        prepare_table_ref_urls(row, project_id=project_id, document_id=document_id)
        for row in rows or []
        if isinstance(row, dict)
    ]


def _attach_table_asset_urls_to_preview(preview: dict | None, *, project_id: str, document_id: str) -> dict | None:
    if not isinstance(preview, dict):
        return preview
    copied = dict(preview)
    copied["complete_requirement_entries"] = _attach_table_asset_urls_to_rows(
        copied.get("complete_requirement_entries") or [],
        project_id=project_id,
        document_id=document_id,
    )
    copied["complete_requirement_entries_raw"] = _attach_table_asset_urls_to_rows(
        copied.get("complete_requirement_entries_raw") or [],
        project_id=project_id,
        document_id=document_id,
    )
    assets = copied.get("table_assets")
    if not isinstance(assets, list) or not assets:
        assets = db.list_document_table_assets(project_id, document_id)
    copied["table_assets"] = [
        prepare_table_asset_urls(asset, project_id=project_id, document_id=document_id)
        for asset in assets
        if isinstance(asset, dict)
    ]
    return copied


def _strip_workspace_preview(workspace: dict[str, object]) -> dict[str, object]:
    copied = dict(workspace)
    copied.pop("preview", None)
    copied.pop("review_entries", None)
    copied.pop("review_entries_raw", None)
    copied.pop("preview_generated_at", None)
    return copied


def _build_document_workspace(
    *,
    project: dict,
    document: dict,
    requirements: list[dict],
    knowledge_docs_count: int = 0,
    preview: dict | None = None,
    preview_meta: dict | None = None,
) -> dict:
    ai_runtime = load_ai_copilot_config()
    project_id = str(project.get("id") or "")
    document_id = str(document.get("id") or "")
    knowledge_docs = _attach_knowledge_syncs(_list_project_knowledge_documents(project_id))
    ai_profiles = _build_ai_profile_catalog(knowledge_docs)
    preview = _attach_table_asset_urls_to_preview(preview, project_id=project_id, document_id=document_id)
    requirement_set_version = _coerce_int(document.get("requirement_set_version"))
    knowledge_base_version = _coerce_int(project.get("knowledge_base_version"))
    ai_requirement_set_version = _coerce_int(document.get("ai_requirement_set_version"))
    ai_knowledge_base_version = _coerce_int(document.get("ai_knowledge_base_version"))
    ai_status = str(document.get("ai_status") or "NOT_RUN").strip().upper() or "NOT_RUN"
    preview_meta = preview_meta or get_preview_cache_state(document)
    ai_stale = ai_status == "STALE" or (
        ai_status == "READY"
        and (
            ai_requirement_set_version != requirement_set_version
            or ai_knowledge_base_version != knowledge_base_version
        )
    )
    if ai_stale and ai_status == "READY":
        ai_status = "STALE"

    review_entries: list[dict] = []
    review_entries_raw: list[dict] = []
    presented_requirements, order_diagnostics = _prepare_requirements_for_display(requirements, preview=preview)
    if preview is not None:
        review_entries = _overlay_review_entries(
            preview.get("complete_requirement_entries"),
            requirements,
            prefer_raw=False,
        )
        review_entries_raw = _overlay_review_entries(
            preview.get("complete_requirement_entries_raw") or preview.get("complete_requirement_entries"),
            requirements,
            prefer_raw=True,
        )
        if not review_entries and requirements:
            review_entries = [dict(item, keep=True) for item in presented_requirements]
        if not review_entries_raw and requirements:
            review_entries_raw = [dict(item, keep=True) for item in presented_requirements]
        presented_requirements, order_diagnostics = _build_workspace_requirements_from_review_entries(
            review_entries,
            fallback_requirements=requirements,
            preview=preview,
        )

    ai_eligible_count = sum(1 for row in presented_requirements if _is_ai_eligible_requirement(row))
    assessment_target_count = sum(1 for row in presented_requirements if _is_assessment_target_requirement(row))
    info_count = sum(1 for row in presented_requirements if _is_info_only_requirement(row))
    presented_requirements = _attach_table_asset_urls_to_rows(
        presented_requirements,
        project_id=project_id,
        document_id=document_id,
    )
    review_entries = _attach_table_asset_urls_to_rows(review_entries, project_id=project_id, document_id=document_id)
    review_entries_raw = _attach_table_asset_urls_to_rows(
        review_entries_raw,
        project_id=project_id,
        document_id=document_id,
    )
    workspace: dict[str, object] = {
        "project": {
            "id": project.get("id"),
            "name": project.get("name"),
            "customer_name": project.get("customer_name"),
            "knowledge_base_version": knowledge_base_version,
        },
        "document": {
            "id": document.get("id"),
            "project_id": document.get("project_id"),
            "file_name": document.get("file_name"),
            "file_type": document.get("file_type"),
            "workflow_stage": str(document.get("workflow_stage") or "UPLOADED"),
            "requirement_set_version": requirement_set_version,
            "extraction_locked_at": str(document.get("extraction_locked_at") or ""),
            "ai_status": ai_status,
            "ai_assessed_at": str(document.get("ai_assessed_at") or ""),
            "ai_requirement_set_version": ai_requirement_set_version,
            "ai_knowledge_base_version": ai_knowledge_base_version,
            "ai_stale": ai_stale,
            "configured_assessment_mode": ai_runtime.assessment_mode,
            "default_ai_profile_id": ai_runtime.profile_id,
            "created_at": document.get("created_at"),
            "updated_at": document.get("updated_at"),
        },
        "summary": {
            "requirement_count": len(presented_requirements),
            "ai_eligible_count": ai_eligible_count,
            "assessment_target_count": assessment_target_count,
            "info_count": info_count,
            "knowledge_doc_count": int(knowledge_docs_count or 0),
            "configured_retrieval_backend": ai_runtime.retrieval_backend,
            "configured_azure_auth_mode": ai_runtime.azure_auth_mode,
            "runnable_ai_profile_count": sum(1 for profile in ai_profiles if profile.get("ready")),
            "locked": bool(str(document.get("extraction_locked_at") or "").strip()),
            "order_source": str(order_diagnostics.get("order_source") or "fallback_page_anchor"),
            "order_mismatch_count": int(order_diagnostics.get("order_mismatch_count") or 0),
            "preview_status": str(preview_meta.get("preview_status") or "NOT_READY"),
            "preview_generated_at": str(preview_meta.get("preview_generated_at") or ""),
            "preview_cache_hit": bool(preview_meta.get("preview_cache_hit")),
            "preview_error": str(preview_meta.get("preview_error") or ""),
        },
        "actions": {
            "can_lock_extraction": requirement_set_version > 0,
            "can_export": bool(str(document.get("extraction_locked_at") or "").strip())
            and len(presented_requirements) > 0,
            "can_run_ai_analysis": bool(str(document.get("extraction_locked_at") or "").strip())
            and ai_eligible_count > 0
            and any(bool(profile.get("ready")) for profile in ai_profiles),
        },
        "ai_profiles": ai_profiles,
        "requirements": presented_requirements,
    }
    if preview is not None:
        workspace["preview_generated_at"] = str(preview_meta.get("preview_generated_at") or "")
        workspace["preview"] = preview
        workspace["review_entries"] = review_entries
        workspace["review_entries_raw"] = review_entries_raw
    return workspace


def _present_requirements(requirements: list[dict], preview: dict | None = None) -> list[dict]:
    ordered, _ = _sort_requirements_for_display(requirements, preview=preview)
    presented: list[dict] = []
    for index, row in enumerate(ordered, start=1):
        copied = dict(row)
        copied["source_order"] = _coerce_positive_int(copied.get("source_order")) or index
        copied["display_sort_index"] = index
        copied["display_req_code"] = f"REQ{index:04d}"
        presented.append(copied)
    return presented


def _prepare_requirements_for_display(
    requirements: list[dict],
    *,
    preview: dict | None = None,
) -> tuple[list[dict], dict[str, object]]:
    presented = _present_requirements(requirements, preview=preview)
    diagnostics = _build_requirement_order_diagnostics(presented, preview=preview)
    return presented, diagnostics


def _require_locked_extraction_for_export(
    project_id: str,
    *,
    document: dict | None = None,
    requirements: list[dict] | None = None,
) -> None:
    scoped_documents: list[dict] = []
    if document is not None:
        document_id = str(document.get("id") or "").strip()
        current = db.backfill_sor_document_runtime(document_id) if document_id else None
        scoped_documents.append(current or document)
    else:
        seen_document_ids: set[str] = set()
        for row in requirements or []:
            document_id = str(row.get("sor_document_id") or "").strip()
            if not document_id or document_id in seen_document_ids:
                continue
            seen_document_ids.add(document_id)
            current = db.backfill_sor_document_runtime(document_id) or db.get_sor_document(document_id)
            if current and current.get("project_id") == project_id:
                scoped_documents.append(current)

    unlocked_documents = [
        item for item in scoped_documents if not str(item.get("extraction_locked_at") or "").strip()
    ]
    if not unlocked_documents:
        return

    names = ", ".join(
        str(item.get("file_name") or item.get("id") or "").strip() for item in unlocked_documents[:3]
    )
    detail = "Compliance Matrix export requires a locked extraction. Lock the reviewed requirement set before exporting."
    if names:
        detail = f"{detail} Unlocked document(s): {names}."
    raise HTTPException(status_code=409, detail=detail)


def _sort_requirements_for_display(
    requirements: list[dict],
    preview: dict | None = None,
) -> tuple[list[dict], dict[str, object]]:
    preview_order = _build_preview_requirement_order_index(preview)
    if not preview_order:
        ranked: list[tuple[tuple, dict]] = []
        for original_index, row in enumerate(requirements):
            copied = dict(row)
            copied["preview_rank"] = None
            ranked.append((_fallback_requirement_display_sort_key(copied, original_index), copied))
        ranked.sort(key=lambda item: item[0])
        ordered = [row for _, row in ranked]
        for display_order, row in enumerate(ordered, start=1):
            row["display_order"] = display_order
            row["source_order"] = display_order
        return ordered, {
            "order_source": "fallback_page_anchor",
            "order_mismatch_count": 0,
        }

    matched: list[tuple[int, int, dict]] = []
    unmatched: list[tuple[int, dict]] = []
    mismatch_count = 0
    for original_index, row in enumerate(requirements):
        preview_rank = _lookup_preview_rank(row, preview_order)
        copied = dict(row)
        if preview_rank < 10**9:
            copied["preview_rank"] = preview_rank
            copied["source_order"] = preview_rank + 1
            matched.append((preview_rank, original_index, copied))
        else:
            copied["preview_rank"] = None
            copied["source_order"] = 0
            unmatched.append((original_index, copied))
            mismatch_count += 1

    matched.sort(key=lambda item: (item[0], item[1]))
    matched_rows = [row for _, _, row in matched]
    last_matched_index_by_owner: dict[str, int] = {}
    for matched_index, row in enumerate(matched_rows):
        for token in _row_owner_section_tokens(row):
            last_matched_index_by_owner[token] = matched_index

    unmatched_after_index: dict[int, list[dict]] = {}
    unmatched_tail: list[dict] = []
    for _, row in unmatched:
        anchor_indices = [
            last_matched_index_by_owner[token]
            for token in _row_owner_section_tokens(row)
            if token in last_matched_index_by_owner
        ]
        if anchor_indices:
            unmatched_after_index.setdefault(max(anchor_indices), []).append(row)
        else:
            unmatched_tail.append(row)

    ordered: list[dict] = []
    for matched_index, row in enumerate(matched_rows):
        ordered.append(row)
        ordered.extend(unmatched_after_index.get(matched_index, []))
    ordered.extend(unmatched_tail)

    for display_order, row in enumerate(ordered, start=1):
        row["display_order"] = display_order
        if row.get("preview_rank") is None:
            row["source_order"] = display_order

    return ordered, {
        "order_source": "review_overlay",
        "order_mismatch_count": mismatch_count,
    }


def _build_requirement_order_diagnostics(
    requirements: list[dict],
    *,
    preview: dict | None = None,
) -> dict[str, object]:
    preview_order = _build_preview_requirement_order_index(preview)
    if not requirements:
        return {
            "order_source": "review_overlay" if preview_order else "fallback_page_anchor",
            "order_mismatch_count": 0,
        }
    if not preview_order:
        return {"order_source": "fallback_page_anchor", "order_mismatch_count": 0}
    mismatch_count = sum(1 for row in requirements if _lookup_preview_rank(row, preview_order) >= 10**9)
    return {"order_source": "review_overlay", "order_mismatch_count": mismatch_count}


def _build_preview_requirement_order_index(preview: dict | None) -> dict[str, object]:
    if not isinstance(preview, dict):
        return {}
    rows = preview.get("complete_requirement_entries_raw") or preview.get("complete_requirement_entries") or []
    if not isinstance(rows, list):
        return {}
    normalized_rows = [dict(row) for row in rows if isinstance(row, dict)]
    return _build_requirement_row_index(normalized_rows)


def _lookup_preview_rank(row: dict, preview_order: dict[str, object]) -> int:
    if not preview_order:
        return 10**9
    matched = _find_indexed_requirement_row(row, preview_order, allow_weak_identifier=True)
    if matched is None:
        return 10**9
    rank_by_row_id = preview_order.get("rank_by_row_id")
    if not isinstance(rank_by_row_id, dict):
        return 10**9
    try:
        return int(rank_by_row_id.get(id(matched), 10**9))
    except (TypeError, ValueError):
        return 10**9


def _fallback_requirement_display_sort_key(row: dict, original_index: int) -> tuple:
    page = _coerce_positive_int(row.get("source_page"))
    page_end = _coerce_positive_int(row.get("source_page_end")) or page
    section_key = _natural_sort_key(
        str(
            row.get("owner_section_code")
            or row.get("section_code")
            or row.get("parent_section_code")
            or row.get("owner_section_label")
            or row.get("parent_section_label")
            or ""
        )
    )
    anchor_key = _natural_sort_key(str(row.get("source_anchor") or ""))
    req_code_key = _natural_sort_key(str(row.get("req_code") or ""))
    text_key = _natural_sort_key(str(row.get("raw_text") or row.get("display_text") or row.get("requirement_text") or ""))
    return (
        page if page > 0 else 10**9,
        page_end if page_end > 0 else 10**9,
        section_key,
        anchor_key,
        req_code_key,
        text_key,
        original_index,
    )


def _requirement_match_tokens(row: dict) -> list[str]:
    tokens: list[str] = []
    req_code = str(row.get("req_code") or "").strip()
    if req_code:
        tokens.append(f"req:{req_code}")
        base = _base_req_code(req_code)
        if base:
            tokens.append(f"base:{base}")
    entry_id = str(row.get("entry_id") or "").strip()
    if entry_id:
        tokens.append(f"entry:{entry_id}")
    page = _coerce_positive_int(row.get("source_page"))
    text = str(row.get("raw_text") or row.get("display_text") or row.get("requirement_text") or "").strip()
    if page > 0 and text:
        tokens.append(f"fp:{page}:{_normalize_requirement_match_text(text)}")
    return tokens


def _normalize_requirement_match_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    return normalized[:240]


def _normalize_source_anchor(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _requirement_anchor_match_key(row: dict) -> tuple[int, str] | None:
    page = _coerce_positive_int(row.get("source_page"))
    anchor = _normalize_source_anchor(row.get("source_anchor"))
    if page <= 0 or not anchor:
        return None
    return (page, anchor)


def _requirement_page_text_match_key(row: dict) -> tuple[int, str] | None:
    page = _coerce_positive_int(row.get("source_page"))
    text = _normalize_requirement_match_text(
        str(row.get("raw_text") or row.get("display_text") or row.get("requirement_text") or "")
    )
    if page <= 0 or not text:
        return None
    return (page, text)


def _requirement_anchor_text_match_key(row: dict) -> tuple[str, str] | None:
    anchor = _normalize_source_anchor(row.get("source_anchor"))
    text = _normalize_requirement_match_text(
        str(row.get("raw_text") or row.get("display_text") or row.get("requirement_text") or "")
    )
    if not anchor or not text:
        return None
    return (anchor, text)


def _requirement_anchor_only_key(row: dict) -> str:
    return _normalize_source_anchor(row.get("source_anchor"))


def _requirement_exact_match_key(row: dict) -> tuple[int, str, str] | None:
    anchor_key = _requirement_anchor_match_key(row)
    text_key = _normalize_requirement_match_text(
        str(row.get("raw_text") or row.get("display_text") or row.get("requirement_text") or "")
    )
    if anchor_key is None or not text_key:
        return None
    return (anchor_key[0], anchor_key[1], text_key)


def _row_has_strong_requirement_identity(row: dict) -> bool:
    return bool(
        _requirement_anchor_match_key(row)
        or _requirement_page_text_match_key(row)
        or _requirement_anchor_text_match_key(row)
        or _requirement_anchor_only_key(row)
    )


def _requirement_entry_kind_group(row: dict) -> str:
    return str(row.get("entry_kind") or "").strip().upper()


def _normalized_section_identity_value(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _requirement_structural_identity_keys(row: dict) -> list[str]:
    kind = _requirement_entry_kind_group(row)
    if not kind:
        return []
    section_tokens: list[str] = []
    for field_name in (
        "section_instance_key",
        "section_key",
        "owner_section_code",
        "parent_section_code",
        "section_code",
    ):
        value = _normalized_section_identity_value(row.get(field_name))
        if value and value not in section_tokens:
            prefix = "code" if field_name.endswith("_code") else "section"
            section_tokens.append(f"{prefix}:{value}")
    identifier_tokens: list[str] = []
    entry_id = str(row.get("entry_id") or "").strip()
    if entry_id:
        identifier_tokens.append(f"entry:{entry_id}")
    req_code = str(row.get("req_code") or "").strip()
    if req_code:
        identifier_tokens.append(f"req:{req_code}")
        base_req_code = _base_req_code(req_code)
        if base_req_code:
            identifier_tokens.append(f"base:{base_req_code}")
    if not section_tokens or not identifier_tokens:
        return []
    keys: list[str] = []
    for section_token in section_tokens:
        for identifier_token in identifier_tokens:
            keys.append(f"{kind}|{section_token}|{identifier_token}")
    return keys


def _row_owner_section_tokens(row: dict) -> list[str]:
    tokens: list[str] = []
    for field_name in (
        "section_instance_key",
        "section_key",
        "owner_section_code",
        "owner_section_label",
        "owner_section_title",
        "section_code",
        "parent_section_code",
        "parent_section_label",
        "parent_section_title",
    ):
        value = _normalized_section_identity_value(row.get(field_name))
        if value and value not in tokens:
            tokens.append(value)
    return tokens


def _rows_have_compatible_entry_kind(left: dict, right: dict) -> bool:
    left_kind = _requirement_entry_kind_group(left)
    right_kind = _requirement_entry_kind_group(right)
    if not left_kind or not right_kind:
        return True
    if left_kind == right_kind:
        return True
    pair = {left_kind, right_kind}
    if pair <= {"REQUIREMENT", "HYBRID"}:
        return True
    if pair <= {"HEADING", "HYBRID"}:
        return True
    return False


def _append_requirement_row_to_index(index: dict[str, object], row: dict) -> None:
    rows = index.setdefault("rows", [])
    if isinstance(rows, list):
        rows.append(row)
        rank_by_row_id = index.setdefault("rank_by_row_id", {})
        if isinstance(rank_by_row_id, dict):
            rank_by_row_id[id(row)] = len(rows) - 1

    def _append_bucket(bucket_name: str, key: object) -> None:
        if key in (None, "", ()):
            return
        bucket = index.setdefault(bucket_name, {})
        if not isinstance(bucket, dict):
            return
        bucket.setdefault(key, []).append(row)

    _append_bucket("by_exact", _requirement_exact_match_key(row))
    _append_bucket("by_anchor", _requirement_anchor_match_key(row))
    _append_bucket("by_page_text", _requirement_page_text_match_key(row))
    _append_bucket("by_anchor_text", _requirement_anchor_text_match_key(row))
    _append_bucket("by_anchor_only", _requirement_anchor_only_key(row))
    for identity_key in _requirement_structural_identity_keys(row):
        _append_bucket("by_identity", identity_key)
    entry_id = str(row.get("entry_id") or "").strip()
    if entry_id:
        _append_bucket("by_entry", entry_id)
    req_code = str(row.get("req_code") or "").strip()
    if req_code:
        _append_bucket("by_req", req_code)
        base_req_code = _base_req_code(req_code)
        if base_req_code:
            _append_bucket("by_base_req", base_req_code)


def _build_requirement_row_index(rows: list[dict]) -> dict[str, object]:
    index: dict[str, object] = {
        "rows": [],
        "rank_by_row_id": {},
        "by_identity": {},
        "by_exact": {},
        "by_anchor": {},
        "by_page_text": {},
        "by_anchor_text": {},
        "by_anchor_only": {},
        "by_entry": {},
        "by_req": {},
        "by_base_req": {},
    }
    for row in rows or []:
        if isinstance(row, dict):
            _append_requirement_row_to_index(index, row)
    return index


def _first_unique_index_match(index: dict[str, object], bucket_name: str, key: object) -> dict | None:
    if key in (None, "", ()):
        return None
    bucket = index.get(bucket_name)
    if not isinstance(bucket, dict):
        return None
    matches = bucket.get(key)
    if not isinstance(matches, list) or len(matches) != 1:
        return None
    candidate = matches[0]
    return candidate if isinstance(candidate, dict) else None


def _candidate_supports_weak_requirement_match(target: dict, candidate: dict) -> bool:
    target_exact = _requirement_exact_match_key(target)
    candidate_exact = _requirement_exact_match_key(candidate)
    if target_exact and candidate_exact and target_exact == candidate_exact:
        return True

    target_anchor = _requirement_anchor_match_key(target)
    candidate_anchor = _requirement_anchor_match_key(candidate)
    if target_anchor and candidate_anchor and target_anchor == candidate_anchor:
        return True

    target_page_text = _requirement_page_text_match_key(target)
    candidate_page_text = _requirement_page_text_match_key(candidate)
    if target_page_text and candidate_page_text and target_page_text == candidate_page_text:
        return True

    target_anchor_text = _requirement_anchor_text_match_key(target)
    candidate_anchor_text = _requirement_anchor_text_match_key(candidate)
    if target_anchor_text and candidate_anchor_text and target_anchor_text == candidate_anchor_text:
        return True

    target_anchor_only = _requirement_anchor_only_key(target)
    candidate_anchor_only = _requirement_anchor_only_key(candidate)
    if target_anchor_only and candidate_anchor_only and target_anchor_only == candidate_anchor_only:
        return True

    return not _row_has_strong_requirement_identity(target)


def _find_indexed_requirement_row(
    row: dict,
    index: dict[str, object],
    *,
    allow_weak_identifier: bool = True,
) -> dict | None:
    rows = index.get("rows")
    if not isinstance(rows, list) or not rows:
        return None

    for identity_key in _requirement_structural_identity_keys(row):
        candidate = _first_unique_index_match(index, "by_identity", identity_key)
        if candidate is not None and _rows_have_compatible_entry_kind(row, candidate):
            return candidate

    for bucket_name, key in (
        ("by_exact", _requirement_exact_match_key(row)),
        ("by_anchor", _requirement_anchor_match_key(row)),
        ("by_page_text", _requirement_page_text_match_key(row)),
        ("by_anchor_text", _requirement_anchor_text_match_key(row)),
        ("by_anchor_only", _requirement_anchor_only_key(row)),
    ):
        candidate = _first_unique_index_match(index, bucket_name, key)
        if candidate is not None and _rows_have_compatible_entry_kind(row, candidate):
            return candidate

    if not allow_weak_identifier:
        return None

    seen_candidates: set[int] = set()
    weak_tokens: list[tuple[str, str]] = []
    entry_id = str(row.get("entry_id") or "").strip()
    if entry_id:
        weak_tokens.append(("by_entry", entry_id))
    req_code = str(row.get("req_code") or "").strip()
    if req_code:
        weak_tokens.append(("by_req", req_code))
        base_req_code = _base_req_code(req_code)
        if base_req_code:
            weak_tokens.append(("by_base_req", base_req_code))

    for bucket_name, token in weak_tokens:
        bucket = index.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        for candidate in bucket.get(token, []):
            if not isinstance(candidate, dict):
                continue
            candidate_id = id(candidate)
            if candidate_id in seen_candidates:
                continue
            seen_candidates.add(candidate_id)
            if not _rows_have_compatible_entry_kind(row, candidate):
                continue
            if _candidate_supports_weak_requirement_match(row, candidate):
                return candidate
    return None


def _natural_sort_key(value: str) -> tuple:
    parts = re.split(r"(\d+)", str(value or "").strip().lower())
    key: list[tuple[int, object]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def _coerce_positive_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _record_assessment_runs(requirement_id: str, runs: list[dict[str, Any]]) -> None:
    for run in runs:
        if not isinstance(run, dict):
            continue
        db.add_assessment_run(
            requirement_id=requirement_id,
            profile_id=str(run.get("profile_id") or ""),
            provider=str(run.get("provider") or ""),
            mode=str(run.get("mode") or ""),
            retrieval_backend=str(run.get("retrieval_backend") or ""),
            model_name=str(run.get("model_name") or ""),
            prompt_version=str(run.get("prompt_version") or ""),
            input_payload_json=str(run.get("input_payload_json") or "{}"),
            output_payload_json=str(run.get("output_payload_json") or ""),
            validation_status=str(run.get("validation_status") or ""),
            fallback_reason=str(run.get("fallback_reason") or ""),
        )


def _is_ai_eligible_requirement(row: dict) -> bool:
    return str(row.get("entry_kind") or "REQUIREMENT").strip().upper() in {"REQUIREMENT", "HYBRID"}


def _is_info_only_requirement(row: dict) -> bool:
    return str(row.get("analysis_label") or "").strip().upper() == "INFO"


def _is_assessment_target_requirement(row: dict) -> bool:
    return _is_ai_eligible_requirement(row) and not _is_info_only_requirement(row)


def _coerce_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _overlay_review_entries(rows: object, persisted_requirements: list[dict], *, prefer_raw: bool) -> list[dict]:
    if not isinstance(rows, list):
        return []
    persisted_index = _build_requirement_row_index([dict(item) for item in (persisted_requirements or []) if isinstance(item, dict)])
    overlaid: list[dict] = []
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        row["preview_rank"] = index
        row["display_order"] = index + 1
        persisted = _find_indexed_requirement_row(row, persisted_index, allow_weak_identifier=True)
        if persisted and not _persisted_review_matches_preview_row(persisted, row):
            persisted = None
        if persisted:
            merged_refs, merged_blocks, merged_has_asset, merged_render_mode = _merge_preview_table_assets_for_review_row(
                preview_row=row,
                persisted_row=persisted,
            )
            row["id"] = persisted.get("id")
            row["keep"] = True
            row["entry_kind"] = str(persisted.get("entry_kind") or row.get("entry_kind") or "REQUIREMENT").strip().upper()
            row["requirement_type"] = str(persisted.get("requirement_type") or row.get("requirement_type") or "")
            row["priority"] = str(persisted.get("priority") or row.get("priority") or "")
            row["dedupe_version"] = str(persisted.get("dedupe_version") or row.get("dedupe_version") or "")
            row["table_refs"] = merged_refs
            row["content_blocks"] = merged_blocks
            row["has_table_asset"] = merged_has_asset
            row["table_render_mode"] = merged_render_mode
            row["status"] = persisted.get("status")
            row["ai_decision"] = persisted.get("ai_decision")
            row["ai_reason"] = persisted.get("ai_reason")
            row["ai_confidence"] = persisted.get("ai_confidence")
            row["assessment_mode"] = persisted.get("assessment_mode")
            row["analysis_label"] = persisted.get("analysis_label")
            row["analysis_basis"] = persisted.get("analysis_basis")
            row["normalized_requirement"] = persisted.get("normalized_requirement")
            row["risk_level"] = persisted.get("risk_level")
            row["clarification_question"] = persisted.get("clarification_question")
            row["final_decision"] = persisted.get("final_decision")
            row["engineer_reason"] = persisted.get("engineer_reason")
            row["evidences"] = persisted.get("evidences", [])
        else:
            row["keep"] = False
            row["entry_kind"] = str(row.get("entry_kind") or "REQUIREMENT").strip().upper() or "REQUIREMENT"
        if prefer_raw:
            row["requirement_text"] = str(row.get("raw_text") or row.get("requirement_text") or row.get("display_text") or "")
        else:
            row["requirement_text"] = str(row.get("display_text") or row.get("requirement_text") or row.get("raw_text") or "")
        if row.get("table_refs") or row.get("content_blocks"):
            row = _refresh_enriched_entry_text_fields(row)
        row["entry_id"] = row.get("entry_id") or row.get("req_code") or f"ENTRY-{index + 1}"
        overlaid.append(row)
    return overlaid


def _merge_preview_table_assets_for_review_row(
    *,
    preview_row: dict,
    persisted_row: dict,
) -> tuple[list[dict], list[dict], bool, str]:
    preview_refs = [dict(item) for item in (preview_row.get("table_refs") or []) if isinstance(item, dict)]
    persisted_refs = [dict(item) for item in (persisted_row.get("table_refs") or []) if isinstance(item, dict)]
    merged_refs: list[dict]

    if preview_refs:
        persisted_ref_by_id = {
            str(item.get("table_id") or "").strip(): item
            for item in persisted_refs
            if str(item.get("table_id") or "").strip()
        }
        merged_refs = []
        for ref in preview_refs:
            merged = dict(ref)
            table_id = str(ref.get("table_id") or "").strip()
            persisted_ref = persisted_ref_by_id.get(table_id)
            if persisted_ref:
                export_variant = str(persisted_ref.get("export_variant") or "").strip().lower()
                recommended = str(merged.get("recommended_export_variant") or "").strip().lower()
                override_marker = persisted_ref.get("export_variant_overridden")
                should_preserve_variant = export_variant in {"slice", "source"} and (
                    bool(override_marker)
                    or (
                        override_marker is None
                        and recommended in {"slice", "source"}
                        and export_variant != recommended
                    )
                )
                if should_preserve_variant:
                    merged["export_variant"] = export_variant
                    if recommended in {"slice", "source"}:
                        merged["export_variant_overridden"] = export_variant != recommended
            merged_refs.append(merged)
    else:
        merged_refs = persisted_refs

    preview_blocks = preview_row.get("content_blocks") if isinstance(preview_row.get("content_blocks"), list) else []
    persisted_blocks = persisted_row.get("content_blocks") if isinstance(persisted_row.get("content_blocks"), list) else []
    merged_blocks = preview_blocks or persisted_blocks or []
    merged_has_asset = bool(preview_row.get("has_table_asset")) if preview_row.get("has_table_asset") is not None else bool(
        persisted_row.get("has_table_asset")
    )
    merged_render_mode = str(preview_row.get("table_render_mode") or persisted_row.get("table_render_mode") or "").strip()
    if merged_refs:
        merged_has_asset = True
    return merged_refs, merged_blocks, merged_has_asset, merged_render_mode


def _persisted_review_matches_preview_row(persisted: dict, row: dict) -> bool:
    persisted_exact = _requirement_exact_match_key(persisted)
    preview_exact = _requirement_exact_match_key(row)
    if persisted_exact and preview_exact and persisted_exact == preview_exact:
        return True

    persisted_anchor = _requirement_anchor_match_key(persisted)
    preview_anchor = _requirement_anchor_match_key(row)
    if persisted_anchor and preview_anchor and persisted_anchor == preview_anchor:
        return True

    persisted_page_text = _requirement_page_text_match_key(persisted)
    preview_page_text = _requirement_page_text_match_key(row)
    if persisted_page_text and preview_page_text and persisted_page_text == preview_page_text:
        return True

    persisted_anchor_text = _requirement_anchor_text_match_key(persisted)
    preview_anchor_text = _requirement_anchor_text_match_key(row)
    if persisted_anchor_text and preview_anchor_text and persisted_anchor_text == preview_anchor_text:
        return True

    persisted_anchor_only = _requirement_anchor_only_key(persisted)
    preview_anchor_only = _requirement_anchor_only_key(row)
    if persisted_anchor_only and preview_anchor_only and persisted_anchor_only == preview_anchor_only:
        return True
    return False


def _build_workspace_requirements_from_review_entries(
    rows: list[dict],
    *,
    fallback_requirements: list[dict],
    preview: dict | None,
) -> tuple[list[dict], dict[str, object]]:
    current_rows: list[dict] = []
    for row in rows or []:
        if not bool(row.get("keep", True)):
            continue
        copied = hydrate_complex_row_metadata(dict(row))
        copied["requirement_text"] = str(
            row.get("raw_text") or row.get("requirement_text") or row.get("display_text") or ""
        ).strip()
        copied["raw_text"] = str(row.get("raw_text") or copied["requirement_text"] or "").strip()
        copied["display_text"] = str(row.get("display_text") or copied["raw_text"] or "").strip()
        copied["table_refs"] = row.get("table_refs") or []
        copied["content_blocks"] = row.get("content_blocks") or []
        copied["has_table_asset"] = bool(row.get("has_table_asset"))
        copied["table_render_mode"] = str(row.get("table_render_mode") or "")
        current_rows.append(copied)
    current_index = _build_requirement_row_index(current_rows)
    active_complex_groups = [row for row in current_rows if is_complex_table_row(row, include_summary=False)]
    for row in fallback_requirements or []:
        if not _is_ai_eligible_requirement(row) and str(row.get("entry_kind") or "").upper() != "HEADING":
            continue
        if row_is_shadowed_by_complex_group(row, active_complex_groups):
            continue
        if _find_indexed_requirement_row(row, current_index, allow_weak_identifier=True) is not None:
            continue
        copied = hydrate_complex_row_metadata(dict(row))
        copied["requirement_text"] = str(
            copied.get("raw_text") or copied.get("requirement_text") or copied.get("display_text") or ""
        ).strip()
        copied["raw_text"] = str(copied.get("raw_text") or copied["requirement_text"] or "").strip()
        copied["display_text"] = str(copied.get("display_text") or copied["raw_text"] or "").strip()
        copied["table_refs"] = copied.get("table_refs") or []
        copied["content_blocks"] = copied.get("content_blocks") or []
        copied["has_table_asset"] = bool(copied.get("has_table_asset"))
        copied["table_render_mode"] = str(copied.get("table_render_mode") or "")
        current_rows.append(copied)
        _append_requirement_row_to_index(current_index, copied)
    if not current_rows:
        return _prepare_requirements_for_display(fallback_requirements, preview=preview)
    return _prepare_requirements_for_display(current_rows, preview=preview)


def _base_req_code(value: object) -> str:
    import re

    token = str(value or "").strip()
    if not token:
        return ""
    return re.sub(r"-D\d+$", "", token, flags=re.I)


def _normalize_review_entry_payload(item: ReviewedRequirementEntry) -> dict[str, object]:
    row = item.model_dump()
    display_text = str(row.get("display_text") or row.get("requirement_text") or "").strip()
    raw_text = str(row.get("raw_text") or row.get("requirement_text") or display_text).strip()
    preferred_text = raw_text or display_text
    normalized = {
        "entry_id": str(row.get("entry_id") or row.get("req_code") or "").strip(),
        "req_code": str(row.get("req_code") or "").strip(),
        "keep": bool(row.get("keep", True)),
        "entry_kind": str(row.get("entry_kind") or "REQUIREMENT").strip().upper() or "REQUIREMENT",
        "section_code": str(row.get("section_code") or "").strip(),
        "section_title": str(row.get("section_title") or "").strip(),
        "section_label": str(row.get("section_label") or "").strip(),
        "section_key": str(row.get("section_key") or "").strip(),
        "section_instance_key": str(row.get("section_instance_key") or "").strip(),
        "source_page": row.get("source_page"),
        "source_page_end": row.get("source_page_end"),
        "source_anchor": str(row.get("source_anchor") or "Unknown").strip() or "Unknown",
        "display_text": preferred_text,
        "raw_text": preferred_text,
        "requirement_type": str(row.get("requirement_type") or "FUNCTIONAL").strip() or "FUNCTIONAL",
        "priority": str(row.get("priority") or "MEDIUM").strip() or "MEDIUM",
        "text_relation": str(row.get("text_relation") or "distinct").strip() or "distinct",
        "is_deduped": bool(row.get("is_deduped")),
        "heading_text": str(row.get("heading_text") or "").strip(),
        "parent_section_code": str(row.get("parent_section_code") or "").strip(),
        "parent_section_title": str(row.get("parent_section_title") or "").strip(),
        "parent_section_label": str(row.get("parent_section_label") or "").strip(),
        "owner_section_code": str(row.get("owner_section_code") or "").strip(),
        "owner_section_title": str(row.get("owner_section_title") or "").strip(),
        "owner_section_label": str(row.get("owner_section_label") or "").strip(),
        "structural_parent_section_code": str(row.get("structural_parent_section_code") or "").strip(),
        "structural_parent_section_key": str(row.get("structural_parent_section_key") or "").strip(),
        "structural_parent_section_title": str(row.get("structural_parent_section_title") or "").strip(),
        "structural_parent_section_label": str(row.get("structural_parent_section_label") or "").strip(),
        "merged_from_req_codes": row.get("merged_from_req_codes") or [],
        "merged_from_entry_ids": row.get("merged_from_entry_ids") or [],
        "table_refs": row.get("table_refs") or [],
        "content_blocks": row.get("content_blocks") or [],
        "has_table_asset": bool(row.get("has_table_asset")),
        "table_render_mode": str(row.get("table_render_mode") or "").strip(),
        "dedupe_version": str(row.get("dedupe_version") or "heading_requirement_v1").strip() or "heading_requirement_v1",
        **_copy_complex_row_fields(row),
    }
    if is_complex_table_row(normalized):
        return normalize_preserved_entry_table_assets(normalized)
    enriched, _ = enrich_entry_with_table_assets(
        normalized,
        document_id="preview",
        snapshot_root=_table_snapshot_root("preview"),
    )
    normalized.update(
        {
            "table_refs": enriched.get("table_refs") or [],
            "content_blocks": enriched.get("content_blocks") or [],
            "has_table_asset": bool(enriched.get("has_table_asset")),
            "table_render_mode": str(enriched.get("table_render_mode") or ""),
        }
    )
    return hydrate_complex_row_metadata(normalized)


def _find_requirement_for_review_entry(requirements: list[dict], normalized: dict[str, object]) -> dict | None:
    requirement_index = _build_requirement_row_index([dict(row) for row in requirements if isinstance(row, dict)])
    return _find_indexed_requirement_row(dict(normalized), requirement_index, allow_weak_identifier=True)


def _build_saved_review_entry(normalized: dict[str, object], persisted: dict | None, *, keep: bool) -> dict[str, object]:
    persisted = persisted or {}
    entry_id = str(normalized.get("entry_id") or persisted.get("req_code") or persisted.get("id") or "").strip()
    actual_req_code = str(persisted.get("req_code") or normalized.get("req_code") or "").strip()
    display_text = str(
        persisted.get("display_text")
        or normalized.get("display_text")
        or persisted.get("requirement_text")
        or normalized.get("raw_text")
        or ""
    ).strip()
    raw_text = str(
        persisted.get("raw_text")
        or normalized.get("raw_text")
        or persisted.get("requirement_text")
        or display_text
    ).strip()
    source_page = persisted.get("source_page")
    if source_page in ("", None):
        source_page = normalized.get("source_page")
    source_page_end = normalized.get("source_page_end")
    if source_page_end in ("", None):
        source_page_end = source_page
    saved = {
        "entry_id": entry_id or actual_req_code,
        "req_code": actual_req_code,
        "keep": bool(keep),
        "entry_kind": str(persisted.get("entry_kind") or normalized.get("entry_kind") or "REQUIREMENT").strip().upper()
        or "REQUIREMENT",
        "section_code": str(persisted.get("section_code") or normalized.get("section_code") or "").strip(),
        "section_title": str(persisted.get("section_title") or normalized.get("section_title") or "").strip(),
        "section_label": str(persisted.get("section_label") or normalized.get("section_label") or "").strip(),
        "section_key": str(persisted.get("section_key") or normalized.get("section_key") or "").strip(),
        "section_instance_key": str(
            persisted.get("section_instance_key") or normalized.get("section_instance_key") or ""
        ).strip(),
        "source_page": source_page,
        "source_page_end": source_page_end,
        "source_anchor": str(persisted.get("source_anchor") or normalized.get("source_anchor") or "Unknown").strip()
        or "Unknown",
        "requirement_text": raw_text or display_text,
        "display_text": display_text or raw_text,
        "raw_text": raw_text or display_text,
        "requirement_type": str(
            persisted.get("requirement_type") or normalized.get("requirement_type") or "FUNCTIONAL"
        ).strip()
        or "FUNCTIONAL",
        "priority": str(persisted.get("priority") or normalized.get("priority") or "MEDIUM").strip() or "MEDIUM",
        "text_relation": str(persisted.get("text_relation") or normalized.get("text_relation") or "distinct").strip()
        or "distinct",
        "is_deduped": bool(
            persisted.get("is_deduped")
            if persisted.get("is_deduped") is not None
            else normalized.get("is_deduped")
        ),
        "heading_text": str(persisted.get("heading_text") or normalized.get("heading_text") or "").strip(),
        "parent_section_code": str(
            persisted.get("parent_section_code") or normalized.get("parent_section_code") or ""
        ).strip(),
        "parent_section_title": str(
            persisted.get("parent_section_title") or normalized.get("parent_section_title") or ""
        ).strip(),
        "parent_section_label": str(
            persisted.get("parent_section_label") or normalized.get("parent_section_label") or ""
        ).strip(),
        "owner_section_code": str(
            persisted.get("owner_section_code") or normalized.get("owner_section_code") or ""
        ).strip(),
        "owner_section_title": str(
            persisted.get("owner_section_title") or normalized.get("owner_section_title") or ""
        ).strip(),
        "owner_section_label": str(
            persisted.get("owner_section_label") or normalized.get("owner_section_label") or ""
        ).strip(),
        "structural_parent_section_code": str(
            persisted.get("structural_parent_section_code") or normalized.get("structural_parent_section_code") or ""
        ).strip(),
        "structural_parent_section_key": str(
            persisted.get("structural_parent_section_key") or normalized.get("structural_parent_section_key") or ""
        ).strip(),
        "structural_parent_section_title": str(
            persisted.get("structural_parent_section_title") or normalized.get("structural_parent_section_title") or ""
        ).strip(),
        "structural_parent_section_label": str(
            persisted.get("structural_parent_section_label") or normalized.get("structural_parent_section_label") or ""
        ).strip(),
        "merged_from_req_codes": persisted.get("merged_from_req_codes")
        if isinstance(persisted.get("merged_from_req_codes"), list)
        else list(normalized.get("merged_from_req_codes") or []),
        "merged_from_entry_ids": persisted.get("merged_from_entry_ids")
        if isinstance(persisted.get("merged_from_entry_ids"), list)
        else list(normalized.get("merged_from_entry_ids") or []),
        "dedupe_version": str(persisted.get("dedupe_version") or normalized.get("dedupe_version") or "").strip(),
        "table_refs": persisted.get("table_refs")
        if isinstance(persisted.get("table_refs"), list)
        else list(normalized.get("table_refs") or []),
        "content_blocks": persisted.get("content_blocks")
        if isinstance(persisted.get("content_blocks"), list)
        else list(normalized.get("content_blocks") or []),
        "has_table_asset": bool(
            persisted.get("has_table_asset")
            if persisted.get("has_table_asset") is not None
            else normalized.get("has_table_asset")
        ),
        "table_render_mode": str(
            persisted.get("table_render_mode") or normalized.get("table_render_mode") or ""
        ).strip(),
    }
    saved.update(_copy_complex_row_fields(normalized))
    return hydrate_complex_row_metadata(saved)
