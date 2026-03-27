from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app.services.assessor import assess_requirement
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
from app.services.progress_tracker import add_progress_event, load_progress_snapshot, replace_plan_items
from app.services.preview_cache import (
    get_preview_cache_state,
    load_cached_preview,
    mark_preview_stale,
    persist_preview_cache,
    reset_document_snapshot_root,
    request_preview_refresh,
)
from app.services.rag import retrieve_evidence
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
STORAGE_DIR = PROTOTYPE_DIR / "storage"
UPLOADS_DIR = STORAGE_DIR / "uploads"
KNOWLEDGE_DIR = STORAGE_DIR / "knowledge"
EXPORTS_DIR = STORAGE_DIR / "exports"
TABLE_SNAPSHOT_DIR = STORAGE_DIR / "table_snapshots"
STATIC_DIR = APP_DIR / "static"

for directory in (UPLOADS_DIR, KNOWLEDGE_DIR, EXPORTS_DIR, TABLE_SNAPSHOT_DIR):
    directory.mkdir(parents=True, exist_ok=True)


_ASSET_RESPONSE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


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


class ReviewedRequirementEntry(BaseModel):
    entry_id: str | None = None
    req_code: str | None = None
    entry_kind: str | None = None
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


class ProgressPlanItem(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1, max_length=200)
    owner: str = Field(default="", max_length=100)
    status: str = Field(default="TODO", max_length=32)
    progress_pct: int = Field(default=0, ge=0, le=100)
    target_date: str = Field(default="", max_length=32)
    notes: str = Field(default="", max_length=1000)


class ProgressPlanUpdateRequest(BaseModel):
    items: list[ProgressPlanItem]


app = FastAPI(title="SOR AI Prototype", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.on_event("startup")
def startup() -> None:
    db.init_db()


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
def health() -> dict[str, str]:
    return {"status": "ok"}


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


@app.post("/api/projects/{project_id}/knowledge-documents")
async def upload_knowledge_document(project_id: str, file: UploadFile = File(...)) -> dict:
    _require_project(project_id)
    suffix = Path(file.filename or "").suffix or ".txt"
    file_name = file.filename or f"knowledge_{uuid.uuid4().hex}{suffix}"
    target = KNOWLEDGE_DIR / f"{uuid.uuid4().hex}_{_safe_name(file_name)}"
    contents = await file.read()
    target.write_bytes(contents)

    extracted_text, _, file_type = parse_document(str(target))
    document = db.add_knowledge_document(
        project_id=project_id,
        file_name=file_name,
        file_type=file_type,
        file_path=str(target),
        extracted_text=extracted_text,
    )
    project = db.bump_project_knowledge_base_version(project_id)
    return {
        **document,
        "knowledge_base_version": int(project.get("knowledge_base_version") or 0),
    }


@app.get("/api/projects/{project_id}/knowledge-documents")
def list_knowledge_documents(project_id: str) -> list[dict]:
    _require_project(project_id)
    return db.list_knowledge_documents(project_id)


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
    knowledge_docs = db.list_knowledge_documents(project_id)
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

    knowledge_docs = db.list_knowledge_documents(project_id)
    if not knowledge_docs:
        raise HTTPException(status_code=422, detail="No knowledge documents uploaded for this project.")

    db.update_sor_document_ai_state(document_id, ai_status="RUNNING", workflow_stage="AI_ANALYZING")
    try:
        for row in eligible_rows:
            requirement_text = str(row.get("display_text") or row.get("requirement_text") or "").strip()
            if not requirement_text:
                continue
            evidences = retrieve_evidence(requirement_text, knowledge_docs, top_k=3)
            assessment = assess_requirement(
                requirement_text=requirement_text,
                requirement_type=str(row.get("requirement_type") or "FUNCTIONAL").strip() or "FUNCTIONAL",
                evidences=evidences,
            )
            db.upsert_assessment(
                requirement_id=str(row.get("id") or ""),
                ai_decision=assessment["ai_decision"],
                ai_reason=assessment["ai_reason"],
                ai_confidence=float(assessment["ai_confidence"]),
                model_name=assessment["model_name"],
                prompt_version=assessment["prompt_version"],
            )
            db.replace_evidences(requirement_id=str(row.get("id") or ""), evidences=evidences)
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
        "ai_processed_requirements": len(eligible_rows),
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
    knowledge_docs = db.list_knowledge_documents(project_id)

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
    knowledge_docs = db.list_knowledge_documents(project_id)
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
    assessment = db.update_final_decision(
        requirement_id=requirement_id,
        final_decision=payload.final_decision,
        engineer_reason=payload.engineer_reason.strip(),
    )
    return {"requirement_id": requirement_id, "assessment": assessment}


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
            knowledge_docs_count=len(db.list_knowledge_documents(project_id)),
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


@app.post("/api/progress/events")
def create_progress_event(payload: ProgressEventCreateRequest) -> dict:
    try:
        event = add_progress_event(
            title=payload.title,
            detail=payload.detail,
            category=payload.category,
            status=payload.status,
            event_time=payload.event_time,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "event": event}


@app.post("/api/progress/plan")
def update_progress_plan(payload: ProgressPlanUpdateRequest) -> dict:
    result = replace_plan_items([item.model_dump() for item in payload.items])
    return {"ok": True, **result}


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
            "display_text": text,
            "raw_text": str(row.get("raw_text") or row.get("requirement_text") or text).strip(),
            "text_relation": str(row.get("text_relation") or "distinct").strip() or "distinct",
            "is_deduped": bool(row.get("is_deduped")),
            "parent_section_code": str(row.get("parent_section_code") or "").strip(),
            "parent_section_title": str(row.get("parent_section_title") or "").strip(),
            "parent_section_label": str(row.get("parent_section_label") or "").strip(),
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
            evidences = retrieve_evidence(display_text, knowledge_docs, top_k=3)
            assessment = assess_requirement(
                requirement_text=display_text,
                requirement_type=str(item.get("requirement_type") or "FUNCTIONAL").strip() or "FUNCTIONAL",
                evidences=evidences,
            )
            db.upsert_assessment(
                requirement_id=req["id"],
                ai_decision=assessment["ai_decision"],
                ai_reason=assessment["ai_reason"],
                ai_confidence=float(assessment["ai_confidence"]),
                model_name=assessment["model_name"],
                prompt_version=assessment["prompt_version"],
            )
            db.replace_evidences(requirement_id=req["id"], evidences=evidences)
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
    if all(str(row.get("parent_section_label") or "").strip() for row in rows):
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
    project_id = str(project.get("id") or "")
    document_id = str(document.get("id") or "")
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
            "created_at": document.get("created_at"),
            "updated_at": document.get("updated_at"),
        },
        "summary": {
            "requirement_count": len(presented_requirements),
            "ai_eligible_count": ai_eligible_count,
            "knowledge_doc_count": int(knowledge_docs_count or 0),
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
            and int(knowledge_docs_count or 0) > 0,
        },
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
    ranked: list[tuple[tuple, dict]] = []
    mismatch_count = 0
    for original_index, row in enumerate(requirements):
        preview_rank = _lookup_preview_rank(row, preview_order)
        if preview_order and preview_rank >= 10**9:
            mismatch_count += 1
        page = _coerce_positive_int(row.get("source_page"))
        page_end = _coerce_positive_int(row.get("source_page_end")) or page
        section_key = _natural_sort_key(
            str(row.get("parent_section_code") or row.get("section_code") or row.get("parent_section_label") or "")
        )
        anchor_key = _natural_sort_key(str(row.get("source_anchor") or ""))
        req_code_key = _natural_sort_key(str(row.get("req_code") or ""))
        text_key = _natural_sort_key(str(row.get("raw_text") or row.get("display_text") or row.get("requirement_text") or ""))
        copied = dict(row)
        copied["source_order"] = preview_rank + 1 if preview_rank < 10**9 else 0
        ranked.append(
            (
                (
                    preview_rank,
                    page if page > 0 else 10**9,
                    page_end if page_end > 0 else 10**9,
                    section_key,
                    anchor_key,
                    req_code_key,
                    text_key,
                    original_index,
                ),
                copied,
            )
        )
    ranked.sort(key=lambda item: item[0])
    return [row for _, row in ranked], {
        "order_source": "review_overlay" if preview_order else "fallback_page_anchor",
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


def _is_ai_eligible_requirement(row: dict) -> bool:
    return str(row.get("entry_kind") or "REQUIREMENT").strip().upper() in {"REQUIREMENT", "HYBRID"}


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
