from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import os
import re
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from time import monotonic
from typing import Any, Callable

from reqpilotx_server import db
from reqpilotx_server.adapters.documents.azure_document_intelligence_source_ir_adapter import (
    AZURE_DI_SOURCE_IR_ADAPTER_ID,
    AZURE_DI_SOURCE_IR_ADAPTER_VERSION,
)
from reqpilotx_server.adapters.documents.docx_source_ir_adapter import DocxSourceIRAdapter
from reqpilotx_server.adapters.documents.pdf_source_ir_adapter import (
    PDF_SOURCE_IR_ADAPTER_ID,
    PDF_SOURCE_IR_ADAPTER_VERSION,
    PageStagingAuthorityLostError,
)
from reqpilotx_server.adapters.documents.spreadsheet_visual_profile import (
    LIBREOFFICE_CELL_IMAGE_VISUAL_RENDERER_VERSION,
    LIBREOFFICE_DIRECT_VISUAL_EXPECTED_VERSION,
    LIBREOFFICE_DIRECT_VISUAL_RENDERER_VERSION,
    SPREADSHEET_VISUAL_PROFILE_SCHEMA_VERSION,
)
from reqpilotx_server.adapters.documents.xlsb_source_ir_adapter import (
    LIBREOFFICE_XLSB_CONVERTER_ID,
    LIBREOFFICE_XLSB_CONVERTER_VERSION,
    XLSB_SOURCE_IR_ADAPTER_ID,
    XLSB_SOURCE_IR_ADAPTER_VERSION,
)
from reqpilotx_server.adapters.documents.xlsx_source_ir_adapter import XlsxSourceIRAdapter
from reqpilotx_server.adapters.persistence.postgres_jobs import JobConflictError
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.extraction import (
    LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
    LlmRequirementDocumentProgress,
    LlmShadowPreviewProgressCancelled,
    LlmShadowPreviewRejected,
    LlmShadowPreviewRejectionCode,
    ShadowPreviewRejected,
    build_extraction_shadow_preview,
    build_llm_requirement_batch_preview,
    build_llm_requirement_document_preview,
)
from reqpilotx_server.application.extraction.contracts import (
    DocumentExtractionCancelRequest,
    DocumentExtractionStartRequest,
    DocumentExtractionVisualRepairRequest,
    LlmDocumentExtractionStartRequest,
)
from reqpilotx_server.application.processing_profiles import (
    CAPABILITY_BAILIAN_OCR,
    CAPABILITY_BAILIAN_LAYOUT,
    get_project_processing_profile,
    record_runtime_capability_failure,
    resolve_processing_plan,
)
from reqpilotx_server.application.extraction.evaluation_contracts import (
    ExtractionShadowPreviewRequest,
)
from reqpilotx_server.application.extraction.llm_provision_batch import (
    LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION,
    MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS,
)
from reqpilotx_server.application.extraction.llm_semantics import (
    MODEL_REQUIREMENT_ASSEMBLER_VERSION,
    MODEL_SEMANTIC_CLASSIFIER_VERSION,
    SEMANTIC_PLANNING_RUNTIME_VERSION,
)
from reqpilotx_server.application.extraction.inspect_structure import (
    STRUCTURE_INSPECTION_RUNTIME_VERSION,
)
from reqpilotx_server.application.extraction.processing_visual_chain import (
    COMPOSABLE_VISUAL_CHAIN_PROVIDER_ID,
    COMPOSABLE_VISUAL_CHAIN_VERSION,
)
from reqpilotx_server.application.extraction.requirement_tree import (
    REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION,
)
from reqpilotx_server.application.extraction.resolve_structure import (
    DEFAULT_STRUCTURE_RESOLVER_VERSION,
)
from reqpilotx_server.application.extraction.source_asset_objects import (
    EXTRACTION_RESULT_ARTIFACT_PUBLICATION_RUNTIME_VERSION,
    SOURCE_ASSET_PUBLICATION_RUNTIME_VERSION,
)
from reqpilotx_server.application.extraction.structure_boundary_rules import (
    STRUCTURE_BOUNDARY_RULESET_VERSION,
)
from reqpilotx_server.application.extraction.structure_presentation import (
    STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION,
)
from reqpilotx_server.application.extraction.table_evidence_projection import (
    TABLE_EVIDENCE_PROJECTION_RUNTIME_VERSION,
)
from reqpilotx_server.application.extraction.table_record_projection import (
    TABLE_RECORD_PROJECTION_RUNTIME_VERSION,
)
from reqpilotx_server.application.extraction.table_row_group_review_index import (
    TABLE_ROW_GROUP_REVIEW_INDEX_SCHEMA_VERSION,
)
from reqpilotx_server.application.transport import (
    ResponseSink,
)
from reqpilotx_server.domain.extraction.llm_requirement_review import (
    LLM_REQUIREMENT_PREDICTION_SCHEMA_VERSION,
    LLM_REQUIREMENT_REVIEW_SCHEMA_VERSION,
    LLM_REQUIREMENT_REVIEW_SESSION_SCHEMA_VERSION,
    LlmRequirementReviewSession,
)
from reqpilotx_server.domain.extraction.processing_profile import (
    CapabilityState,
    ResolvedProcessingPlanV1,
    RunProfileOverrideV1,
    processing_plan_requires_connection,
)
from reqpilotx_server.domain.extraction.structure_revision import (
    StructureRevisionV1,
)
from reqpilotx_server.domain.extraction.source_ir import SOURCE_IR_SCHEMA_VERSION
from reqpilotx_server.infrastructure.runtime_settings import DatabaseBackend
from reqpilotx_server.ports.job_queue import (
    DurableJob,
    JobExecutionContext,
    JobLeaseLostError,
    JobStatus,
    NonRetryableJobError,
)
from reqpilotx_server.ports.llm_requirement_review_repository import (
    LlmRequirementReviewIncompatibleError,
    LlmRequirementReviewRepositoryError,
)
from reqpilotx_server.ports.semantic_provider import (
    CancellableSemanticProviderPort,
    DocumentSessionSemanticProviderPort,
    PreflightSemanticProviderPort,
    SemanticProviderCancelled,
)
from reqpilotx_server.services.ai_copilot import get_runtime_connection_revision
from reqpilotx_server.services.extraction_visual_routes import (
    AuthorizedPdfVisualRuntime,
    ExtractionVisualRouteRejected,
    build_authorized_pdf_visual_runtime,
    extraction_visual_strategy_identity,
    normalize_extraction_visual_strategy_identity,
)
from reqpilotx_server.services.llm_extraction_job_checkpoint import (
    LLM_EXTRACTION_PARTIAL_SCHEMA_VERSION,
    build_llm_extraction_job_checkpoint,
    canonical_llm_extraction_payload_sha256,
    restore_llm_extraction_job_checkpoint,
)
from reqpilotx_server.services.llm_extraction_result_cache import (
    LlmExtractionJobCheckpointCache,
    LlmExtractionResultCache,
)
from reqpilotx_server.services.visual_text_snapshot_cache import (
    VisualTextProviderTelemetryV1,
    VisualTextSnapshotCache,
    VisualTextSnapshotCacheBinding,
)


_LOCAL_CONTENT_ROLE_RUNTIME_VERSION = "local-content-role/v1"
_DOCUMENT_EXTRACTION_EXECUTION_REQUIREMENTS_SCHEMA_VERSION = (
    "document-extraction-execution-requirements/v1"
)

logger = logging.getLogger(__name__)


def _document_extraction_execution_requirements(
    processing_plan: ResolvedProcessingPlanV1,
) -> dict[str, Any]:
    connection_required = processing_plan_requires_connection(processing_plan)
    return {
        "schema_version": (_DOCUMENT_EXTRACTION_EXECUTION_REQUIREMENTS_SCHEMA_VERSION),
        "connection_revision_required": connection_required,
        "required_connection_revision": (
            processing_plan.connection_revision if connection_required else None
        ),
    }


def _validate_document_extraction_execution_requirements(
    payload: dict[str, Any],
    *,
    processing_plan: ResolvedProcessingPlanV1,
) -> dict[str, Any]:
    """Recompute server-owned worker requirements and reject forged payloads.

    Legacy queued jobs predate this projection and remain readable: absence is
    accepted, but every newly enqueued payload includes the exact projection.
    """

    expected = _document_extraction_execution_requirements(processing_plan)
    stored = payload.get("execution_requirements")
    if stored is not None and stored != expected:
        raise NonRetryableJobError(
            error_code="document_extraction_execution_requirements_mismatch",
            message="Durable extraction execution requirements are invalid.",
        )
    return expected


def _document_extraction_worker_fleet_status(
    processing_plan: ResolvedProcessingPlanV1,
) -> dict[str, Any]:
    """Read the redacted, plan-specific durable worker projection."""

    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, _durable_jobs

    requirements = _document_extraction_execution_requirements(processing_plan)
    try:
        durable_jobs = _durable_jobs()
    except RuntimeError:
        durable_jobs = None
    worker_fleet_status = getattr(durable_jobs, "worker_fleet_status", None)
    if not callable(worker_fleet_status):
        in_process_runtime = (
            RUNTIME_SETTINGS.database_backend is not DatabaseBackend.POSTGRESQL
        )
        return {
            "schema_version": "runtime-worker-fleet-status/v1",
            "state": "READY" if in_process_runtime else "UNAVAILABLE",
            "desired_connection_revision": get_runtime_connection_revision(),
            "required_kind": "document.extract",
            "revision_required": requirements["connection_revision_required"],
            "registered_worker_count": 0,
            "live_worker_count": 1 if in_process_runtime else 0,
            "eligible_worker_count": 1 if in_process_runtime else 0,
            "idle_worker_count": 1 if in_process_runtime else 0,
            "active_worker_count": 0,
            "draining_worker_count": 0,
            "stale_revision_worker_count": 0,
            "workers": [],
        }
    fleet = worker_fleet_status(
        desired_revision=get_runtime_connection_revision(),
        required_kind="document.extract",
        plan={
            "processing_plan": processing_plan.model_dump(mode="json"),
            "execution_requirements": requirements,
            "source_revision": str(os.getenv("REQPILOT_RUNTIME_SOURCE_REVISION") or ""),
            "runtime_instance_id": str(
                os.getenv("REQPILOT_LOCAL_RUNTIME_INSTANCE_ID") or ""
            ),
        },
    )
    return fleet if isinstance(fleet, dict) else {}


def _require_document_extraction_worker_fleet(
    processing_plan: ResolvedProcessingPlanV1,
) -> dict[str, Any]:
    fleet = _document_extraction_worker_fleet_status(processing_plan)
    requirements = _document_extraction_execution_requirements(processing_plan)
    revision_required = bool(requirements["connection_revision_required"])
    api_revision = get_runtime_connection_revision()
    configuration_superseded = bool(
        revision_required and processing_plan.connection_revision != api_revision
    )
    try:
        eligible_count = max(0, int(fleet.get("eligible_worker_count") or 0))
        live_count = max(0, int(fleet.get("live_worker_count") or 0))
    except (TypeError, ValueError):
        eligible_count = 0
        live_count = 0
    if eligible_count > 0 and not configuration_superseded:
        return fleet
    reason_code = (
        "configuration_changed_during_submission"
        if configuration_superseded
        else "no_live_eligible_worker"
        if live_count == 0
        else "worker_connection_revision_not_acknowledged"
        if revision_required
        else "no_compatible_worker_lane"
    )
    worker_revisions = sorted(
        {
            int(worker.get("connection_revision"))
            for worker in fleet.get("workers") or []
            if isinstance(worker, dict)
            and isinstance(worker.get("connection_revision"), int)
            and not isinstance(worker.get("connection_revision"), bool)
        }
    )
    readiness_state = (
        "CONFIGURATION_SUPERSEDED"
        if configuration_superseded
        else "BLOCKED_NO_LIVE_WORKER"
        if live_count == 0
        else "WAITING_FOR_WORKER_SYNC"
    )
    recommended_action = (
        "RETRY_CURRENT_CONFIGURATION"
        if configuration_superseded
        else "WAIT_FOR_WORKER_SYNC"
        if live_count > 0
        else "START_RUNTIME_WORKERS"
    )
    detail = {
        "code": "document_extraction_worker_fleet_not_ready",
        "reason_code": reason_code,
        "message": (
            "AI connection settings changed while this extraction was being "
            "submitted. Retry to freeze the current configuration."
            if configuration_superseded
            else "No live extraction worker has acknowledged the current connection "
            "revision. Wait for worker synchronization before retrying."
            if revision_required
            else "No live worker can currently execute this extraction plan."
        ),
        "execution_readiness": {
            "schema_version": "document-extraction-execution-readiness/v1",
            "state": readiness_state,
            "revision_required": revision_required,
            "required_connection_revision": requirements[
                "required_connection_revision"
            ],
            "api_connection_revision": api_revision,
            "worker_fleet_state": str(fleet.get("state") or "UNAVAILABLE"),
            "retryable": True,
            "recommended_action": recommended_action,
        },
        "error_context": {
            "failure_phase": "PRE_EXECUTION",
            "pages_started": 0,
            "submitted_connection_revision": (
                processing_plan.connection_revision if revision_required else None
            ),
            "api_connection_revision": api_revision,
            "worker_connection_revisions": worker_revisions,
            "recommended_action": recommended_action,
        },
        "retry_after_ms": 1000,
    }
    raise ApplicationError(
        status_code=503,
        code="document_extraction_worker_fleet_not_ready",
        detail=detail,
    )


def _supersede_stale_pending_document_extraction(
    current: dict[str, Any] | None,
    *,
    replacement_plan: ResolvedProcessingPlanV1,
) -> dict[str, Any] | None:
    """Cancel only an unclaimed remote attempt made obsolete by settings.

    The queue operation is a single PENDING-only compare-and-set. If a worker
    claimed the job after this status snapshot, the CAS returns ``None`` and the
    caller keeps the normal active-plan conflict instead of cancelling work.
    """

    if not isinstance(current, dict) or str(current.get("status") or "").upper() != (
        "PENDING"
    ):
        return current
    raw_plan = current.get("processing_plan")
    try:
        current_plan = ResolvedProcessingPlanV1.model_validate(raw_plan)
    except (TypeError, ValueError):
        return current
    if (
        current_plan.canonical_sha256 == replacement_plan.canonical_sha256
        or not processing_plan_requires_connection(current_plan)
        or current_plan.connection_revision == get_runtime_connection_revision()
    ):
        return current
    job_id = str(current.get("job_id") or "")
    if not job_id:
        return current
    from reqpilotx_server.application.runtime import _durable_jobs

    supersede_pending = getattr(_durable_jobs(), "supersede_pending", None)
    if not callable(supersede_pending):
        return current
    superseded = supersede_pending(
        job_id,
        reason_code="document_extraction_connection_revision_superseded",
    )
    return None if superseded is not None else current


def _document_extraction_execution_status(
    payload: dict[str, Any],
    *,
    status: str,
    error_code: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        processing_plan = ResolvedProcessingPlanV1.model_validate(
            payload.get("processing_plan")
        )
    except (TypeError, ValueError):
        return (
            {
                "schema_version": "document-extraction-execution-readiness/v1",
                "state": "NOT_APPLICABLE",
                "revision_required": False,
                "required_connection_revision": None,
                "api_connection_revision": get_runtime_connection_revision(),
                "worker_fleet_state": "UNAVAILABLE",
                "retryable": False,
                "recommended_action": "NONE",
            },
            None,
        )
    requirements = _document_extraction_execution_requirements(processing_plan)
    revision_required = bool(requirements["connection_revision_required"])
    api_revision = get_runtime_connection_revision()
    normalized_status = str(status or "").upper()
    connection_error = error_code in {
        "document_extraction_connection_revision_mismatch",
        "document_extraction_connection_revision_superseded",
    }
    superseded = bool(
        revision_required
        and processing_plan.connection_revision != api_revision
        and normalized_status in {"PENDING", "FAILED", "CANCELLED"}
    )
    fleet: dict[str, Any] = {}
    if normalized_status == "RUNNING":
        state = "EXECUTING"
        retryable = False
        recommended_action = "NONE"
    elif superseded:
        # A superseded attempt still needs the *current* fleet projection so
        # clients can enable retry as soon as workers acknowledge the API's
        # desired revision. The queue compares workers with desired_revision,
        # not the obsolete plan's required revision.
        fleet = _document_extraction_worker_fleet_status(processing_plan)
        state = "CONFIGURATION_SUPERSEDED"
        retryable = True
        recommended_action = "RETRY_CURRENT_CONFIGURATION"
    elif normalized_status == "PENDING" or connection_error:
        fleet = _document_extraction_worker_fleet_status(processing_plan)
        eligible_count = max(0, int(fleet.get("eligible_worker_count") or 0))
        live_count = max(0, int(fleet.get("live_worker_count") or 0))
        if eligible_count > 0:
            state = "READY"
            recommended_action = (
                "RETRY_CURRENT_CONFIGURATION" if connection_error else "NONE"
            )
        elif live_count > 0:
            state = "WAITING_FOR_WORKER_SYNC"
            recommended_action = "WAIT_FOR_WORKER_SYNC"
        else:
            state = "BLOCKED_NO_LIVE_WORKER"
            recommended_action = "START_RUNTIME_WORKERS"
        retryable = True
    else:
        state = "NOT_APPLICABLE"
        retryable = False
        recommended_action = "NONE"
    readiness = {
        "schema_version": "document-extraction-execution-readiness/v1",
        "state": state,
        "revision_required": revision_required,
        "required_connection_revision": requirements["required_connection_revision"],
        "api_connection_revision": api_revision,
        "worker_fleet_state": str(fleet.get("state") or "UNAVAILABLE"),
        "retryable": retryable,
        "recommended_action": recommended_action,
    }
    if not error_code:
        return readiness, None
    worker_revisions = sorted(
        {
            int(worker.get("connection_revision"))
            for worker in fleet.get("workers") or []
            if isinstance(worker, dict)
            and isinstance(worker.get("connection_revision"), int)
            and not isinstance(worker.get("connection_revision"), bool)
        }
    )
    completed_pages = max(0, int(payload.get("completed_pages") or 0))
    current_page = payload.get("current_page")
    pages_started = max(
        completed_pages,
        1 if isinstance(current_page, int) and current_page > 0 else 0,
    )
    return readiness, {
        "failure_phase": "PRE_EXECUTION" if pages_started == 0 else "EXECUTION",
        "pages_started": pages_started,
        "submitted_connection_revision": (
            processing_plan.connection_revision if revision_required else None
        ),
        "api_connection_revision": api_revision,
        "worker_connection_revisions": worker_revisions,
        "recommended_action": recommended_action,
    }


class _LlmExtractionJobCancelled(LlmShadowPreviewProgressCancelled):
    """Stop a worker whose document/job binding is no longer current."""


class _LlmExtractionVisualStrategyMismatch(RuntimeError):
    """Stop a worker whose authorized visual route no longer matches its job."""


_LLM_EXTRACTION_JOB_LOCK = RLock()

_LLM_EXTRACTION_JOBS: dict[str, dict[str, Any]] = {}

_LLM_EXTRACTION_PREPARATION_HEARTBEAT_SECONDS = 15.0
_DOCUMENT_EXTRACTION_QUEUE_STALL_SECONDS = 45
_DOCUMENT_EXTRACTION_PAGE_PROGRESS_MIN_INTERVAL_SECONDS = 0.2


def _configured_local_worker_capacity(job: object = None) -> int:
    try:
        from reqpilotx_server.application.runtime import RUNTIME_SETTINGS

        value = int(getattr(RUNTIME_SETTINGS, "local_worker_count", 1) or 1)
    except (ImportError, TypeError, ValueError):
        return 1
    capacity = min(max(value, 1), 2)
    payload: dict[str, Any] = {}
    if isinstance(job, DurableJob):
        payload = job.payload
    elif isinstance(job, dict):
        payload = job
    plan = payload.get("processing_plan")
    if not isinstance(plan, dict):
        nested = payload.get("payload")
        plan = nested.get("processing_plan") if isinstance(nested, dict) else None
    llm_provider = str((plan or {}).get("llm_provider") or "").strip().lower()
    if capacity > 1 and llm_provider and llm_provider != "none":
        return 1
    return capacity


_DOCUMENT_EXTRACTION_RESULT_RUNTIME_SCHEMA_VERSION = (
    "document-extraction-result-runtime/v8"
)
_DOCUMENT_EXTRACTION_RESULT_ARTIFACT_SCHEMA_VERSION = (
    "document-extraction-result-artifact/v1"
)
_DURABLE_RESULT_CAS_TOKEN_KEY = "_durable_result_cas_token"
_MAX_DOCUMENT_EXTRACTION_RESULT_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_CACHED_DOCUMENT_EXTRACTION_RESULT_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_CACHED_DOCUMENT_EXTRACTION_RESULT_ARTIFACTS = 2
_DOCUMENT_EXTRACTION_RESULT_ARTIFACT_CACHE_LOCK = RLock()
_DOCUMENT_EXTRACTION_RESULT_ARTIFACT_CACHE: OrderedDict[
    tuple[int, str, str, int],
    tuple[object, bytes],
] = OrderedDict()

_LLM_EXTRACTION_PREPARATION_MESSAGES = {
    "preflight": (
        "正在校验文档身份与输入安全。",
        "Validating document identity and input safety.",
    ),
    "visual_authorization": (
        "正在确认 PDF 视觉识别策略。",
        "Validating the PDF visual recognition strategy.",
    ),
    "source_ir": (
        "正在读取页面、文本、表格、图片与来源坐标。",
        "Reading pages, text, tables, images, and source coordinates.",
    ),
    "visual_validation": (
        "正在核对视觉识别结果与原始页面。",
        "Validating visual observations against the source pages.",
    ),
    "structure_build": (
        "正在构建原文结构树。",
        "Building the source-faithful document structure.",
    ),
    "structure_audit": (
        "正在复核并修正结构关系。",
        "Reviewing and repairing structural relationships.",
    ),
    "agent_source_planning": (
        "正在准备整文页面与来源证据。",
        "Preparing whole-document page and source evidence.",
    ),
    "agent_page_evidence": (
        "正在为 Codex 准备全部授权页面。",
        "Preparing every authorized page for Codex.",
    ),
    "agent_document_understanding": (
        "Codex 正在识别整文结构、原文条目与需求属性。",
        "Codex is recognizing document structure, source entries, and requirement status.",
    ),
    "scope_planning": (
        "正在确定需求提取范围。",
        "Locating the requirement extraction scope.",
    ),
    "scoped_visual_validation": (
        "正在检查提取范围内的视觉完整性。",
        "Checking visual completeness inside the extraction scope.",
    ),
    "semantic_image_planning": (
        "正在准备语义分析所需的页面证据。",
        "Preparing page evidence for semantic analysis.",
    ),
    "batch_planning": (
        "正在准备 Codex 文档条款分析。",
        "Preparing the Codex document analysis.",
    ),
}


def build_sor_extraction_shadow_preview(
    project_id: str,
    document_id: str,
    payload: ExtractionShadowPreviewRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    """Compatibility helper for focused parser tests; not exposed by the product API."""
    from reqpilotx_server.application.extraction.provider_support import (
        _build_llm_shadow_semantic_provider,
    )
    from reqpilotx_server.application.extraction.review_support import (
        _bind_llm_requirement_review_session,
    )
    from reqpilotx_server.application.projects import _require_project
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS, _runtime_record_path

    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document.get("project_id") != project_id:
        raise ApplicationError(
            status_code=404,
            detail="SOR document not found for this project.",
        )

    path = _runtime_record_path(document)
    if not path.exists() or not path.is_file():
        raise ApplicationError(status_code=404, detail="SOR source file not found.")

    response.headers.update(_ASSET_RESPONSE_HEADERS)
    expected_source_sha256 = str(document.get("source_sha256") or "")
    if payload.collection in {
        "llm_requirement_batch",
        "llm_requirement_document",
    }:
        if payload.collection == "llm_requirement_batch" and not payload.section_paths:
            raise ApplicationError(
                status_code=422,
                detail={
                    "code": "llm_shadow_preview_empty_section_paths",
                    "message": "Select at least one server-validated section path.",
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        if payload.collection == "llm_requirement_document" and payload.section_paths:
            raise ApplicationError(
                status_code=422,
                detail={
                    "code": "llm_shadow_preview_unexpected_section_paths",
                    "message": (
                        "Automatic document extraction owns the complete BODY scope."
                    ),
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        if not re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256):
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "llm_document_source_identity_unavailable",
                    "message": (
                        "This document has no trusted stored source identity for "
                        "remote semantic extraction."
                    ),
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        provider = _build_llm_shadow_semantic_provider()
        try:
            shadow = (
                build_llm_requirement_document_preview(
                    path,
                    expected_source_sha256=expected_source_sha256,
                    provider=provider,
                )
                if payload.collection == "llm_requirement_document"
                else build_llm_requirement_batch_preview(
                    path,
                    expected_source_sha256=expected_source_sha256,
                    section_paths=payload.section_paths,
                    provider=provider,
                )
            )
        except LlmShadowPreviewRejected as exc:
            status_code = (
                409
                if exc.code is LlmShadowPreviewRejectionCode.SOURCE_HASH_MISMATCH
                else 422
            )
            raise ApplicationError(
                status_code=status_code,
                detail={
                    "code": exc.code.value,
                    "message": str(exc),
                    "stage": exc.stage,
                    "cause_code": exc.cause_code,
                },
                headers=_ASSET_RESPONSE_HEADERS,
            ) from exc
        shadow = _bind_llm_requirement_review_session(
            project_id=project_id,
            document_id=document_id,
            shadow=shadow,
        )
    else:
        if payload.section_paths:
            raise ApplicationError(
                status_code=422,
                detail={
                    "code": "shadow_preview_unexpected_section_paths",
                    "message": "Section paths are only accepted in LLM preview mode.",
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        try:
            shadow = build_extraction_shadow_preview(
                path,
                expected_source_sha256=expected_source_sha256,
                visual_provider=payload.visual_provider,
                collection=payload.collection,
                offset=payload.offset,
                limit=payload.limit,
            )
        except ShadowPreviewRejected as exc:
            status_code = 409 if exc.code.value == "source_hash_mismatch" else 422
            raise ApplicationError(
                status_code=status_code,
                detail={
                    "code": exc.code.value,
                    "message": str(exc),
                    "stage": exc.stage,
                    "cause_code": exc.cause_code,
                },
                headers=_ASSET_RESPONSE_HEADERS,
            ) from exc

    return {
        "project_id": project_id,
        "document_id": document_id,
        "file_name": str(document.get("file_name") or path.name),
        **shadow,
    }


def _prepare_llm_document_visual_runtime(
    *,
    source: Path,
    source_sha256: str,
    provider_name: str,
) -> tuple[AuthorizedPdfVisualRuntime | None, dict[str, Any]]:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    selected = str(provider_name or "").strip().lower()
    if selected == "off":
        return None, extraction_visual_strategy_identity(None)
    try:
        authorized = build_authorized_pdf_visual_runtime(
            source=source,
            source_sha256=source_sha256,
            provider_name=selected,
        )
    except ExtractionVisualRouteRejected as exc:
        status_code = (
            409
            if exc.code == "visual_route_source_mismatch"
            else 503
            if exc.code
            in {
                "visual_route_registry_unavailable",
                "visual_route_registry_invalid",
                "visual_route_registry_ambiguous",
                "visual_runtime_unavailable",
            }
            else 422
        )
        raise ApplicationError(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc
    return authorized, extraction_visual_strategy_identity(authorized)


def _llm_job_visual_strategy(job: dict[str, Any] | None) -> dict[str, Any]:
    if not job:
        return extraction_visual_strategy_identity(None)
    value = job.get("visual_strategy")
    if value is None:
        return extraction_visual_strategy_identity(None)
    return normalize_extraction_visual_strategy_identity(value)


def _llm_visual_strategy_matches_current_runtime(
    value: Any,
) -> bool:
    try:
        strategy = normalize_extraction_visual_strategy_identity(value)
    except (TypeError, ValueError):
        return False
    adapter_version = str(strategy["source_ir_adapter_version"])
    if strategy["mode"] == "OFF":
        from reqpilotx_server.adapters.documents.azure_document_intelligence_source_ir_adapter import (
            AZURE_DI_SOURCE_IR_ADAPTER_VERSION,
        )

        return bool(
            adapter_version == PDF_SOURCE_IR_ADAPTER_VERSION
            or adapter_version.startswith(f"{PDF_SOURCE_IR_ADAPTER_VERSION}+visual.")
            or adapter_version.startswith(f"{AZURE_DI_SOURCE_IR_ADAPTER_VERSION}+plan.")
        )
    return adapter_version.startswith(f"{PDF_SOURCE_IR_ADAPTER_VERSION}+visual.")


def _source_ir_pipeline_matches_current_runtime(
    source_ir: Any,
    *,
    processing_plan: ResolvedProcessingPlanV1,
    expected_visual_strategy: dict[str, Any] | None = None,
) -> bool:
    """Validate the concrete adapter identity published by a completed result."""

    if not isinstance(source_ir, dict):
        return False
    adapter_id = str(source_ir.get("adapter_id") or "")
    adapter_version = str(source_ir.get("version") or "")
    if not adapter_id or not adapter_version:
        return False
    if adapter_id == PDF_SOURCE_IR_ADAPTER_ID:
        if processing_plan.document.primary_parser != "pymupdf":
            return False
        if expected_visual_strategy is not None:
            expected_version = str(
                expected_visual_strategy.get("source_ir_adapter_version") or ""
            )
            return adapter_version == expected_version
        if (
            processing_plan.document.enhancement_chain
            or processing_plan.document.complex_vision
            or processing_plan.force_visual_pages
        ):
            return bool(
                re.fullmatch(
                    rf"{re.escape(PDF_SOURCE_IR_ADAPTER_VERSION)}"
                    r"\+visual\.[0-9a-f]{12}",
                    adapter_version,
                )
            )
        return adapter_version == PDF_SOURCE_IR_ADAPTER_VERSION
    if adapter_id == AZURE_DI_SOURCE_IR_ADAPTER_ID:
        return bool(
            processing_plan.document.primary_parser == "azure_document_intelligence"
            and adapter_version
            == (
                f"{AZURE_DI_SOURCE_IR_ADAPTER_VERSION}+plan."
                f"{processing_plan.canonical_sha256[:12]}"
            )
        )
    if adapter_id == DocxSourceIRAdapter.adapter_id:
        return bool(
            processing_plan.document.primary_parser == "pymupdf"
            and adapter_version == DocxSourceIRAdapter.adapter_version
        )
    if adapter_id == XlsxSourceIRAdapter.adapter_id:
        return bool(
            processing_plan.document.primary_parser == "pymupdf"
            and adapter_version == XlsxSourceIRAdapter.adapter_version
        )
    if adapter_id == XLSB_SOURCE_IR_ADAPTER_ID:
        return bool(
            processing_plan.document.primary_parser == "pymupdf"
            and re.fullmatch(
                rf"{re.escape(XLSB_SOURCE_IR_ADAPTER_VERSION)}"
                rf"\+lo-{re.escape(LIBREOFFICE_XLSB_CONVERTER_VERSION)}"
                r"\+cfg-[0-9a-f]{16}"
                r"\+derived-[0-9a-f]{64}"
                r"\+visual-[0-9a-f]{40}",
                adapter_version,
            )
            is not None
        )
    return False


def _persist_llm_extraction_job_checkpoint(job: dict[str, Any]) -> None:
    from reqpilotx_server.application.runtime import STORAGE_DIR

    visual_strategy = _llm_job_visual_strategy(job)
    payload = build_llm_extraction_job_checkpoint(
        job,
        preview_schema_version=LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
        pdf_source_ir_adapter_version=str(visual_strategy["source_ir_adapter_version"]),
        structure_resolver_version=DEFAULT_STRUCTURE_RESOLVER_VERSION,
        visual_strategy=visual_strategy,
    )
    LlmExtractionJobCheckpointCache(STORAGE_DIR).save(
        project_id=str(job.get("project_id") or ""),
        document_id=str(job.get("document_id") or ""),
        source_sha256=str(job.get("source_sha256") or ""),
        payload=payload,
    )


def _load_llm_extraction_job_checkpoint(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
) -> dict[str, Any] | None:
    from reqpilotx_server.application.runtime import STORAGE_DIR

    payload = LlmExtractionJobCheckpointCache(STORAGE_DIR).load(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
    )
    if payload is None:
        return None
    restored = restore_llm_extraction_job_checkpoint(
        payload,
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        preview_schema_version=LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
        pdf_source_ir_adapter_version=None,
        structure_resolver_version=DEFAULT_STRUCTURE_RESOLVER_VERSION,
    )
    if restored is None:
        logger.info(
            "Ignoring incompatible LLM extraction job checkpoint. "
            "project_id=%s document_id=%s",
            project_id,
            document_id,
        )
    elif not _llm_visual_strategy_matches_current_runtime(
        restored.get("visual_strategy")
    ):
        logger.info(
            "Ignoring stale LLM extraction visual strategy. "
            "project_id=%s document_id=%s",
            project_id,
            document_id,
        )
        return None
    return restored


def _purge_llm_extraction_job_checkpoints(
    *,
    project_id: str,
    document_id: str,
    best_effort: bool = False,
) -> None:
    from reqpilotx_server.application.runtime import STORAGE_DIR

    try:
        LlmExtractionJobCheckpointCache(STORAGE_DIR).purge_document(
            project_id=project_id,
            document_id=document_id,
        )
    except Exception:
        if not best_effort:
            raise
        logger.warning(
            "LLM extraction job checkpoint cleanup failed. "
            "project_id=%s document_id=%s",
            project_id,
            document_id,
            exc_info=True,
        )


def _interrupted_llm_extraction_job(
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    if str(checkpoint.get("status") or "").upper() == "FAILED":
        return checkpoint
    partial_result = checkpoint.get("partial_result")
    if isinstance(partial_result, dict):
        partial_result = {
            **partial_result,
            "editable": False,
            "recovery": {
                "mode": "INTERRUPTED_CHECKPOINT",
                "message": (
                    "The last process stopped after this read-only partial "
                    "checkpoint. Start a new run to continue."
                ),
            },
        }
    return {
        **checkpoint,
        "status": "INTERRUPTED",
        "stage": "interrupted_after_restart",
        "current_chunk_index": None,
        "current_chunk_target_count": 0,
        "error_code": "llm_document_extraction_interrupted",
        "error_stage": "restart_recovery",
        "error_cause_code": "",
        "partial_result": partial_result,
        "finished_at_utc": "",
    }


def _public_visual_text_provider_event(value: object) -> dict[str, Any] | None:
    """Return the content-free public/durable subset of one provider event."""

    if not isinstance(value, dict):
        return None
    page_number = value.get("page_number")
    request_count = value.get("request_count")
    latency_ms = value.get("latency_ms")
    provider_id = value.get("provider_id")
    provider_version = value.get("provider_version")
    cache_hit = value.get("cache_hit")
    outcome = value.get("outcome")
    updated_at = value.get("updated_at")
    if (
        type(page_number) is not int
        or not 1 <= page_number <= 1_000_000
        or type(request_count) is not int
        or not 0 <= request_count <= 10_000
        or type(latency_ms) is not int
        or not 0 <= latency_ms <= 86_400_000
        or not isinstance(provider_id, str)
        or not 1 <= len(provider_id) <= 160
        or not isinstance(provider_version, str)
        or not 1 <= len(provider_version) <= 240
        or type(cache_hit) is not bool
        or outcome not in {"READY", "FAILED", "CACHE_HIT"}
        or not isinstance(updated_at, str)
        or not 1 <= len(updated_at) <= 64
    ):
        return None
    try:
        parsed_updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed_updated_at.tzinfo is None:
        return None
    return {
        "page_number": page_number,
        "provider_id": provider_id,
        "provider_version": provider_version,
        "request_count": request_count,
        "latency_ms": latency_ms,
        "cache_hit": cache_hit,
        "outcome": outcome,
        "updated_at": updated_at,
    }


def _serialize_llm_extraction_job(job: dict[str, Any] | None) -> dict[str, Any]:
    if not job:
        execution_readiness, _ = _document_extraction_execution_status(
            {},
            status="IDLE",
            error_code="",
        )
        return {
            "status": "IDLE",
            "stage": "idle",
            "job_id": None,
            "progress": {
                "total_clauses": 0,
                "total_targets": 0,
                "total_chunks": 0,
                "completed_chunks": 0,
                "completed_targets": 0,
                "successful_chunks": 0,
                "unresolved_chunks": 0,
                "blocked_chunks": 0,
                "current_chunk_index": None,
                "current_chunk_target_count": 0,
                "completed_pages": 0,
                "total_pages": 0,
                "current_page": None,
                "page_progress_kind": "",
                "page_started_at_utc": "",
            },
            "queue": {
                "state": "NOT_ACTIVE",
                "position": None,
                "ahead_count": 0,
                "running_count": 0,
                "configured_capacity": _configured_local_worker_capacity(),
                "available_slots": _configured_local_worker_capacity(),
                "wait_seconds": 0,
                "can_cancel": False,
                "recoverable": False,
            },
            "error_code": "",
            "error_stage": "",
            "error_cause_code": "",
            "execution_readiness": execution_readiness,
            "error_context": None,
            "created_at_utc": "",
            "started_at_utc": "",
            "finished_at_utc": "",
            "updated_at_utc": "",
            "partial_revision": 0,
            "last_progress": "",
            "last_progress_en": "",
            "visual_strategy": extraction_visual_strategy_identity(None),
            "page_staging": None,
            "latest_visual_page_event": None,
        }
    progress_keys = (
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
        "completed_pages",
        "total_pages",
        "current_page",
        "page_progress_kind",
        "page_started_at_utc",
    )
    progress = {key: job.get(key, 0) for key in progress_keys}
    progress["current_page"] = job.get("current_page")
    progress["page_progress_kind"] = str(job.get("page_progress_kind") or "")
    progress["page_started_at_utc"] = str(job.get("page_started_at_utc") or "")
    error_code = str(job.get("error_code") or "")
    error_cause_code = str(job.get("error_cause_code") or "")
    if error_cause_code == error_code:
        error_cause_code = ""
    execution_readiness = job.get("execution_readiness")
    error_context = job.get("error_context")
    if not isinstance(execution_readiness, dict):
        execution_readiness, computed_error_context = (
            _document_extraction_execution_status(
                job,
                status=str(job.get("status") or "IDLE"),
                error_code=error_code,
            )
        )
        if error_context is None:
            error_context = computed_error_context
    return {
        "status": str(job.get("status") or "IDLE"),
        "stage": str(job.get("stage") or "idle"),
        "job_id": str(job.get("job_id") or "") or None,
        "source_sha256": str(job.get("source_sha256") or ""),
        "progress": progress,
        "queue": (
            copy.deepcopy(job.get("queue"))
            if isinstance(job.get("queue"), dict)
            else {
                "state": "CLAIMED"
                if str(job.get("status") or "").upper() == "RUNNING"
                else "WAITING_FOR_WORKER"
                if str(job.get("status") or "").upper() == "PENDING"
                else "NOT_ACTIVE",
                "position": None,
                "ahead_count": 0,
                "running_count": 0,
                "configured_capacity": _configured_local_worker_capacity(job),
                "available_slots": _configured_local_worker_capacity(job),
                "wait_seconds": 0,
                "can_cancel": str(job.get("status") or "").upper()
                in {"PENDING", "RUNNING", "CANCEL_REQUESTED"},
                "recoverable": str(job.get("status") or "").upper() == "PENDING",
            }
        ),
        "error_code": error_code,
        "error_stage": str(job.get("error_stage") or ""),
        "error_cause_code": error_cause_code,
        "execution_readiness": copy.deepcopy(execution_readiness),
        "error_context": (
            copy.deepcopy(error_context) if isinstance(error_context, dict) else None
        ),
        "created_at_utc": str(job.get("created_at_utc") or ""),
        "started_at_utc": str(job.get("started_at_utc") or ""),
        "finished_at_utc": str(job.get("finished_at_utc") or ""),
        "updated_at_utc": str(job.get("updated_at_utc") or ""),
        "partial_revision": int(job.get("partial_revision") or 0),
        "last_progress": str(job.get("last_progress") or ""),
        "last_progress_en": str(job.get("last_progress_en") or ""),
        "visual_strategy": _llm_job_visual_strategy(job),
        "processing_plan": (
            copy.deepcopy(job.get("processing_plan"))
            if isinstance(job.get("processing_plan"), dict)
            else None
        ),
        "page_staging": None,
        "latest_visual_page_event": _public_visual_text_provider_event(
            job.get("latest_visual_page_event")
        ),
    }


def _build_llm_extraction_job_response(
    project_id: str,
    document_id: str,
    *,
    job: dict[str, Any] | None,
) -> dict[str, Any]:
    result = None
    partial_result = None
    response_job = job
    status = str(job.get("status") or "").upper() if job else ""
    if job and status == "COMPLETED":
        candidate = job.get("result")
        if isinstance(candidate, dict):
            _record_document_runtime_capability_failures(candidate)
            if _llm_extraction_result_matches_runtime(
                candidate,
                expected_visual_strategy=_llm_job_visual_strategy(job),
            ):
                result = _with_matching_llm_requirement_review(
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=str(job.get("source_sha256") or ""),
                    result=candidate,
                )
            else:
                response_job = _incompatible_llm_extraction_job(job)
    elif job and status in {"PENDING", "RUNNING", "INTERRUPTED"}:
        candidate = job.get("partial_result")
        if isinstance(candidate, dict):
            partial_result = candidate
    serialized_job = _serialize_llm_extraction_job(response_job)
    if job and str(job.get("job_id") or ""):
        from reqpilotx_server.application.extraction.source_page_facts import (
            public_document_extraction_page_staging,
        )

        page_staging = public_document_extraction_page_staging(
            project_id=project_id,
            document_id=document_id,
            source_sha256=str(job.get("source_sha256") or ""),
            job_id=str(job.get("job_id") or ""),
        )
        ready_page_count = int(page_staging.get("ready_page_count") or 0)
        completed_page_count = int(job.get("completed_pages") or 0)
        page_staging["completed_page_count"] = completed_page_count
        page_staging["browsable_lag_page_count"] = max(
            0,
            completed_page_count - ready_page_count,
        )
        if str(job.get("page_staging_state") or "") == "DEGRADED":
            page_staging["state"] = "DEGRADED"
            page_staging["error_code"] = str(
                job.get("page_staging_error_code") or "page_staging_persistence_failed"
            )
        if status == "COMPLETED" and isinstance(result, dict):
            formal_snapshot = result.get("source_page_snapshot")
            formal_snapshot_id = (
                str(formal_snapshot.get("snapshot_id") or "")
                if isinstance(formal_snapshot, dict)
                else ""
            )
            if formal_snapshot_id:
                page_staging.update(
                    {
                        "state": "RETIRED_AFTER_FORMAL_PUBLICATION",
                        "ready_page_count": 0,
                        "ready_page_ranges": [],
                        "page_url_template": "",
                        "browsable_lag_page_count": 0,
                        "formal_snapshot_id": formal_snapshot_id,
                    }
                )
        serialized_job["page_staging"] = page_staging
    response = {
        "project_id": project_id,
        "document_id": document_id,
        "job": serialized_job,
        "result": result,
        "partial_result": partial_result,
        "visual_repair": _public_document_extraction_visual_repair(job),
    }
    previous_artifact = _previous_document_extraction_artifact_summary(
        project_id=project_id,
        document_id=document_id,
        current_job=job,
    )
    if previous_artifact is not None:
        response["previous_artifact"] = previous_artifact
    return response


def _public_document_extraction_visual_repair(
    job: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Expose server-derived visual repair eligibility without page authority."""

    if not isinstance(job, dict):
        return None
    status = str(job.get("status") or "").upper()
    active_plan = (
        dict(job.get("visual_repair") or {})
        if isinstance(job.get("visual_repair"), dict)
        else {}
    )
    if (
        status
        in {
            "PENDING",
            "RUNNING",
            "CANCEL_REQUESTED",
            "FAILED",
            "CANCELLED",
        }
        and active_plan
    ):
        failed_pages = _positive_unique_page_numbers(
            active_plan.get("remote_allowed_page_numbers")
        )
        cache_pages = _positive_unique_page_numbers(
            active_plan.get("cache_required_page_numbers")
        )
        return {
            "schema_version": "document-extraction-visual-repair-eligibility/v1",
            "state": (
                "ACTIVE"
                if status in {"PENDING", "RUNNING", "CANCEL_REQUESTED"}
                else "AVAILABLE"
            ),
            "reason_code": (
                ""
                if status in {"PENDING", "RUNNING", "CANCEL_REQUESTED"}
                else "visual_repair_previous_attempt_not_published"
            ),
            "mode": "FAILED_OCR_PAGES_ONLY",
            "base_job_id": str(active_plan.get("base_job_id") or ""),
            "base_job_generation": int(active_plan.get("base_job_generation") or 0),
            "base_artifact_sha256": str(active_plan.get("base_artifact_sha256") or ""),
            "required_page_count": len(cache_pages) + len(failed_pages),
            "ready_page_count": len(cache_pages),
            "failed_page_count": len(failed_pages),
            "failed_page_numbers": list(failed_pages),
            "remote_request_ceiling": len(failed_pages),
            "deep_layout_request_ceiling": 0,
        }
    if status != "COMPLETED" or not isinstance(job.get("result"), dict):
        return None
    result = dict(job["result"])
    plan_payload = result.get("processing_plan")
    try:
        plan = ResolvedProcessingPlanV1.model_validate(plan_payload)
    except (TypeError, ValueError):
        return None
    artifact_identity = _verified_visual_repair_artifact_identity(
        job=job,
        result=result,
        plan=plan,
    )
    if artifact_identity is None:
        return None
    if not result.get("page_processing_receipts"):
        return None
    baseline = _visual_repair_baseline(result)
    if baseline is None:
        return _unsupported_visual_repair("visual_repair_provenance_incomplete")
    failed_pages = tuple(baseline["failed_page_numbers"])
    if not failed_pages:
        return {
            "schema_version": "document-extraction-visual-repair-eligibility/v1",
            "state": "NOT_REQUIRED",
            "reason_code": "",
            "mode": "FAILED_OCR_PAGES_ONLY",
            "required_page_count": len(baseline["routed_page_numbers"]),
            "ready_page_count": len(baseline["cache_required_page_numbers"]),
            "failed_page_count": 0,
            "failed_page_numbers": [],
            "remote_request_ceiling": 0,
            "deep_layout_request_ceiling": 0,
        }
    if not _visual_repair_plan_supported(plan):
        unsupported = _unsupported_visual_repair("visual_repair_plan_unsupported")
        unsupported.update(
            {
                "required_page_count": len(baseline["routed_page_numbers"]),
                "ready_page_count": len(baseline["cache_required_page_numbers"]),
                "failed_page_count": len(failed_pages),
                "failed_page_numbers": list(failed_pages),
            }
        )
        return unsupported
    return {
        "schema_version": "document-extraction-visual-repair-eligibility/v1",
        "state": "AVAILABLE",
        "reason_code": "",
        "mode": "FAILED_OCR_PAGES_ONLY",
        "base_job_id": artifact_identity["job_id"],
        "base_job_generation": artifact_identity["job_generation"],
        "base_artifact_sha256": artifact_identity["artifact_sha256"],
        "required_page_count": len(baseline["routed_page_numbers"]),
        "ready_page_count": len(baseline["cache_required_page_numbers"]),
        "failed_page_count": len(failed_pages),
        "failed_page_numbers": list(failed_pages),
        "remote_request_ceiling": len(failed_pages),
        "deep_layout_request_ceiling": 0,
    }


def _verified_visual_repair_artifact_identity(
    *,
    job: dict[str, Any],
    result: dict[str, Any],
    plan: ResolvedProcessingPlanV1,
) -> dict[str, Any] | None:
    """Bind repair eligibility to one exact immutable durable artifact."""

    project_id = str(job.get("project_id") or "")
    document_id = str(job.get("document_id") or "")
    source_sha256 = str(job.get("source_sha256") or "")
    result_runtime_sha256 = str(result.get("result_runtime_sha256") or "")
    try:
        job_plan = ResolvedProcessingPlanV1.model_validate(job.get("processing_plan"))
    except (TypeError, ValueError):
        return None
    receipt = job.get(_DURABLE_RESULT_CAS_TOKEN_KEY)
    if not isinstance(receipt, dict):
        return None
    try:
        artifact_size = int(receipt.get("size") or 0)
        job_generation = int(job.get("_durable_job_generation") or 0)
    except (TypeError, ValueError):
        return None
    artifact_sha256 = str(receipt.get("sha256") or "")
    if (
        not project_id
        or not document_id
        or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
        or str(result.get("project_id") or "") != project_id
        or str(result.get("document_id") or "") != document_id
        or str(result.get("source_sha256") or "") != source_sha256
        or plan.project_id != project_id
        or plan.document_id != document_id
        or plan.source_sha256 != source_sha256
        or job_plan.canonical_sha256 != plan.canonical_sha256
        or not re.fullmatch(r"[0-9a-f]{64}", result_runtime_sha256)
        or str(job.get("result_runtime_sha256") or "") != result_runtime_sha256
        or receipt.get("schema_version")
        != _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_SCHEMA_VERSION
        or str(receipt.get("project_id") or "") != project_id
        or str(receipt.get("document_id") or "") != document_id
        or str(receipt.get("source_sha256") or "") != source_sha256
        or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256)
        or not str(receipt.get("storage_key") or "")
        or artifact_size < 1
        or artifact_size > _MAX_DOCUMENT_EXTRACTION_RESULT_ARTIFACT_BYTES
        or str(receipt.get("media_type") or "") != "application/json"
        or not re.fullmatch(r"job-[A-Za-z0-9]+", str(job.get("job_id") or ""))
        or job_generation < 1
    ):
        return None
    return {
        "job_id": str(job["job_id"]),
        "job_generation": job_generation,
        "artifact_sha256": artifact_sha256,
    }


def _unsupported_visual_repair(reason_code: str) -> dict[str, Any]:
    return {
        "schema_version": "document-extraction-visual-repair-eligibility/v1",
        "state": "UNSUPPORTED",
        "reason_code": reason_code,
        "mode": "FAILED_OCR_PAGES_ONLY",
        "required_page_count": 0,
        "ready_page_count": 0,
        "failed_page_count": 0,
        "failed_page_numbers": [],
        "remote_request_ceiling": 0,
        "deep_layout_request_ceiling": 0,
    }


def _positive_unique_page_numbers(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    pages = tuple(value)
    if any(
        isinstance(page, bool) or not isinstance(page, int) or page < 1
        for page in pages
    ):
        return ()
    if len(pages) != len(set(pages)):
        return ()
    return tuple(sorted(pages))


def _visual_repair_plan_supported(plan: ResolvedProcessingPlanV1) -> bool:
    return bool(
        plan.document.primary_parser == "pymupdf"
        and plan.document.enhancement_chain == ("bailian_ocr",)
        and plan.document.complex_vision is None
        and plan.llm_provider == "none"
        and plan.provider_bindings.enhancement_providers
        and len(plan.provider_bindings.enhancement_providers) == 1
        and plan.provider_bindings.enhancement_providers[0].startswith(
            "aliyun:bailian-ocr:"
        )
    )


def _visual_repair_baseline(result: dict[str, Any]) -> dict[str, Any] | None:
    raw_receipts = result.get("page_processing_receipts")
    if not isinstance(raw_receipts, list) or not raw_receipts:
        return None
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in raw_receipts:
        if not isinstance(item, dict):
            return None
        page_number = item.get("page_number")
        state = str(item.get("state") or "")
        page_image_sha256 = str(item.get("page_image_sha256") or "")
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
            or page_number in seen
            or state not in {"READY", "DEGRADED"}
            or not re.fullmatch(r"[0-9a-f]{64}", page_image_sha256)
        ):
            return None
        seen.add(page_number)
        normalized.append(
            {
                "page_number": page_number,
                "page_image_sha256": page_image_sha256,
                "state": state,
                "attempted_provider_ids": sorted(
                    str(provider_id)
                    for provider_id in item.get("attempted_provider_ids") or ()
                    if str(provider_id)
                ),
                "adopted_provider_id": str(item.get("adopted_provider_id") or ""),
                "degraded_reason_codes": sorted(
                    str(reason)
                    for reason in item.get("degraded_reason_codes") or ()
                    if str(reason)
                ),
            }
        )
    normalized.sort(key=lambda item: int(item["page_number"]))
    failed_pages = tuple(
        int(item["page_number"]) for item in normalized if item["state"] == "DEGRADED"
    )
    declared_failed = _positive_unique_page_numbers(result.get("degraded_page_numbers"))
    if failed_pages != declared_failed:
        return None
    ready_pages = tuple(
        int(item["page_number"]) for item in normalized if item["state"] == "READY"
    )
    receipt_bytes = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "base_page_receipts_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "routed_page_numbers": tuple(sorted(seen)),
        "cache_required_page_numbers": ready_pages,
        "remote_allowed_page_numbers": failed_pages,
        "failed_page_numbers": failed_pages,
        "page_image_sha256_by_page": {
            str(item["page_number"]): item["page_image_sha256"] for item in normalized
        },
    }


def _previous_document_extraction_artifact_summary(
    *,
    project_id: str,
    document_id: str,
    current_job: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Expose safe availability facts without serving obsolete result bytes."""

    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, _durable_jobs

    if (
        current_job is None
        or RUNTIME_SETTINGS.database_backend is not DatabaseBackend.POSTGRESQL
    ):
        return None
    status = str(current_job.get("status") or "").upper()
    if status not in {
        "PENDING",
        "RUNNING",
        "CANCEL_REQUESTED",
        "FAILED",
        "INTERRUPTED",
        "CANCELLED",
    }:
        return None
    queue = _durable_jobs()
    get_latest_succeeded = getattr(queue, "get_latest_succeeded", None)
    if not callable(get_latest_succeeded):
        return None
    previous = get_latest_succeeded(
        kind="document.extract",
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if (
        previous is None
        or previous.status is not JobStatus.SUCCEEDED
        or previous.job_id == str(current_job.get("job_id") or "")
    ):
        return None
    payload = dict(previous.payload or {})
    receipt = dict(previous.result or {})
    source_sha256 = str(current_job.get("source_sha256") or "")
    available = bool(
        str(payload.get("project_id") or "") == project_id
        and str(payload.get("document_id") or "") == document_id
        and str(payload.get("source_sha256") or "") == source_sha256
        and receipt.get("schema_version")
        == _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_SCHEMA_VERSION
        and re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("sha256") or ""))
        and str(receipt.get("storage_key") or "")
    )
    if not available:
        return None
    previous_runtime = str(payload.get("result_runtime_sha256") or "")
    try:
        previous_plan = ResolvedProcessingPlanV1.model_validate(
            payload.get("processing_plan")
        )
        expected_runtime = _document_extraction_result_runtime_sha256(previous_plan)
    except (TypeError, ValueError):
        expected_runtime = ""
    runtime_compatible = bool(expected_runtime and previous_runtime == expected_runtime)
    return {
        "available": True,
        "job_id": previous.job_id,
        "source_sha256": source_sha256,
        "runtime_compatible": runtime_compatible,
        "read_mode": "READ_ONLY" if runtime_compatible else "HISTORICAL_ONLY",
        "can_browse_structure": runtime_compatible,
        "can_continue_structure": False,
        "published_at_utc": previous.updated_at.isoformat(),
    }


def _record_document_runtime_capability_failures(
    result: dict[str, Any],
) -> None:
    plan = (
        result.get("processing_plan")
        if isinstance(result.get("processing_plan"), dict)
        else {}
    )
    document = plan.get("document") if isinstance(plan.get("document"), dict) else {}
    enhancement_chain = tuple(document.get("enhancement_chain") or ())
    if not {"bailian_ocr", "bailian_layout"}.intersection(enhancement_chain):
        return
    reasons = {
        str(reason)
        for receipt in result.get("page_processing_receipts") or ()
        if isinstance(receipt, dict)
        for reason in receipt.get("degraded_reason_codes") or ()
    }
    if not any(reason == "aliyun-bailian-qwen-ocr:unavailable" for reason in reasons):
        return
    is_fast_ocr = "bailian_ocr" in enhancement_chain
    record_runtime_capability_failure(
        capability_id=(
            CAPABILITY_BAILIAN_OCR if is_fast_ocr else CAPABILITY_BAILIAN_LAYOUT
        ),
        state=CapabilityState.AUTH_FAILED,
        reason_code=(
            "bailian_ocr_auth_failed" if is_fast_ocr else "bailian_layout_auth_failed"
        ),
        message=(
            "百炼视觉文字服务拒绝了当前凭据；请检查 API Key 与 Base URL "
            "是否属于同一地域和业务空间。"
        ),
    )


def _latest_llm_requirement_review(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
) -> LlmRequirementReviewSession | None:
    from reqpilotx_server.application.extraction.review_support import (
        _llm_requirement_review_repository,
    )

    try:
        return _llm_requirement_review_repository().get_latest(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
    except LlmRequirementReviewIncompatibleError:
        raise
    except (ValueError, LlmRequirementReviewRepositoryError):
        logger.warning(
            "Latest LLM review session could not be loaded. project_id=%s document_id=%s",
            project_id,
            document_id,
            exc_info=True,
        )
        return None


def _with_matching_llm_requirement_review(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.review_support import (
        _llm_requirement_review_repository,
        _serialize_llm_requirement_review_session,
    )

    review_payload = result.get("llm_review")
    prediction_payload = (
        review_payload.get("model_prediction")
        if isinstance(review_payload, dict)
        else None
    )
    prediction_id = (
        str(prediction_payload.get("prediction_id") or "")
        if isinstance(prediction_payload, dict)
        else ""
    )
    if not re.fullmatch(r"llm-prediction-[0-9a-f]{32}", prediction_id):
        return dict(result)
    try:
        matching = _llm_requirement_review_repository().get_for_prediction(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            prediction_id=prediction_id,
        )
    except (ValueError, LlmRequirementReviewRepositoryError):
        logger.warning(
            "Prediction-bound LLM review session could not be loaded. "
            "project_id=%s document_id=%s prediction_id=%s",
            project_id,
            document_id,
            prediction_id,
            exc_info=True,
        )
        return dict(result)
    if matching is None:
        return dict(result)
    return {
        **result,
        "llm_review": _serialize_llm_requirement_review_session(matching),
    }


def _restored_llm_extraction_job(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
) -> dict[str, Any] | None:
    from reqpilotx_server.application.runtime import STORAGE_DIR

    checkpoint = _load_llm_extraction_job_checkpoint(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
    )
    cached = LlmExtractionResultCache(STORAGE_DIR).load(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
    )
    cached_matches_runtime = bool(
        cached is not None and _llm_extraction_cache_matches_runtime(cached)
    )
    checkpoint_final_digest = (
        str(checkpoint.get("final_result_sha256") or "") if checkpoint else ""
    )
    completed_checkpoint_matches_cache = bool(
        checkpoint
        and str(checkpoint.get("status") or "").upper() == "RUNNING"
        and str(checkpoint.get("stage") or "") == "finalizing"
        and checkpoint_final_digest
        and cached_matches_runtime
        and _llm_extraction_result_matches_runtime(
            cached,
            expected_visual_strategy=_llm_job_visual_strategy(checkpoint),
        )
        and canonical_llm_extraction_payload_sha256(cached) == checkpoint_final_digest
    )
    if checkpoint and not completed_checkpoint_matches_cache:
        return _interrupted_llm_extraction_job(checkpoint)
    if completed_checkpoint_matches_cache:
        _purge_llm_extraction_job_checkpoints(
            project_id=project_id,
            document_id=document_id,
            best_effort=True,
        )
    if cached is not None and not cached_matches_runtime:
        cached = None
    if cached is None:
        try:
            latest = _latest_llm_requirement_review(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
            )
        except LlmRequirementReviewIncompatibleError as exc:
            logger.info(
                "Legacy LLM review requires regeneration. "
                "project_id=%s document_id=%s stored_schema=%s",
                project_id,
                document_id,
                exc.stored_schema_version,
            )
            return {
                "job_id": "",
                "project_id": project_id,
                "document_id": document_id,
                "source_sha256": source_sha256,
                "status": "FAILED",
                "stage": "incompatible_review",
                "error_code": "llm_requirement_review_schema_incompatible",
            }
        if latest is None:
            return None
        cached = _session_only_llm_extraction_result(latest)
    counts = cached.get("counts") if isinstance(cached.get("counts"), dict) else {}
    total_chunks = max(
        0,
        int(counts.get("semantic_chunks") or counts.get("provisions") or 0),
    )
    successful_chunks = max(0, int(counts.get("successful_provisions") or 0))
    unresolved_chunks = max(0, int(counts.get("unresolved_provisions") or 0))
    blocked_chunks = max(0, int(counts.get("blocked_provisions") or 0))
    cached_visual_strategy = (
        extraction_visual_strategy_identity(None)
        if cached.get("visual_strategy") is None
        else normalize_extraction_visual_strategy_identity(cached["visual_strategy"])
    )
    return {
        "job_id": "",
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "visual_strategy": cached_visual_strategy,
        "status": "COMPLETED",
        "stage": "restored",
        "total_clauses": max(0, int(counts.get("analyzed_clauses") or 0)),
        "total_targets": max(0, int(counts.get("semantic_annotations") or 0)),
        "total_chunks": total_chunks,
        "completed_chunks": total_chunks,
        "completed_targets": max(0, int(counts.get("semantic_annotations") or 0)),
        "successful_chunks": successful_chunks,
        "unresolved_chunks": unresolved_chunks,
        "blocked_chunks": blocked_chunks,
        "current_chunk_index": None,
        "current_chunk_target_count": 0,
        "error_code": "",
        "error_stage": "",
        "error_cause_code": "",
        "result": cached,
        "created_at_utc": "",
        "started_at_utc": "",
        "finished_at_utc": "",
        "updated_at_utc": "",
    }


def _llm_extraction_result_matches_runtime(
    payload: dict[str, Any],
    *,
    expected_visual_strategy: dict[str, Any] | None = None,
) -> bool:
    if payload.get("schema_version") != LLM_SHADOW_PREVIEW_SCHEMA_VERSION:
        return False
    if (
        payload.get("requirement_tree_projection_runtime_version")
        != REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
    ):
        return False
    if payload.get("mode") == "LOCAL_STRUCTURE_PROJECTION":
        return _local_structure_extraction_result_matches_runtime(payload)
    if payload.get("mode") == "EDITABLE_AGENT_NATIVE_REVIEW":
        return _agent_native_extraction_result_matches_runtime(
            payload,
            expected_visual_strategy=expected_visual_strategy,
        )
    raw_plan = payload.get("processing_plan")
    try:
        processing_plan = ResolvedProcessingPlanV1.model_validate(raw_plan)
    except (TypeError, ValueError):
        return False
    if (
        processing_plan.llm_provider == "none"
        or payload.get("source_sha256") != processing_plan.source_sha256
        or payload.get("result_runtime_sha256")
        != _document_extraction_result_runtime_sha256(processing_plan)
    ):
        return False
    review = payload.get("llm_review")
    prediction = review.get("model_prediction") if isinstance(review, dict) else None
    reviewed_result = (
        review.get("reviewed_result") if isinstance(review, dict) else None
    )
    if (
        not isinstance(review, dict)
        or review.get("schema_version") != LLM_REQUIREMENT_REVIEW_SESSION_SCHEMA_VERSION
        or not isinstance(prediction, dict)
        or prediction.get("schema_version") != LLM_REQUIREMENT_PREDICTION_SCHEMA_VERSION
        or not isinstance(reviewed_result, dict)
        or reviewed_result.get("schema_version")
        != LLM_REQUIREMENT_REVIEW_SCHEMA_VERSION
    ):
        return False
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, dict):
        return False
    source_ir = pipeline.get("source_ir")
    structure = pipeline.get("structure")
    semantic = pipeline.get("semantic")
    if not isinstance(source_ir, dict):
        return False
    raw_visual_strategy = payload.get("visual_strategy")
    if raw_visual_strategy is None:
        if source_ir.get("version") != PDF_SOURCE_IR_ADAPTER_VERSION:
            return False
        actual_visual_strategy = extraction_visual_strategy_identity(None)
    else:
        try:
            actual_visual_strategy = normalize_extraction_visual_strategy_identity(
                raw_visual_strategy
            )
        except (TypeError, ValueError):
            return False
    if not _llm_visual_strategy_matches_current_runtime(actual_visual_strategy):
        return False
    if expected_visual_strategy is not None:
        try:
            normalized_expected = normalize_extraction_visual_strategy_identity(
                expected_visual_strategy
            )
        except (TypeError, ValueError):
            return False
        if actual_visual_strategy != normalized_expected:
            return False
    return (
        _source_ir_pipeline_matches_current_runtime(
            source_ir,
            processing_plan=processing_plan,
            expected_visual_strategy=actual_visual_strategy,
        )
        and source_ir.get("version")
        == actual_visual_strategy["source_ir_adapter_version"]
        and isinstance(structure, dict)
        and structure.get("version") == DEFAULT_STRUCTURE_RESOLVER_VERSION
        and isinstance(semantic, dict)
        and semantic.get("planning_runtime_version")
        == SEMANTIC_PLANNING_RUNTIME_VERSION
        and semantic.get("classifier_runtime_version")
        == MODEL_SEMANTIC_CLASSIFIER_VERSION
        and semantic.get("assembler_runtime_version")
        == MODEL_REQUIREMENT_ASSEMBLER_VERSION
        and semantic.get("document_runtime_version")
        == LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION
    )


def _agent_native_extraction_result_matches_runtime(
    payload: dict[str, Any],
    *,
    expected_visual_strategy: dict[str, Any] | None = None,
) -> bool:
    from reqpilotx_server.application.extraction.agent_native_document import (
        AGENT_NATIVE_DOCUMENT_RUNTIME_VERSION,
    )

    try:
        processing_plan = ResolvedProcessingPlanV1.model_validate(
            payload.get("processing_plan")
        )
    except (TypeError, ValueError):
        return False
    if (
        processing_plan.llm_provider != "codex"
        or payload.get("collection") != "agent_native_requirement_document"
        or payload.get("source_sha256") != processing_plan.source_sha256
        or payload.get("result_runtime_sha256")
        != _document_extraction_result_runtime_sha256(processing_plan)
        or payload.get("llm_review") is not None
    ):
        return False
    pipeline = payload.get("pipeline")
    source_ir = pipeline.get("source_ir") if isinstance(pipeline, dict) else None
    structure = pipeline.get("structure") if isinstance(pipeline, dict) else None
    semantic = pipeline.get("semantic") if isinstance(pipeline, dict) else None
    requirement_view = (
        pipeline.get("requirement_view") if isinstance(pipeline, dict) else None
    )
    llm_batch = payload.get("llm_batch")
    agent_contract = (
        llm_batch.get("agent_contract") if isinstance(llm_batch, dict) else None
    )
    items = payload.get("items")
    requirement_groups = payload.get("requirement_groups")
    review_groups = payload.get("review_groups")
    decomposition = payload.get("requirement_decomposition")
    baseline = payload.get("baseline")
    atomization = payload.get("atomization")
    counts = payload.get("counts")
    raw_visual_strategy = payload.get("visual_strategy")
    try:
        actual_visual_strategy = normalize_extraction_visual_strategy_identity(
            raw_visual_strategy
        )
        normalized_expected = (
            normalize_extraction_visual_strategy_identity(expected_visual_strategy)
            if expected_visual_strategy is not None
            else None
        )
    except (TypeError, ValueError):
        return False
    return bool(
        _llm_visual_strategy_matches_current_runtime(actual_visual_strategy)
        and (
            normalized_expected is None or actual_visual_strategy == normalized_expected
        )
        and _source_ir_pipeline_matches_current_runtime(
            source_ir,
            processing_plan=processing_plan,
            expected_visual_strategy=actual_visual_strategy,
        )
        and isinstance(structure, dict)
        and structure.get("version") == DEFAULT_STRUCTURE_RESOLVER_VERSION
        and structure.get("used_as_agent_boundary") is False
        and isinstance(semantic, dict)
        and semantic.get("version") == AGENT_NATIVE_DOCUMENT_RUNTIME_VERSION
        and semantic.get("legacy_node_lane") == "BYPASSED"
        and isinstance(requirement_view, dict)
        and requirement_view.get("version") == AGENT_NATIVE_DOCUMENT_RUNTIME_VERSION
        and isinstance(llm_batch, dict)
        and llm_batch.get("strategy") == "AGENT_NATIVE_DOCUMENT"
        and llm_batch.get("runtime_version") == AGENT_NATIVE_DOCUMENT_RUNTIME_VERSION
        and llm_batch.get("workflow_mode") == "BASELINE"
        and isinstance(agent_contract, dict)
        and agent_contract.get("authority") == "PAGE_AND_SOURCE_SPANS"
        and agent_contract.get("parser_role") == "READ_ONLY_FACT_TOOL"
        and agent_contract.get("resolver_role") == "READ_ONLY_SUGGESTION_TOOL"
        and agent_contract.get("legacy_scope_used") is False
        and int(agent_contract.get("legacy_scope_node_count") or 0) == 0
        and isinstance(items, list)
        and isinstance(requirement_groups, list)
        and isinstance(review_groups, list)
        and isinstance(decomposition, list)
        and decomposition == []
        and isinstance(baseline, dict)
        and baseline.get("source_text_preserved") is True
        and baseline.get("exact_cover_visible") is True
        and int(baseline.get("entry_count") or 0) == len(review_groups)
        and isinstance(atomization, dict)
        and atomization.get("state") == "NOT_REQUESTED"
        and atomization.get("explicit_trigger_required") is True
        and atomization.get("parent_preserved") is True
        and isinstance(counts, dict)
        and int(counts.get("items") or 0) == len(items)
        and int(counts.get("requirement_groups") or 0) == len(requirement_groups)
        and int(counts.get("review_groups") or 0) == len(review_groups)
        and int(counts.get("legacy_scope_nodes") or 0) == 0
    )


def _local_structure_extraction_result_matches_runtime(
    payload: dict[str, Any],
) -> bool:
    if (
        payload.get("requirement_tree_projection_runtime_version")
        != REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
    ):
        return False
    try:
        plan = ResolvedProcessingPlanV1.model_validate(payload.get("processing_plan"))
    except (TypeError, ValueError):
        return False
    if payload.get(
        "result_runtime_sha256"
    ) != _document_extraction_result_runtime_sha256(plan):
        return False
    pipeline = payload.get("pipeline")
    source_ir = pipeline.get("source_ir") if isinstance(pipeline, dict) else None
    structure = pipeline.get("structure") if isinstance(pipeline, dict) else None
    semantic = pipeline.get("semantic") if isinstance(pipeline, dict) else None
    requirement_view = (
        pipeline.get("requirement_view") if isinstance(pipeline, dict) else None
    )
    items = payload.get("items")
    requirement_groups = payload.get("requirement_groups")
    review_groups = payload.get("review_groups")
    table_record_projections = payload.get("table_record_projections")
    table_evidence_bindings = payload.get("table_evidence_bindings")
    structure_inspection = payload.get("structure_inspection")
    structure_agent = payload.get("structure_agent")
    counts = payload.get("counts")
    semantic_not_run = bool(
        isinstance(structure, dict)
        and isinstance(semantic, dict)
        and semantic.get("status") == "NOT_RUN"
        and semantic.get("authority") == "NONE"
        and semantic.get("origin") == "NONE"
        and semantic.get("id") is None
        and semantic.get("runtime_version") == _LOCAL_CONTENT_ROLE_RUNTIME_VERSION
        and semantic.get("structure_snapshot_id") == structure.get("id")
    )
    requirement_view_not_run = bool(
        isinstance(structure, dict)
        and isinstance(requirement_view, dict)
        and requirement_view.get("status") == "NOT_RUN"
        and requirement_view.get("authority") == "NONE"
        and requirement_view.get("origin") == "NONE"
        and requirement_view.get("runtime_version")
        == _LOCAL_CONTENT_ROLE_RUNTIME_VERSION
        and requirement_view.get("id") is None
        and requirement_view.get("semantic_snapshot_id") is None
        and requirement_view.get("structure_snapshot_id") == structure.get("id")
        and items == []
        and requirement_groups == []
        and review_groups == []
    )
    return bool(
        plan.llm_provider == "none"
        and payload.get("mode") == "LOCAL_STRUCTURE_PROJECTION"
        and payload.get("source_sha256") == plan.source_sha256
        and payload.get("llm_review") is None
        and payload.get("requirement_set_id") is None
        and isinstance(items, list)
        and isinstance(requirement_groups, list)
        and isinstance(review_groups, list)
        and isinstance(table_record_projections, list)
        and isinstance(table_evidence_bindings, list)
        and isinstance(source_ir, dict)
        and str(source_ir.get("id") or "")
        and _source_ir_pipeline_matches_current_runtime(
            source_ir,
            processing_plan=plan,
        )
        and isinstance(structure, dict)
        and structure.get("version") == DEFAULT_STRUCTURE_RESOLVER_VERSION
        and _local_structure_inspection_matches_result(
            structure_inspection,
            structure_agent=structure_agent,
            source_ir=source_ir,
            structure=structure,
        )
        and semantic_not_run
        and requirement_view_not_run
        and isinstance(counts, dict)
        and int(counts.get("items") or 0) == len(items)
        and int(counts.get("requirement_groups") or 0) == len(requirement_groups)
        and int(counts.get("review_groups") or 0) == len(review_groups)
        and int(counts.get("table_record_projections") or 0)
        == len(table_record_projections)
        and int(counts.get("table_evidence_bindings") or 0)
        == len(table_evidence_bindings)
        and int(counts.get("table_records") or 0)
        == sum(
            len(item.get("records") or [])
            for item in table_record_projections
            if isinstance(item, dict)
        )
    )


def _local_structure_inspection_matches_result(
    value: Any,
    *,
    structure_agent: Any,
    source_ir: dict[str, Any],
    structure: dict[str, Any],
) -> bool:
    if not isinstance(value, dict):
        return False
    counts = value.get("counts")
    candidates = value.get("actionable_candidates")
    agent_copy = (
        structure_agent.get("deterministic_inspection")
        if isinstance(structure_agent, dict)
        else None
    )
    if isinstance(agent_copy, dict) and (
        agent_copy.get("inspection_id") != value.get("inspection_id")
        or agent_copy.get("canonical_sha256") != value.get("canonical_sha256")
    ):
        return False
    return bool(
        value.get("schema_version") == "structure-inspection/v1"
        and str(value.get("inspection_id") or "").startswith("structure-inspection-")
        and re.fullmatch(
            r"[0-9a-f]{64}",
            str(value.get("canonical_sha256") or ""),
        )
        and value.get("source_ir_id") == source_ir.get("id")
        and value.get("structure_snapshot_id") == structure.get("base_snapshot_id")
        and value.get("resolver_version") == structure.get("version")
        and value.get("basis") == "BASE_STRUCTURE"
        and int(value.get("basis_revision") or 0) == 0
        and value.get("status")
        in {
            "COMPLETE",
            "REVIEW_REQUIRED",
            "REQUIRES_REEXTRACTION",
            "STALE_AFTER_STRUCTURE_REVISION",
        }
        and isinstance(counts, dict)
        and all(
            isinstance(counts.get(key), int) and counts[key] >= 0
            for key in {
                "candidate_count",
                "observed_actionable_candidate_count",
                "observed_structural_actionable_candidate_count",
                "observed_document_capture_gap_count",
                "observed_ungrounded_source_fact_count",
                "actionable_candidate_count",
                "structural_actionable_candidate_count",
                "capture_gap_count",
                "ungrounded_source_fact_count",
                "suppressed_candidate_count",
            }
        )
        and isinstance(candidates, list)
    )


def _llm_extraction_cache_matches_runtime(payload: dict[str, Any]) -> bool:
    return _llm_extraction_result_matches_runtime(payload)


def _incompatible_llm_extraction_job(
    job: dict[str, Any],
) -> dict[str, Any]:
    return {
        **job,
        "status": "FAILED",
        "stage": "incompatible_result",
        "error_code": "llm_document_extraction_result_schema_incompatible",
        "result": None,
        "partial_result": None,
    }


def _session_only_llm_extraction_result(
    session: LlmRequirementReviewSession,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.review_support import (
        _serialize_llm_requirement_review_session,
    )

    reviewed_nodes = session.reviewed_result.nodes
    requirement_count = sum(
        node.include_in_view and node.semantic_class.value == "REQUIREMENT"
        for node in reviewed_nodes
    )
    return {
        "schema_version": LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
        "mode": "EDITABLE_LLM_REVIEW",
        "collection": "llm_requirement_document",
        "status": "REVIEW_REQUIRED",
        "serving_impact": "NONE",
        "recovery": {
            "mode": "REVIEW_SESSION_ONLY",
            "message": (
                "The persisted review was restored without its optional full-context cache."
            ),
        },
        "items": [],
        "requirement_groups": [],
        "review_groups": [],
        "tables": [],
        "counts": {
            "recognized_requirement_groups": requirement_count,
            "requirement_groups": requirement_count,
            "review_groups": len(reviewed_nodes),
            "semantic_annotations": len(session.prediction.nodes),
            "unresolved_provisions": None,
            "blocked_provisions": None,
            "coverage_status": "UNKNOWN_WITHOUT_FULL_CONTEXT_CACHE",
        },
        "llm_review": _serialize_llm_requirement_review_session(session),
    }


def _get_llm_extraction_job(
    project_id: str,
    document_id: str,
    *,
    source_sha256: str,
) -> dict[str, Any] | None:
    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, _durable_jobs

    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        durable = _durable_jobs().get_latest(
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id=document_id,
        )
        if durable is None:
            return None
        snapshot = _llm_extraction_snapshot_from_durable(durable)
        if (
            str(snapshot.get("project_id") or "") != project_id
            or str(snapshot.get("source_sha256") or "") != source_sha256
        ):
            return None
        return snapshot
    with _LLM_EXTRACTION_JOB_LOCK:
        job = _LLM_EXTRACTION_JOBS.get(document_id)
        if (
            not job
            or str(job.get("project_id") or "") != project_id
            or str(job.get("source_sha256") or "") != source_sha256
        ):
            return None
        return dict(job)


def _get_latest_successful_llm_extraction_job(
    project_id: str,
    document_id: str,
    *,
    source_sha256: str,
) -> dict[str, Any] | None:
    """Restore the latest immutable successful result for the current source.

    This deliberately does not replace ``_get_llm_extraction_job``: status and
    mutation commands still need the newest attempt.  Read projections may use
    this result only after a newer terminal retry has no usable payload.  The
    caller remains responsible for the normal runtime-schema compatibility
    check before serving a READY workbench.
    """

    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, _durable_jobs

    if RUNTIME_SETTINGS.database_backend is not DatabaseBackend.POSTGRESQL:
        return None
    queue = _durable_jobs()
    get_latest_succeeded = getattr(queue, "get_latest_succeeded", None)
    if not callable(get_latest_succeeded):
        return None
    durable = get_latest_succeeded(
        kind="document.extract",
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if durable is None or durable.status is not JobStatus.SUCCEEDED:
        return None
    snapshot = _llm_extraction_snapshot_from_durable(durable)
    if (
        str(snapshot.get("project_id") or "") != project_id
        or str(snapshot.get("document_id") or document_id) != document_id
        or str(snapshot.get("source_sha256") or "") != source_sha256
        or str(snapshot.get("status") or "").upper() != "COMPLETED"
        or not isinstance(snapshot.get("result"), dict)
    ):
        return None
    return snapshot


def _document_extraction_result_artifact_identity(
    project_id: str,
    document_id: str,
    *,
    source_sha256: str,
) -> str:
    """Return the current immutable result identity without loading its bytes."""

    receipt = _document_extraction_result_artifact_receipt_identity(
        project_id,
        document_id,
        source_sha256=source_sha256,
    )
    return str(receipt.get("artifact_sha256") or "")


def _document_extraction_result_artifact_receipt_identity(
    project_id: str,
    document_id: str,
    *,
    source_sha256: str,
) -> dict[str, Any]:
    """Return a lightweight, exact durable-result receipt for read caching.

    The receipt intentionally includes the serving artifact's durable identity
    and the newest attempt identity.  They differ when a terminal retry falls
    back to the last successful artifact, so callers can reject a stale
    in-process projection without opening or decoding the large result artifact.
    Test/in-memory jobs have no durable receipt and remain uncached.
    """

    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, _durable_jobs

    if RUNTIME_SETTINGS.database_backend is not DatabaseBackend.POSTGRESQL:
        return {}
    queue = _durable_jobs()
    durable = queue.get_latest(
        kind="document.extract",
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if durable is None:
        return {}
    latest_attempt = durable
    if durable.status in {
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.CANCEL_REQUESTED,
    }:
        # An active attempt owns the current read identity.  Reusing an older
        # receipt here could make HTTP caching hide its progress.
        return {}
    if durable.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
        get_latest_succeeded = getattr(queue, "get_latest_succeeded", None)
        if not callable(get_latest_succeeded):
            return {}
        durable = get_latest_succeeded(
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id=document_id,
        )
    if (
        durable is None
        or durable.status is not JobStatus.SUCCEEDED
        or str((durable.payload or {}).get("project_id") or "") != project_id
        or str((durable.payload or {}).get("source_sha256") or "") != source_sha256
        or not isinstance(durable.result, dict)
        or durable.result.get("schema_version")
        != _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_SCHEMA_VERSION
    ):
        return {}
    result = durable.result
    digest = str(result.get("sha256") or "")
    storage_key = str(result.get("storage_key") or "")
    media_type = str(result.get("media_type") or "")
    try:
        size = int(result.get("size") or 0)
    except (TypeError, ValueError):
        return {}
    if (
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not storage_key
        or size < 1
        or size > _MAX_DOCUMENT_EXTRACTION_RESULT_ARTIFACT_BYTES
        or media_type != "application/json"
    ):
        return {}
    return {
        "schema_version": _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_SCHEMA_VERSION,
        "job_id": durable.job_id,
        "job_generation": durable.generation,
        "job_updated_at_utc": durable.updated_at.isoformat(),
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "storage_key": storage_key,
        "artifact_sha256": digest,
        "artifact_size": size,
        "media_type": media_type,
        # The immutable artifact may come from the last successful attempt,
        # while the newest attempt is a terminal failure/cancellation.  Include
        # both identities so byte-level read caches cannot hide latest status.
        "latest_attempt": {
            "job_id": str(getattr(latest_attempt, "job_id", "") or ""),
            "job_generation": int(getattr(latest_attempt, "generation", 0) or 0),
            "job_status": latest_attempt.status.value,
            "job_updated_at_utc": (
                latest_attempt.updated_at.isoformat()
                if getattr(latest_attempt, "updated_at", None) is not None
                else ""
            ),
        },
    }


def _update_llm_extraction_job(
    document_id: str,
    job_id: str,
    *,
    persist_checkpoint: bool = False,
    persist_checkpoint_without_partial: bool = False,
    **updates: Any,
) -> dict[str, Any] | None:
    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS

    with _LLM_EXTRACTION_JOB_LOCK:
        job = _LLM_EXTRACTION_JOBS.get(document_id)
        if not job or str(job.get("job_id") or "") != job_id:
            return None
        job.update(updates)
        job["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        if persist_checkpoint or (
            persist_checkpoint_without_partial
            and int(job.get("partial_revision") or 0) == 0
        ):
            if RUNTIME_SETTINGS.database_backend is DatabaseBackend.SQLITE_TEST:
                _persist_llm_extraction_job_checkpoint(job)
        context = job.get("_durable_context")
        if isinstance(context, JobExecutionContext):
            progress = {
                key: value
                for key, value in job.items()
                if key not in {"_durable_context", "result"}
            }
            durable = context.heartbeat(progress=progress)
            if durable.cancel_requested and str(
                job.get("status") or ""
            ).upper() not in {"COMPLETED", "FAILED", "CANCELLED"}:
                job["status"] = "CANCELLED"
                return None
        return dict(job)


def _record_visual_text_provider_telemetry(
    document_id: str,
    job_id: str,
    event: VisualTextProviderTelemetryV1,
) -> None:
    """Best-effort status projection; never change extraction acceptance."""

    latest_event = _public_visual_text_provider_event(
        {
            "page_number": event.page_number,
            "provider_id": event.provider_id,
            "provider_version": event.provider_version,
            "request_count": event.request_count,
            "latency_ms": event.latency_ms,
            "cache_hit": event.cache_hit,
            "outcome": event.outcome,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    if latest_event is None:
        logger.warning(
            "Visual provider telemetry was not status-safe. document_id=%s job_id=%s",
            document_id,
            job_id,
        )
        return
    try:
        _update_llm_extraction_job(
            document_id,
            job_id,
            persist_checkpoint_without_partial=True,
            latest_visual_page_event=latest_event,
        )
    except Exception:
        # The provider result has already been decided. Observability must not
        # turn a trusted READY/FAILED outcome into an extraction failure, and
        # exception details are intentionally omitted to avoid secret leakage.
        logger.warning(
            "Visual provider telemetry status write failed. document_id=%s job_id=%s",
            document_id,
            job_id,
        )


def _llm_extraction_snapshot_from_durable(job: DurableJob) -> dict[str, Any]:
    progress = dict(job.progress or {})
    payload = dict(job.payload or {})
    durable_result_cas_token = (
        copy.deepcopy(job.result) if isinstance(job.result, dict) else None
    )
    status = {
        JobStatus.PENDING: "PENDING",
        JobStatus.RUNNING: "RUNNING",
        JobStatus.CANCEL_REQUESTED: "CANCEL_REQUESTED",
        JobStatus.SUCCEEDED: "COMPLETED",
        JobStatus.FAILED: "FAILED",
        JobStatus.CANCELLED: "CANCELLED",
    }[job.status]
    result: dict[str, Any] | None = None
    artifact_error_code = ""
    if isinstance(job.result, dict):
        try:
            result = _load_document_extraction_result_artifact(
                dict(job.result),
                expected_project_id=str(payload.get("project_id") or ""),
                expected_document_id=str(payload.get("document_id") or ""),
                expected_source_sha256=str(payload.get("source_sha256") or ""),
            )
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
            logger.exception(
                "Durable extraction result artifact could not be restored. job_id=%s",
                job.job_id,
            )
            artifact_error_code = "document_extraction_result_artifact_unavailable"
            if job.status is JobStatus.SUCCEEDED:
                status = "FAILED"
    error_code = artifact_error_code or job.error_code
    error_cause_code = str(progress.get("error_cause_code") or "")
    if error_cause_code == error_code:
        error_cause_code = ""
    execution_readiness, error_context = _document_extraction_execution_status(
        {**payload, **progress},
        status=status,
        error_code=error_code,
    )
    return {
        **payload,
        **progress,
        "job_id": job.job_id,
        "status": status,
        "queue": _durable_document_extraction_queue_state(job),
        "result": result,
        "error_code": error_code,
        "error_cause_code": error_cause_code,
        "execution_readiness": execution_readiness,
        "error_context": error_context,
        "updated_at_utc": job.updated_at.isoformat(),
        "created_at_utc": job.created_at.isoformat(),
        "started_at_utc": "",
        "finished_at_utc": (
            job.updated_at.isoformat()
            if job.status
            in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
            else ""
        ),
        # Internal only: the expanded result is convenient for readers, but
        # PostgreSQL CAS must compare the exact JSONB value observed in this
        # same durable read. The public serializer is intentionally allowlisted
        # and never exposes this object-storage receipt or legacy inline token.
        _DURABLE_RESULT_CAS_TOKEN_KEY: durable_result_cas_token,
        "_durable_job_generation": int(getattr(job, "generation", 0) or 0),
    }


def _reset_document_extraction_page_progress(job: dict[str, Any]) -> None:
    """Start each durable delivery without progress from an earlier attempt."""

    job.update(
        {
            "completed_pages": 0,
            "total_pages": 0,
            "current_page": None,
            "page_progress_kind": "",
            "page_started_at_utc": "",
            "page_staging_state": "",
            "page_staging_error_code": "",
            "latest_visual_page_event": None,
        }
    )


def _durable_queue_diagnostics(job_id: str) -> dict[str, Any]:
    try:
        from reqpilotx_server.application.runtime import _durable_jobs

        diagnostics = getattr(_durable_jobs(), "queue_diagnostics", None)
        queue = diagnostics(job_id) if callable(diagnostics) else {}
    except Exception:
        logger.warning(
            "Durable extraction queue diagnostics are unavailable. job_id=%s",
            job_id,
            exc_info=True,
        )
        return {}
    return queue if isinstance(queue, dict) else {}


def _durable_document_extraction_queue_state(job: DurableJob) -> dict[str, Any]:
    """Describe durable delivery separately from business execution.

    A PENDING job may be waiting behind another document, waiting for its retry
    delay, or stranded because no worker is claiming the first runnable item.
    Keeping those cases explicit prevents the UI from claiming that parsing is
    already running while the worker has not started it.
    """

    now = datetime.now(timezone.utc)
    created_at = job.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    wait_seconds = max(0, int((now - created_at).total_seconds()))
    progress = dict(job.progress or {})
    can_cancel = bool(
        job.status in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}
        and not bool(progress.get("non_cancellable"))
    )
    metadata: dict[str, Any] = {
        "state": "NOT_ACTIVE",
        "position": None,
        "ahead_count": 0,
        "running_count": 0,
        "configured_capacity": _configured_local_worker_capacity(job),
        "available_slots": _configured_local_worker_capacity(job),
        "wait_seconds": wait_seconds,
        "can_cancel": can_cancel,
        "recoverable": False,
    }
    queue = (
        _durable_queue_diagnostics(job.job_id)
        if job.status
        in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}
        else {}
    )
    reported_running_count = queue.get("running_count")
    eligible_running_count = (
        max(0, int(reported_running_count))
        if isinstance(reported_running_count, int)
        and not isinstance(reported_running_count, bool)
        else None
    )
    if job.status is JobStatus.RUNNING:
        metadata["state"] = "CLAIMED"
        metadata["running_count"] = max(1, eligible_running_count or 0)
        metadata["available_slots"] = max(
            0,
            metadata["configured_capacity"] - metadata["running_count"],
        )
        return metadata
    if job.status is JobStatus.CANCEL_REQUESTED:
        metadata["state"] = "CANCELLING"
        metadata["running_count"] = max(1, eligible_running_count or 0)
        metadata["available_slots"] = max(
            0,
            metadata["configured_capacity"] - metadata["running_count"],
        )
        return metadata
    if job.status is not JobStatus.PENDING:
        return metadata

    available_at = job.available_at
    if available_at.tzinfo is None:
        available_at = available_at.replace(tzinfo=timezone.utc)
    if job.attempts > 0 and available_at > now:
        metadata.update(
            {
                "state": "RETRY_SCHEDULED",
                "retry_available_at_utc": available_at.isoformat(),
                "recoverable": True,
            }
        )
        return metadata
    runnable_since = max(created_at, available_at)
    runnable_wait_seconds = max(0, int((now - runnable_since).total_seconds()))
    metadata["runnable_wait_seconds"] = runnable_wait_seconds

    position = queue.get("position")
    ahead_count = queue.get("ahead_count")
    running_count = queue.get("running_count")
    metadata["position"] = (
        int(position)
        if isinstance(position, int) and not isinstance(position, bool)
        else None
    )
    metadata["ahead_count"] = (
        max(0, int(ahead_count))
        if isinstance(ahead_count, int) and not isinstance(ahead_count, bool)
        else 0
    )
    metadata["running_count"] = (
        max(0, int(running_count))
        if isinstance(running_count, int) and not isinstance(running_count, bool)
        else 0
    )
    metadata["available_slots"] = max(
        0,
        metadata["configured_capacity"] - metadata["running_count"],
    )
    metadata["recoverable"] = True
    if metadata["ahead_count"] > 0:
        metadata["state"] = "WAITING_BEHIND_JOBS"
    elif metadata["running_count"] > 0:
        metadata["state"] = "WAITING_FOR_RUNNING_JOB"
    elif runnable_wait_seconds >= _DOCUMENT_EXTRACTION_QUEUE_STALL_SECONDS:
        metadata["state"] = "WORKER_NOT_CLAIMING"
    else:
        metadata["state"] = "WAITING_FOR_WORKER"
    return metadata


class _LlmExtractionPreparationHeartbeat:
    """Persist a truthful heartbeat while deterministic preparation is running."""

    def __init__(
        self,
        document_id: str,
        job_id: str,
        *,
        interval_seconds: float = _LLM_EXTRACTION_PREPARATION_HEARTBEAT_SECONDS,
    ) -> None:
        self._document_id = document_id
        self._job_id = job_id
        self._interval_seconds = interval_seconds
        self._state_lock = Lock()
        self._stop_event = Event()
        self._stage = "preflight"
        self._page_progress: tuple[str, int, int, int] | None = None
        self._page_started_at_utc = ""
        self._last_page_progress_publish_at = 0.0
        self._thread: Thread | None = None

    def start(self) -> None:
        self.publish(self._stage)
        self._thread = Thread(
            target=self._run,
            name=f"llm-preparation-heartbeat-{self._document_id}",
            daemon=True,
        )
        self._thread.start()

    def publish(self, stage: str) -> None:
        if stage not in _LLM_EXTRACTION_PREPARATION_MESSAGES:
            raise ValueError("unsupported LLM extraction preparation stage")
        with self._state_lock:
            self._stage = stage
            self._page_progress = None
            self._page_started_at_utc = ""
            if self._publish_locked() is None:
                raise _LlmExtractionJobCancelled

    def publish_source_page_progress(
        self,
        completed: int,
        total: int,
        page_number: int,
    ) -> None:
        self._publish_page_progress("SOURCE", completed, total, page_number)

    def publish_visual_capture_progress(
        self,
        completed: int,
        authorized_upper_bound: int,
        page_number: int,
    ) -> None:
        self._publish_page_progress(
            "VISUAL",
            completed,
            authorized_upper_bound,
            page_number,
        )

    def _publish_page_progress(
        self,
        kind: str,
        completed: int,
        total: int,
        page_number: int,
    ) -> None:
        if (
            isinstance(completed, bool)
            or not isinstance(completed, int)
            or completed < (1 if kind == "VISUAL" else 0)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < completed
            or isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
        ):
            raise ValueError("invalid PDF page progress")
        if kind not in {"SOURCE", "VISUAL"}:
            raise ValueError("invalid PDF page progress kind")
        with self._state_lock:
            self._stage = "source_ir"
            previous = self._page_progress
            if previous is None or previous[0] != kind or previous[3] != page_number:
                self._page_started_at_utc = datetime.now(timezone.utc).isoformat()
            self._page_progress = (
                kind,
                completed,
                total,
                page_number,
            )
            now = monotonic()
            final_page = total > 0 and completed >= total
            if (
                not final_page
                and self._last_page_progress_publish_at > 0
                and now - self._last_page_progress_publish_at
                < _DOCUMENT_EXTRACTION_PAGE_PROGRESS_MIN_INTERVAL_SECONDS
            ):
                return
            if self._publish_locked() is None:
                raise _LlmExtractionJobCancelled
            self._last_page_progress_publish_at = now

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, self._interval_seconds + 1.0))

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                with self._state_lock:
                    if self._publish_locked() is None:
                        self._stop_event.set()
                        return
            except Exception:
                logger.warning(
                    "LLM extraction preparation heartbeat stopped unexpectedly. "
                    "document_id=%s job_id=%s",
                    self._document_id,
                    self._job_id,
                    exc_info=True,
                )
                self._stop_event.set()
                return

    def _publish_locked(self) -> dict[str, Any] | None:
        progress_updates: dict[str, Any] = {}
        if self._page_progress is None:
            message, message_en = _LLM_EXTRACTION_PREPARATION_MESSAGES[self._stage]
        else:
            kind, completed, total, page_number = self._page_progress
            progress_updates = {
                "completed_pages": completed,
                "total_pages": total,
                "current_page": page_number,
                "page_progress_kind": kind,
                "page_started_at_utc": self._page_started_at_utc,
            }
            if kind == "VISUAL":
                message = (
                    f"已完成 {completed} 页 PDF 视觉处理"
                    f"（授权上限 {total} 页，当前 P{page_number}）。"
                )
                message_en = (
                    f"Processed {completed} PDF pages "
                    f"(authorized ceiling: {total}; current P{page_number})."
                )
            else:
                message = f"已处理 {completed}/{total} 页，当前第 {page_number} 页。"
                message_en = (
                    f"Processed {completed}/{total} pages; current page {page_number}."
                )
        # Compare and update under the same job lock.  A late heartbeat must
        # never revive a FAILED/COMPLETED job or replace its terminal
        # checkpoint after the worker has left preparation.
        with _LLM_EXTRACTION_JOB_LOCK:
            current = _LLM_EXTRACTION_JOBS.get(self._document_id)
            if (
                not current
                or str(current.get("job_id") or "") != self._job_id
                or str(current.get("status") or "").upper()
                not in {"PENDING", "RUNNING"}
            ):
                return None
            return _update_llm_extraction_job(
                self._document_id,
                self._job_id,
                persist_checkpoint_without_partial=True,
                status="RUNNING",
                stage=self._stage,
                last_progress=message,
                last_progress_en=message_en,
                **progress_updates,
            )


def _llm_extraction_batch_progress_messages(
    progress: LlmRequirementDocumentProgress,
) -> tuple[str, str]:
    if progress.stage == "planned":
        return (
            "文档分析已准备，正在启动 Codex。",
            "The document analysis is ready and Codex is starting.",
        )
    if progress.stage == "completed":
        return (
            "Codex 已完成文档条款分析。",
            "Codex completed the document analysis.",
        )
    if progress.progress_basis == "PAGE_EVIDENCE":
        return (
            (
                "Codex 正在理解整份文档；已处理页面证据 "
                f"{progress.completed_targets}/{progress.total_targets}。"
            ),
            (
                "Codex is understanding the whole document; page evidence processed: "
                f"{progress.completed_targets}/{progress.total_targets}."
            ),
        )
    return (
        "Codex 正在分析文档；已处理来源锚点 "
        f"{progress.completed_targets}/{progress.total_targets}。",
        "Codex is analyzing the document; source anchors processed: "
        f"{progress.completed_targets}/{progress.total_targets}.",
    )


def _record_llm_extraction_progress(
    document_id: str,
    job_id: str,
    progress: LlmRequirementDocumentProgress,
) -> None:
    last_progress, last_progress_en = _llm_extraction_batch_progress_messages(progress)
    updated = _update_llm_extraction_job(
        document_id,
        job_id,
        persist_checkpoint_without_partial=True,
        status="RUNNING",
        stage=progress.stage,
        total_clauses=progress.total_clauses,
        total_targets=progress.total_targets,
        total_chunks=progress.total_chunks,
        completed_chunks=progress.completed_chunks,
        completed_targets=progress.completed_targets,
        successful_chunks=progress.successful_chunks,
        unresolved_chunks=progress.unresolved_chunks,
        blocked_chunks=progress.blocked_chunks,
        current_chunk_index=progress.current_chunk_index,
        current_chunk_target_count=progress.current_chunk_target_count,
        last_progress=last_progress,
        last_progress_en=last_progress_en,
    )
    if updated is None:
        raise _LlmExtractionJobCancelled


def _record_llm_extraction_partial_result(
    document_id: str,
    job_id: str,
    *,
    project_id: str,
    source_sha256: str,
    partial_result: dict[str, Any],
) -> None:
    if not isinstance(partial_result, dict):
        raise TypeError("partial_result must be an object")
    with _LLM_EXTRACTION_JOB_LOCK:
        job = _LLM_EXTRACTION_JOBS.get(document_id)
        if (
            not job
            or str(job.get("job_id") or "") != job_id
            or str(job.get("project_id") or "") != project_id
            or str(job.get("source_sha256") or "") != source_sha256
            or str(job.get("status") or "").upper() not in {"PENDING", "RUNNING"}
        ):
            raise _LlmExtractionJobCancelled
        source_identity = (
            partial_result.get("source_identity")
            if isinstance(partial_result.get("source_identity"), dict)
            else {}
        )
        if (
            str(partial_result.get("schema_version") or "")
            != LLM_EXTRACTION_PARTIAL_SCHEMA_VERSION
            or str(source_identity.get("source_sha256") or "") != source_sha256
        ):
            raise ValueError("partial result identity is invalid")
        expected_visual_strategy = _llm_job_visual_strategy(job)
        partial_visual_strategy = partial_result.get("visual_strategy")
        if partial_visual_strategy is None:
            partial_visual_strategy = expected_visual_strategy
        try:
            normalized_partial_visual_strategy = (
                normalize_extraction_visual_strategy_identity(partial_visual_strategy)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("partial result visual strategy is invalid") from exc
        if normalized_partial_visual_strategy != expected_visual_strategy:
            raise ValueError("partial result visual strategy is invalid")
        revision = int(partial_result.get("revision") or 0)
        expected_revision = int(job.get("partial_revision") or 0) + 1
        coverage = (
            partial_result.get("coverage")
            if isinstance(partial_result.get("coverage"), dict)
            else {}
        )
        completed_indexes = tuple(coverage.get("completed_chunk_indexes") or ())
        previous_indexes = tuple(job.get("partial_completed_chunk_indexes") or ())
        if (
            revision != expected_revision
            or int(coverage.get("completed_chunks") or 0) != revision
            or len(completed_indexes) != revision
            or len(set(completed_indexes)) != revision
            or any(
                isinstance(index, bool) or not isinstance(index, int) or index < 1
                for index in completed_indexes
            )
            or not set(previous_indexes).issubset(completed_indexes)
            or (previous_indexes and set(previous_indexes) == set(completed_indexes))
        ):
            raise ValueError("partial result revision is not monotonic")
        published = {
            **partial_result,
            "project_id": project_id,
            "document_id": document_id,
            "job_id": job_id,
            "source_sha256": source_sha256,
            "revision": revision,
            "editable": False,
            "visual_strategy": expected_visual_strategy,
        }
        job["partial_result"] = published
        job["partial_revision"] = revision
        job["partial_completed_chunk_indexes"] = completed_indexes
        job["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        context = job.get("_durable_context")
        if isinstance(context, JobExecutionContext):
            durable = context.heartbeat(
                progress={
                    key: value
                    for key, value in job.items()
                    if key not in {"_durable_context", "result"}
                }
            )
            if durable.cancel_requested:
                job["status"] = "CANCELLED"
                raise _LlmExtractionJobCancelled
        else:
            _persist_llm_extraction_job_checkpoint(job)


def _reject_review_mutation_while_extraction_runs(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
) -> None:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    job = _get_llm_extraction_job(
        project_id,
        document_id,
        source_sha256=source_sha256,
    )
    if job is None and re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        job = _restored_llm_extraction_job(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
    if job is None:
        return
    status = str(job.get("status") or "").upper()
    if status in {"PENDING", "RUNNING", "CANCEL_REQUESTED"}:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "llm_document_extraction_in_progress",
                "message": (
                    "The current document extraction must finish before "
                    "review revisions can be changed."
                ),
                "reason_code": "structure_derivation_rerun_in_progress",
                "read_only_reason": "structure_derivation_rerun_in_progress",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    result = job.get("result") if status == "COMPLETED" else None
    structure_agent = (
        result.get("structure_agent")
        if isinstance(result, dict) and isinstance(result.get("structure_agent"), dict)
        else None
    )
    recovery = (
        result.get("recovery")
        if isinstance(result, dict) and isinstance(result.get("recovery"), dict)
        else None
    )
    restored_session_only = bool(
        status == "COMPLETED"
        and not str(job.get("job_id") or "")
        and str(job.get("stage") or "") == "restored"
        and isinstance(recovery, dict)
        and recovery.get("mode") == "REVIEW_SESSION_ONLY"
        and result.get("mode") == "EDITABLE_LLM_REVIEW"
    )
    if restored_session_only:
        return
    if status == "COMPLETED" and structure_agent is not None:
        if structure_agent.get("editable") is True:
            return
        read_only_reason = str(
            structure_agent.get("read_only_reason") or "structure_result_not_editable"
        )
    elif status == "COMPLETED":
        read_only_reason = "structure_result_not_editable"
    else:
        read_only_reason = "structure_derivation_rerun_not_complete"
    raise ApplicationError(
        status_code=409,
        detail={
            "code": "structure_result_read_only",
            "message": "Structure mutations require a completed final result.",
            "reason_code": read_only_reason,
            "read_only_reason": read_only_reason,
        },
        headers=_ASSET_RESPONSE_HEADERS,
    )


def _reject_review_mutation_for_stale_structure(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    prediction_structure_snapshot_id: str,
) -> None:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    job = _get_llm_extraction_job(
        project_id,
        document_id,
        source_sha256=source_sha256,
    )
    if job is None and re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        job = _restored_llm_extraction_job(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
    result = (
        job.get("result")
        if isinstance(job, dict)
        and str(job.get("status") or "").upper() == "COMPLETED"
        and isinstance(job.get("result"), dict)
        else None
    )
    agent = (
        result.get("structure_agent")
        if isinstance(result, dict) and isinstance(result.get("structure_agent"), dict)
        else None
    )
    identity = (
        agent.get("effective_structure_identity")
        if isinstance(agent, dict)
        and isinstance(agent.get("effective_structure_identity"), dict)
        else None
    )
    current_snapshot_id = (
        str(identity.get("snapshot_id") or "") if isinstance(identity, dict) else ""
    )
    if current_snapshot_id and prediction_structure_snapshot_id != current_snapshot_id:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "llm_requirement_review_structure_stale",
                "message": (
                    "The review prediction is bound to an obsolete Structure "
                    "revision and must be rerun before editing."
                ),
                "required_effective_snapshot_id": current_snapshot_id,
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )


def _llm_extraction_worker_error_code(exc: Exception) -> str:
    if isinstance(exc, LlmShadowPreviewRejected):
        return exc.code.value
    if isinstance(exc, _LlmExtractionVisualStrategyMismatch):
        return "llm_document_extraction_visual_strategy_mismatch"
    if isinstance(exc, ApplicationError) and isinstance(exc.detail, dict):
        code = str(exc.detail.get("code") or "").strip()
        if re.fullmatch(r"[a-z][a-z0-9_]{0,119}", code):
            return code
    return "llm_document_extraction_worker_failed"


def _llm_extraction_worker_error_details(exc: Exception) -> dict[str, str]:
    details = {
        "error_code": _llm_extraction_worker_error_code(exc),
        "error_stage": "",
        "error_cause_code": "",
    }
    if isinstance(exc, LlmShadowPreviewRejected):
        details["error_stage"] = (
            exc.stage if re.fullmatch(r"[a-z][a-z0-9_]{0,119}", exc.stage) else ""
        )
        details["error_cause_code"] = (
            exc.cause_code
            if re.fullmatch(r"[a-z][a-z0-9_]{0,119}", exc.cause_code)
            else ""
        )
    elif isinstance(exc, ApplicationError) and isinstance(exc.detail, dict):
        for source_key, target_key in (
            ("stage", "error_stage"),
            ("cause_code", "error_cause_code"),
        ):
            value = str(exc.detail.get(source_key) or "")
            if re.fullmatch(r"[a-z][a-z0-9_]{0,119}", value):
                details[target_key] = value
    return details


def _require_llm_document_source_identity(
    document: dict[str, Any],
) -> dict[str, Any]:
    """Require the immutable identity established by the upload boundary."""
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    stored_hash = str(document.get("source_sha256") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", stored_hash):
        return document
    raise ApplicationError(
        status_code=409,
        detail={
            "code": "llm_document_source_identity_unavailable",
            "message": (
                "This document predates the immutable upload identity and must "
                "be migrated or uploaded again before extraction."
            ),
        },
        headers=_ASSET_RESPONSE_HEADERS,
    )


def _visual_repair_cache_binding_kwargs(
    visual_repair: dict[str, Any] | None,
) -> dict[str, Any]:
    if visual_repair is None:
        return {}
    return {
        "cache_required_page_numbers": frozenset(
            _positive_unique_page_numbers(
                visual_repair.get("cache_required_page_numbers")
            )
        ),
        "remote_allowed_page_numbers": frozenset(
            _positive_unique_page_numbers(
                visual_repair.get("remote_allowed_page_numbers")
            )
        ),
    }


def _run_llm_document_extraction_worker(
    *,
    job_id: str,
    project_id: str,
    document: dict[str, Any],
    provider: PreflightSemanticProviderPort,
    processing_plan: ResolvedProcessingPlanV1,
    structure_revisions: tuple[StructureRevisionV1, ...] = (),
    authorized_visual_runtime: AuthorizedPdfVisualRuntime | None = None,
    page_staging_callback: Callable[[dict[str, Any]], None] | None = None,
    visual_repair: dict[str, Any] | None = None,
) -> None:
    from reqpilotx_server.application.extraction.review_support import (
        _bind_llm_requirement_review_session,
    )
    from reqpilotx_server.application.runtime import (
        RUNTIME_SETTINGS,
        STORAGE_DIR,
        _runtime_record_path,
    )

    document_id = str(document.get("id") or "")
    source_sha256 = str(document.get("source_sha256") or "")
    path = _runtime_record_path(document)
    result_runtime_sha256 = _document_extraction_result_runtime_sha256(processing_plan)
    started_at = datetime.now(timezone.utc).isoformat()
    preparation_heartbeat: _LlmExtractionPreparationHeartbeat | None = None
    provider_cancellation_bound = False
    try:
        from reqpilotx_server.application.extraction.processing_pipeline import (
            processing_plan_visual_strategy,
        )

        expected_visual_strategy = processing_plan_visual_strategy(processing_plan)
        with _LLM_EXTRACTION_JOB_LOCK:
            current_job = _LLM_EXTRACTION_JOBS.get(document_id)
            if not current_job or str(current_job.get("job_id") or "") != job_id:
                return
            if _llm_job_visual_strategy(current_job) != expected_visual_strategy:
                raise _LlmExtractionVisualStrategyMismatch
            stored_plan = ResolvedProcessingPlanV1.model_validate(
                current_job.get("processing_plan")
            )
            if stored_plan.canonical_sha256 != processing_plan.canonical_sha256:
                raise RuntimeError(
                    "Processing plan identity changed before extraction."
                )
            if (
                str(current_job.get("result_runtime_sha256") or "")
                != result_runtime_sha256
            ):
                raise RuntimeError("Result runtime identity changed before extraction.")
        if (
            _update_llm_extraction_job(
                document_id,
                job_id,
                persist_checkpoint=True,
                status="RUNNING",
                stage="preflight",
                started_at_utc=started_at,
                error_code="",
                error_stage="",
                error_cause_code="",
            )
            is None
        ):
            return
        preparation_heartbeat = _LlmExtractionPreparationHeartbeat(
            document_id,
            job_id,
        )
        preparation_heartbeat.start()

        def record_progress(progress: LlmRequirementDocumentProgress) -> None:
            assert preparation_heartbeat is not None
            preparation_heartbeat.stop()
            _record_llm_extraction_progress(
                document_id,
                job_id,
                progress,
            )

        def partial_callback(partial_result: dict[str, Any]) -> None:
            _record_llm_extraction_partial_result(
                document_id,
                job_id,
                project_id=project_id,
                source_sha256=source_sha256,
                partial_result=partial_result,
            )

        def provider_telemetry_callback(
            event: VisualTextProviderTelemetryV1,
        ) -> None:
            _record_visual_text_provider_telemetry(document_id, job_id, event)

        if isinstance(provider, CancellableSemanticProviderPort):

            def provider_cancellation_requested() -> bool:
                with _LLM_EXTRACTION_JOB_LOCK:
                    current = _LLM_EXTRACTION_JOBS.get(document_id)
                    if current is None or str(current.get("job_id") or "") != job_id:
                        return True
                    durable = current.get("_durable_context")
                    local_cancelled = str(current.get("status") or "").upper() in {
                        "CANCEL_REQUESTED",
                        "CANCELLED",
                    }
                return local_cancelled or (
                    isinstance(durable, JobExecutionContext)
                    and durable.cancellation_requested()
                )

            provider.set_cancellation_requested(provider_cancellation_requested)
            provider_cancellation_bound = True

        uses_composable_source_ir = bool(
            processing_plan.document.primary_parser != "pymupdf"
            or processing_plan.document.enhancement_chain
            or processing_plan.document.complex_vision
            or processing_plan.force_visual_pages
        )
        if uses_composable_source_ir:
            from reqpilotx_server.application.extraction.build_processing_plan_preview import (
                build_processing_plan_document_preview,
            )

            shadow = build_processing_plan_document_preview(
                path,
                plan=processing_plan,
                provider=provider,
                structure_revisions=structure_revisions,
                max_concurrent_chunks=MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS,
                progress_callback=record_progress,
                preparation_progress_callback=preparation_heartbeat.publish,
                page_progress_callback=(
                    preparation_heartbeat.publish_source_page_progress
                ),
                page_staging_callback=page_staging_callback,
                visual_text_snapshot_cache=VisualTextSnapshotCacheBinding(
                    cache=VisualTextSnapshotCache(STORAGE_DIR),
                    project_id=project_id,
                    document_id=document_id,
                    **_visual_repair_cache_binding_kwargs(visual_repair),
                ),
                provider_telemetry_callback=provider_telemetry_callback,
                partial_result_callback=partial_callback,
                include_source_read_model=True,
            )
        else:
            shadow = build_llm_requirement_document_preview(
                path,
                expected_source_sha256=source_sha256,
                provider=provider,
                structure_revisions=structure_revisions,
                max_concurrent_chunks=MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS,
                progress_callback=record_progress,
                preparation_progress_callback=preparation_heartbeat.publish,
                visual_capture_progress_callback=(
                    preparation_heartbeat.publish_visual_capture_progress
                ),
                partial_result_callback=partial_callback,
                authorized_visual_runtime=authorized_visual_runtime,
                visual_text_snapshot_cache=VisualTextSnapshotCacheBinding(
                    cache=VisualTextSnapshotCache(STORAGE_DIR),
                    project_id=project_id,
                    document_id=document_id,
                    **_visual_repair_cache_binding_kwargs(visual_repair),
                ),
                include_source_read_model=True,
            )
        raw_result_visual_strategy = shadow.get("visual_strategy")
        if raw_result_visual_strategy is None:
            if expected_visual_strategy["mode"] != "OFF":
                raise _LlmExtractionVisualStrategyMismatch
            shadow = {
                **shadow,
                "visual_strategy": expected_visual_strategy,
            }
        else:
            try:
                result_visual_strategy = normalize_extraction_visual_strategy_identity(
                    raw_result_visual_strategy
                )
            except (TypeError, ValueError) as exc:
                raise _LlmExtractionVisualStrategyMismatch from exc
            if result_visual_strategy != expected_visual_strategy:
                raise _LlmExtractionVisualStrategyMismatch
        structure_agent = shadow.get("structure_agent")
        if isinstance(structure_agent, dict):
            structure_agent["processing_plan"] = processing_plan.model_dump(mode="json")
        _retain_structure_revision_provenance(
            shadow,
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            structure_revisions=structure_revisions,
        )
        from reqpilotx_server.application.extraction.source_page_facts import (
            SOURCE_PAGE_PREVIEW_KEY,
            persist_source_page_preview,
        )

        source_page_snapshot = persist_source_page_preview(
            shadow.pop(SOURCE_PAGE_PREVIEW_KEY, None),
            project_id=project_id,
            document_id=document_id,
        )
        if source_page_snapshot is not None:
            shadow["source_page_snapshot"] = source_page_snapshot
        with _LLM_EXTRACTION_JOB_LOCK:
            current = _LLM_EXTRACTION_JOBS.get(document_id)
            if not current or str(current.get("job_id") or "") != job_id:
                return
            shadow = _bind_llm_requirement_review_session(
                project_id=project_id,
                document_id=document_id,
                shadow=shadow,
            )
            result = {
                "project_id": project_id,
                "document_id": document_id,
                "file_name": str(document.get("file_name") or path.name),
                "processing_plan": processing_plan.model_dump(mode="json"),
                **shadow,
                "result_runtime_sha256": result_runtime_sha256,
            }
            final_result_sha256 = canonical_llm_extraction_payload_sha256(result)
            if (
                _update_llm_extraction_job(
                    document_id,
                    job_id,
                    persist_checkpoint=True,
                    status="RUNNING",
                    stage="finalizing",
                    current_chunk_index=None,
                    current_chunk_target_count=0,
                    final_result_sha256=final_result_sha256,
                )
                is None
            ):
                return
            if RUNTIME_SETTINGS.database_backend is DatabaseBackend.SQLITE_TEST:
                LlmExtractionResultCache(STORAGE_DIR).save(
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=source_sha256,
                    payload=result,
                )
            finished_at = datetime.now(timezone.utc).isoformat()
            _update_llm_extraction_job(
                document_id,
                job_id,
                status="COMPLETED",
                stage="completed",
                current_chunk_index=None,
                current_chunk_target_count=0,
                error_code="",
                error_stage="",
                error_cause_code="",
                result=result,
                partial_result=None,
                partial_completed_chunk_indexes=(),
                final_result_sha256="",
                finished_at_utc=finished_at,
            )
            if RUNTIME_SETTINGS.database_backend is DatabaseBackend.SQLITE_TEST:
                _purge_llm_extraction_job_checkpoints(
                    project_id=project_id,
                    document_id=document_id,
                    best_effort=True,
                )
    except SemanticProviderCancelled:
        _update_llm_extraction_job(
            document_id,
            job_id,
            status="CANCELLED",
            stage="cancelled",
            current_chunk_index=None,
            current_chunk_target_count=0,
            error_code="",
            error_stage="",
            error_cause_code="",
            result=None,
            partial_result=None,
            partial_revision=0,
            partial_completed_chunk_indexes=(),
            final_result_sha256="",
            finished_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        return
    except _LlmExtractionJobCancelled:
        return
    except Exception as exc:
        failure_updates = {
            "status": "FAILED",
            "stage": "failed",
            "current_chunk_index": None,
            "current_chunk_target_count": 0,
            **_llm_extraction_worker_error_details(exc),
            "result": None,
            "partial_result": None,
            "partial_revision": 0,
            "partial_completed_chunk_indexes": (),
            "final_result_sha256": "",
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        try:
            updated = _update_llm_extraction_job(
                document_id,
                job_id,
                persist_checkpoint=True,
                **failure_updates,
            )
        except Exception:
            logger.warning(
                "Failed to persist terminal LLM extraction job state. "
                "document_id=%s job_id=%s",
                document_id,
                job_id,
                exc_info=True,
            )
            updated = _update_llm_extraction_job(
                document_id,
                job_id,
                **failure_updates,
            )
        if updated is None:
            return
        logger.exception(
            "Automatic LLM requirement extraction failed. document_id=%s job_id=%s",
            document_id,
            job_id,
        )
    finally:
        if provider_cancellation_bound:
            provider.set_cancellation_requested(None)
        if isinstance(provider, DocumentSessionSemanticProviderPort):
            try:
                provider.close_document_session()
            except Exception:
                logger.warning(
                    "Codex document session cleanup failed. document_id=%s job_id=%s",
                    document_id,
                    job_id,
                    exc_info=True,
                )
        if preparation_heartbeat is not None:
            preparation_heartbeat.stop()


def _run_local_structure_document_extraction_worker(
    *,
    job_id: str,
    project_id: str,
    document: dict[str, Any],
    processing_plan: ResolvedProcessingPlanV1,
    structure_revisions: tuple[StructureRevisionV1, ...] = (),
    page_staging_callback: Callable[[dict[str, Any]], None] | None = None,
    visual_repair: dict[str, Any] | None = None,
) -> None:
    """Produce deterministic rule requirements without constructing an AI client."""

    from reqpilotx_server.application.extraction.processing_pipeline import (
        build_local_structure_document_result,
    )
    from reqpilotx_server.application.extraction.source_page_facts import (
        SOURCE_PAGE_PREVIEW_KEY,
        persist_source_page_preview,
    )
    from reqpilotx_server.application.runtime import (
        RUNTIME_SETTINGS,
        STORAGE_DIR,
        _runtime_record_path,
    )

    document_id = str(document.get("id") or "")
    source_sha256 = str(document.get("source_sha256") or "")
    path = _runtime_record_path(document)
    result_runtime_sha256 = _document_extraction_result_runtime_sha256(processing_plan)
    preparation_heartbeat: _LlmExtractionPreparationHeartbeat | None = None
    try:
        if processing_plan.llm_provider != "none":
            raise RuntimeError("Local-structure worker received a remote LLM plan.")
        if (
            _update_llm_extraction_job(
                document_id,
                job_id,
                persist_checkpoint=True,
                status="RUNNING",
                stage="source_ir",
                started_at_utc=datetime.now(timezone.utc).isoformat(),
                error_code="",
                error_stage="",
                error_cause_code="",
            )
            is None
        ):
            return
        preparation_heartbeat = _LlmExtractionPreparationHeartbeat(
            document_id,
            job_id,
        )
        preparation_heartbeat.start()

        def provider_telemetry_callback(
            event: VisualTextProviderTelemetryV1,
        ) -> None:
            _record_visual_text_provider_telemetry(document_id, job_id, event)

        extracted = build_local_structure_document_result(
            path,
            plan=processing_plan,
            result_runtime_sha256=result_runtime_sha256,
            structure_revisions=structure_revisions,
            include_source_read_model=True,
            page_progress_callback=(preparation_heartbeat.publish_source_page_progress),
            page_staging_callback=page_staging_callback,
            visual_text_snapshot_cache=VisualTextSnapshotCacheBinding(
                cache=VisualTextSnapshotCache(STORAGE_DIR),
                project_id=project_id,
                document_id=document_id,
                **_visual_repair_cache_binding_kwargs(visual_repair),
            ),
            provider_telemetry_callback=provider_telemetry_callback,
        )
        source_page_snapshot = persist_source_page_preview(
            extracted.pop(SOURCE_PAGE_PREVIEW_KEY, None),
            project_id=project_id,
            document_id=document_id,
        )
        result = {
            "project_id": project_id,
            "document_id": document_id,
            "file_name": str(document.get("file_name") or path.name),
            **extracted,
        }
        if source_page_snapshot is not None:
            result["source_page_snapshot"] = source_page_snapshot
        counts = (
            dict(result.get("counts") or {})
            if isinstance(result.get("counts"), dict)
            else {}
        )
        final_result_sha256 = canonical_llm_extraction_payload_sha256(result)
        semantic_annotations = int(counts.get("semantic_annotations") or 0)
        review_group_count = int(counts.get("review_groups") or 0)
        if (
            _update_llm_extraction_job(
                document_id,
                job_id,
                persist_checkpoint=True,
                status="RUNNING",
                stage="finalizing",
                total_clauses=int(counts.get("structure_nodes") or 0),
                total_targets=semantic_annotations,
                total_chunks=0,
                completed_chunks=0,
                completed_targets=semantic_annotations,
                successful_chunks=review_group_count,
                unresolved_chunks=int(counts.get("semantic_unresolved") or 0),
                blocked_chunks=0,
                final_result_sha256=final_result_sha256,
            )
            is None
        ):
            return
        if RUNTIME_SETTINGS.database_backend is DatabaseBackend.SQLITE_TEST:
            LlmExtractionResultCache(STORAGE_DIR).save(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                payload=result,
            )
        _update_llm_extraction_job(
            document_id,
            job_id,
            status="COMPLETED",
            stage="completed",
            result=result,
            final_result_sha256="",
            finished_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        if RUNTIME_SETTINGS.database_backend is DatabaseBackend.SQLITE_TEST:
            _purge_llm_extraction_job_checkpoints(
                project_id=project_id,
                document_id=document_id,
                best_effort=True,
            )
    except _LlmExtractionJobCancelled:
        return
    except Exception as exc:
        updates = {
            "status": "FAILED",
            "stage": "failed",
            **_llm_extraction_worker_error_details(exc),
            "result": None,
            "partial_result": None,
            "partial_revision": 0,
            "partial_completed_chunk_indexes": (),
            "final_result_sha256": "",
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _update_llm_extraction_job(
                document_id,
                job_id,
                persist_checkpoint=True,
                **updates,
            )
        except Exception:
            _update_llm_extraction_job(document_id, job_id, **updates)
        logger.exception(
            "Local-structure document extraction failed. document_id=%s job_id=%s",
            document_id,
            job_id,
        )
    finally:
        if preparation_heartbeat is not None:
            preparation_heartbeat.stop()


def _retain_structure_revision_provenance(
    shadow: dict[str, Any],
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_revisions: tuple[StructureRevisionV1, ...],
) -> None:
    """Carry actor/provider receipts across a no-audit semantic rerun."""

    if not structure_revisions:
        return
    from reqpilotx_server.application.runtime import STORAGE_DIR

    previous = LlmExtractionResultCache(STORAGE_DIR).load(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
    )
    previous_agent = (
        previous.get("structure_agent")
        if isinstance(previous, dict)
        and isinstance(previous.get("structure_agent"), dict)
        else {}
    )
    current_agent = (
        shadow.get("structure_agent")
        if isinstance(shadow.get("structure_agent"), dict)
        else None
    )
    if current_agent is None:
        return
    proposal_ids = {
        revision.patch.accepted_proposal_id
        for revision in structure_revisions
        if revision.patch is not None
    }
    retained = [
        dict(item)
        for item in previous_agent.get("proposal_summaries") or ()
        if isinstance(item, dict) and str(item.get("proposal_id") or "") in proposal_ids
    ]
    if retained:
        current_agent["proposal_summaries"] = retained
        current_agent["patch_summaries"] = [
            {
                "patch_id": revision.patch.patch_id,
                "accepted_proposal_id": revision.patch.accepted_proposal_id,
                "expected_revision_number": revision.patch.expected_revision_number,
                "operations": [
                    operation.operation for operation in revision.patch.operations
                ],
            }
            for revision in structure_revisions
            if revision.patch is not None
        ]
    previous_optimization = (
        previous_agent.get("optimization")
        if isinstance(previous_agent.get("optimization"), dict)
        else None
    )
    retained_revision_ids = {revision.revision_id for revision in structure_revisions}
    if (
        previous_optimization is not None
        and str(previous_optimization.get("revision_id") or "") in retained_revision_ids
    ):
        current_agent["optimization"] = copy.deepcopy(previous_optimization)


def _build_document_extraction_page_staging_callback(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    worker_id: str,
    processing_plan_sha256: str,
    result_runtime_sha256: str,
) -> Callable[[dict[str, Any]], None]:
    """Build a best-effort page UX writer with an authority fail-closed fence."""

    def persist_completed_page(candidate: dict[str, Any]) -> None:
        from reqpilotx_server.application.extraction.source_page_facts import (
            persist_document_extraction_staged_page,
        )

        try:
            persist_document_extraction_staged_page(
                candidate,
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                job_id=job_id,
                attempt_generation=attempt_generation,
                worker_id=worker_id,
                processing_plan_sha256=processing_plan_sha256,
                result_runtime_sha256=result_runtime_sha256,
            )
        except PageStagingAuthorityLostError:
            raise
        except Exception as exc:
            try:
                updated = _update_llm_extraction_job(
                    document_id,
                    job_id,
                    page_staging_state="DEGRADED",
                    page_staging_error_code=_page_staging_error_code(exc),
                )
            except JobLeaseLostError as fence_exc:
                raise PageStagingAuthorityLostError(
                    "Extraction delivery lost its lease while recording page staging degradation."
                ) from fence_exc
            except Exception:
                # A second ordinary status-write failure cannot be allowed to
                # turn this optional read path into a formal extraction gate.
                return
            if updated is None:
                raise PageStagingAuthorityLostError(
                    "Extraction delivery was cancelled while staging a completed page."
                )

    return persist_completed_page


def _page_staging_error_code(exc: Exception) -> str:
    if isinstance(exc, OSError):
        return "page_staging_io_failed"
    if isinstance(exc, (TypeError, ValueError, json.JSONDecodeError)):
        return "page_staging_contract_invalid"
    return "page_staging_persistence_failed"


def run_durable_document_extraction(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.provider_support import (
        _build_llm_shadow_semantic_provider,
    )
    from reqpilotx_server.application.extraction.review_support import _require_project_document

    payload = dict(durable_job.payload or {})
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    document = _require_project_document(project_id, document_id)
    source_sha256 = str(document.get("source_sha256") or "")
    if source_sha256 != str(payload.get("source_sha256") or ""):
        raise NonRetryableJobError(
            error_code="document_extraction_source_binding_mismatch",
            message="Document source identity changed before extraction.",
        )
    try:
        processing_plan = ResolvedProcessingPlanV1.model_validate(
            payload.get("processing_plan")
        )
    except (TypeError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="document_extraction_plan_binding_invalid",
            message="Durable extraction processing plan payload is invalid.",
        ) from exc
    if (
        processing_plan.project_id != project_id
        or processing_plan.document_id != document_id
        or processing_plan.source_sha256 != source_sha256
    ):
        raise NonRetryableJobError(
            error_code="document_extraction_plan_binding_invalid",
            message="Durable extraction processing plan binding is invalid.",
        )
    execution_requirements = _validate_document_extraction_execution_requirements(
        payload,
        processing_plan=processing_plan,
    )
    visual_repair = _validated_visual_repair_execution_plan(
        payload.get("visual_repair"),
        processing_plan=processing_plan,
    )
    if str(payload.get("result_runtime_sha256") or "") != (
        _document_extraction_result_runtime_sha256(processing_plan)
    ):
        raise NonRetryableJobError(
            error_code="document_extraction_runtime_binding_mismatch",
            message=(
                "Durable extraction result runtime binding is obsolete; "
                "start a new extraction for the current runtime."
            ),
        )
    from reqpilotx_server.application.extraction.processing_pipeline import (
        processing_plan_visual_strategy,
    )

    authorized_visual_runtime = None
    visual_strategy = processing_plan_visual_strategy(processing_plan)
    if visual_strategy != payload.get("visual_strategy"):
        raise NonRetryableJobError(
            error_code="document_extraction_visual_strategy_binding_mismatch",
            message="Visual strategy identity changed before extraction.",
        )
    structure_revisions = _validated_structure_revisions_for_document_rerun(
        project_id=project_id,
        document_id=document_id,
        document=document,
        visual_strategy=visual_strategy,
    )
    expected_revision_chain_sha256 = str(
        payload.get("structure_revision_chain_sha256") or ""
    )
    if expected_revision_chain_sha256 and (
        not structure_revisions
        or structure_revisions[-1].revision_chain_sha256
        != expected_revision_chain_sha256
    ):
        raise NonRetryableJobError(
            error_code="document_extraction_structure_revision_binding_mismatch",
            message="Structure revision chain changed before derivation rerun.",
        )
    if (
        execution_requirements["connection_revision_required"]
        and processing_plan.connection_revision != get_runtime_connection_revision()
    ):
        raise NonRetryableJobError(
            error_code="document_extraction_connection_revision_mismatch",
            message=(
                "AI connection settings changed after this extraction was "
                "submitted; start a new extraction with the current configuration."
            ),
        )
    snapshot = _llm_extraction_snapshot_from_durable(durable_job)
    _reset_document_extraction_page_progress(snapshot)
    snapshot["_durable_context"] = context
    snapshot["status"] = "PENDING"
    with _LLM_EXTRACTION_JOB_LOCK:
        _LLM_EXTRACTION_JOBS[document_id] = snapshot
    reset_delivery = context.heartbeat(
        progress={
            key: value
            for key, value in snapshot.items()
            if key not in {"_durable_context", "result"}
        }
    )
    if reset_delivery.cancel_requested:
        with _LLM_EXTRACTION_JOB_LOCK:
            current = _LLM_EXTRACTION_JOBS.get(document_id)
            if current is snapshot:
                _LLM_EXTRACTION_JOBS.pop(document_id, None)
        return {"cancelled": True}
    from reqpilotx_server.application.extraction.source_asset_objects import (
        source_asset_publication_attempt,
    )

    lease_fence = context.lease_fence()
    persist_completed_page = _build_document_extraction_page_staging_callback(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        job_id=lease_fence.job_id,
        attempt_generation=lease_fence.generation,
        worker_id=lease_fence.worker_id,
        processing_plan_sha256=processing_plan.canonical_sha256,
        result_runtime_sha256=str(payload["result_runtime_sha256"]),
    )

    with source_asset_publication_attempt(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        job_id=lease_fence.job_id,
        attempt_generation=lease_fence.generation,
        worker_id=lease_fence.worker_id,
    ) as asset_publication:
        try:
            if processing_plan.llm_provider == "none":
                _run_local_structure_document_extraction_worker(
                    job_id=durable_job.job_id,
                    project_id=project_id,
                    document=document,
                    processing_plan=processing_plan,
                    structure_revisions=structure_revisions,
                    page_staging_callback=persist_completed_page,
                    visual_repair=visual_repair,
                )
            else:
                _run_llm_document_extraction_worker(
                    job_id=durable_job.job_id,
                    project_id=project_id,
                    document=document,
                    provider=_build_llm_shadow_semantic_provider(processing_plan),
                    processing_plan=processing_plan,
                    structure_revisions=structure_revisions,
                    authorized_visual_runtime=authorized_visual_runtime,
                    page_staging_callback=persist_completed_page,
                    visual_repair=visual_repair,
                )
            with _LLM_EXTRACTION_JOB_LOCK:
                completed = dict(_LLM_EXTRACTION_JOBS.get(document_id) or {})
            status = str(completed.get("status") or "").upper()
            if status == "COMPLETED" and isinstance(completed.get("result"), dict):
                _validate_document_extraction_visual_repair_result(
                    payload=payload,
                    result=completed["result"],
                )
                # The result object and asset refs are one publication unit.
                # Close cancellation first so succeed() cannot turn committed
                # refs into a cancelled job after this boundary.
                context.begin_publication()
                result_receipt = _persist_document_extraction_result_artifact(
                    dict(completed["result"]),
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=source_sha256,
                    job_id=lease_fence.job_id,
                    attempt_generation=lease_fence.generation,
                    worker_id=lease_fence.worker_id,
                )
                try:
                    asset_publication.seal()
                except BaseException:
                    from reqpilotx_server.application.extraction.source_asset_objects import (
                        delete_unreferenced_extraction_result_artifacts,
                    )

                    candidates = db.abort_extraction_result_artifact_reference(
                        job_id=lease_fence.job_id,
                        attempt_generation=lease_fence.generation,
                    )
                    delete_unreferenced_extraction_result_artifacts(candidates)
                    raise
                return result_receipt
            if status == "CANCELLED" or context.cancellation_requested():
                return {"cancelled": True}
            raise RuntimeError(
                str(completed.get("error_code") or "document_extraction_failed")
            )
        finally:
            with _LLM_EXTRACTION_JOB_LOCK:
                current = _LLM_EXTRACTION_JOBS.get(document_id)
                if current and str(current.get("job_id") or "") == durable_job.job_id:
                    _LLM_EXTRACTION_JOBS.pop(document_id, None)


def _validate_document_extraction_visual_repair_result(
    *,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> None:
    repair = payload.get("visual_repair")
    if not isinstance(repair, dict):
        return
    baseline_failed = set(
        _positive_unique_page_numbers(repair.get("remote_allowed_page_numbers"))
    )
    baseline_ready = set(
        _positive_unique_page_numbers(repair.get("cache_required_page_numbers"))
    )
    baseline_images = repair.get("page_image_sha256_by_page")
    current = _visual_repair_baseline(result)
    if (
        not baseline_failed
        or not isinstance(baseline_images, dict)
        or current is None
        or set(current["routed_page_numbers"]) != baseline_failed | baseline_ready
        or current["page_image_sha256_by_page"] != baseline_images
    ):
        raise NonRetryableJobError(
            error_code="visual_repair_regression",
            message="Visual repair changed the frozen page evidence identity.",
        )
    current_failed = set(current["failed_page_numbers"])
    if not current_failed.issubset(baseline_failed):
        raise NonRetryableJobError(
            error_code="visual_repair_regression",
            message="Visual repair introduced a new failed page.",
        )
    if current_failed == baseline_failed:
        raise NonRetryableJobError(
            error_code="visual_repair_no_improvement",
            message="Visual repair did not reduce the failed page set.",
        )


def _validated_visual_repair_execution_plan(
    value: object,
    *,
    processing_plan: ResolvedProcessingPlanV1,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not _visual_repair_plan_supported(
        processing_plan
    ):
        raise NonRetryableJobError(
            error_code="visual_repair_plan_unsupported",
            message="The frozen visual repair plan is unsupported.",
        )
    cache_pages = _positive_unique_page_numbers(
        value.get("cache_required_page_numbers")
    )
    remote_pages = _positive_unique_page_numbers(
        value.get("remote_allowed_page_numbers")
    )
    image_bindings = value.get("page_image_sha256_by_page")
    required_keys = {
        "schema_version",
        "policy",
        "base_job_id",
        "base_job_generation",
        "base_artifact_sha256",
        "base_processing_plan_sha256",
        "base_result_runtime_sha256",
        "base_page_receipts_sha256",
        "cache_required_page_numbers",
        "remote_allowed_page_numbers",
        "page_image_sha256_by_page",
        "remote_request_ceiling",
        "deep_layout_request_ceiling",
        "repair_of_job_id",
        "repair_identity_sha256",
    }
    if (
        set(value) != required_keys
        or value.get("schema_version") != "document-extraction-visual-repair-plan/v1"
        or value.get("policy") != "FAILED_OCR_PAGES_ONLY"
        or not remote_pages
        or set(cache_pages).intersection(remote_pages)
        or not isinstance(image_bindings, dict)
        or set(image_bindings) != {str(page) for page in (*cache_pages, *remote_pages)}
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(digest or ""))
            for digest in image_bindings.values()
        )
        or value.get("base_processing_plan_sha256") != processing_plan.canonical_sha256
        or value.get("remote_request_ceiling") != len(remote_pages)
        or value.get("deep_layout_request_ceiling") != 0
    ):
        raise NonRetryableJobError(
            error_code="visual_repair_provenance_incomplete",
            message="The frozen visual repair provenance is invalid.",
        )
    repair_of_job_id = str(value.get("repair_of_job_id") or "")
    repair_identity_sha256 = str(value.get("repair_identity_sha256") or "")
    identity_payload = {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key not in {"repair_of_job_id", "repair_identity_sha256"}
    }
    expected_identity_sha256 = hashlib.sha256(
        json.dumps(
            identity_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        repair_of_job_id != str(value.get("base_job_id") or "")
        or repair_identity_sha256 != expected_identity_sha256
    ):
        raise NonRetryableJobError(
            error_code="visual_repair_provenance_incomplete",
            message="The frozen visual repair identity is invalid.",
        )
    return copy.deepcopy(value)


def _persist_document_extraction_result_artifact(
    result: dict[str, Any],
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str = "",
    attempt_generation: int = 0,
    worker_id: str = "",
    artifact_operation_id: str = "",
    expected_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE

    container = RUNTIME_STATE.container
    if container is None:
        raise RuntimeError("Runtime object storage is not initialized.")
    serialized = json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if (
        not serialized
        or len(serialized) > _MAX_DOCUMENT_EXTRACTION_RESULT_ARTIFACT_BYTES
    ):
        raise RuntimeError(
            "Document extraction result artifact exceeds the size limit."
        )
    digest = hashlib.sha256(serialized).hexdigest()
    storage_key = (
        "runtime/extraction-results/"
        f"{_artifact_key_token(project_id)}/"
        f"{_artifact_key_token(document_id)}/"
        f"{source_sha256}/{digest}.json"
    )
    durable_cas = bool(artifact_operation_id)
    durable_staging = bool(job_id) and not durable_cas
    if durable_cas:
        if not isinstance(expected_result, dict) or not job_id:
            raise RuntimeError("Artifact CAS publication requires its prior receipt.")
        db.stage_extraction_result_artifact_cas_operation(
            operation_id=artifact_operation_id,
            job_id=job_id,
            expected_result=expected_result,
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            storage_key=storage_key,
            artifact_sha256=digest,
            size_bytes=len(serialized),
            media_type="application/json",
        )
    elif durable_staging:
        db.stage_extraction_result_artifact_reference(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            job_id=job_id,
            attempt_generation=attempt_generation,
            worker_id=worker_id,
            storage_key=storage_key,
            artifact_sha256=digest,
            size_bytes=len(serialized),
            media_type="application/json",
        )
    stored = container.object_store.stat(storage_key)
    try:
        if stored is None:
            stored = container.object_store.put(
                key=storage_key,
                source=io.BytesIO(serialized),
                expected_sha256=digest,
                media_type="application/json",
            )
        elif stored.sha256 != digest or int(stored.size) != len(serialized):
            raise RuntimeError(
                "Document extraction result artifact key has different bytes."
            )
        if (
            stored.key != storage_key
            or stored.sha256 != digest
            or int(stored.size) != len(serialized)
        ):
            raise RuntimeError(
                "Document extraction result artifact receipt identity changed."
            )
    except BaseException:
        candidates: tuple[str, ...] = ()
        if durable_cas:
            candidates = db.abort_extraction_result_artifact_cas_operation(
                operation_id=artifact_operation_id
            )
        elif durable_staging:
            candidates = db.abort_extraction_result_artifact_reference(
                job_id=job_id, attempt_generation=attempt_generation
            )
        if candidates:
            from reqpilotx_server.application.extraction.source_asset_objects import (
                delete_unreferenced_extraction_result_artifacts,
            )

            delete_unreferenced_extraction_result_artifacts(candidates)
        raise
    confirmed = True
    if durable_cas:
        confirmed = db.confirm_extraction_result_artifact_cas_operation(
            operation_id=artifact_operation_id,
            storage_key=storage_key,
            artifact_sha256=digest,
            size_bytes=len(serialized),
            media_type="application/json",
        )
    elif durable_staging:
        confirmed = db.confirm_extraction_result_artifact_reference(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            job_id=job_id,
            attempt_generation=attempt_generation,
            worker_id=worker_id,
            storage_key=storage_key,
            artifact_sha256=digest,
            size_bytes=len(serialized),
            media_type="application/json",
        )
    if not confirmed:
        if durable_cas:
            candidates = db.abort_extraction_result_artifact_cas_operation(
                operation_id=artifact_operation_id
            )
        else:
            candidates = db.abort_extraction_result_artifact_reference(
                job_id=job_id, attempt_generation=attempt_generation
            )
        from reqpilotx_server.application.extraction.source_asset_objects import (
            delete_unreferenced_extraction_result_artifacts,
        )

        delete_unreferenced_extraction_result_artifacts(
            tuple(dict.fromkeys((*candidates, storage_key)))
        )
        raise RuntimeError("Extraction result artifact publication lost ownership.")
    return {
        "schema_version": _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "storage_key": stored.key,
        "sha256": stored.sha256,
        "size": stored.size,
        "media_type": "application/json",
    }


def _load_document_extraction_result_artifact(
    receipt: dict[str, Any],
    *,
    expected_project_id: str,
    expected_document_id: str,
    expected_source_sha256: str,
) -> dict[str, Any]:
    # Completed jobs created before the object-backed cutover remain readable;
    # no alternate UI or execution path is retained.
    if (
        receipt.get("schema_version")
        != _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_SCHEMA_VERSION
    ):
        return dict(receipt)

    from reqpilotx_server.application.runtime import RUNTIME_STATE

    project_id = str(receipt.get("project_id") or "")
    document_id = str(receipt.get("document_id") or "")
    source_sha256 = str(receipt.get("source_sha256") or "")
    storage_key = str(receipt.get("storage_key") or "")
    expected_sha256 = str(receipt.get("sha256") or "")
    expected_size = int(receipt.get("size") or 0)
    if (
        project_id != expected_project_id
        or document_id != expected_document_id
        or source_sha256 != expected_source_sha256
        or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or expected_size < 1
        or expected_size > _MAX_DOCUMENT_EXTRACTION_RESULT_ARTIFACT_BYTES
        or not storage_key
    ):
        raise ValueError("Document extraction result artifact receipt is invalid.")
    container = RUNTIME_STATE.container
    if container is None:
        raise RuntimeError("Runtime object storage is not initialized.")
    object_store = container.object_store
    cache_key = (id(object_store), storage_key, expected_sha256, expected_size)
    with _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_CACHE_LOCK:
        cached = _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_CACHE.get(cache_key)
        if cached is not None and cached[0] is object_store:
            serialized = cached[1]
            _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_CACHE.move_to_end(cache_key)
        else:
            stored = object_store.stat(storage_key)
            if (
                stored is None
                or stored.sha256 != expected_sha256
                or stored.size != expected_size
            ):
                raise RuntimeError(
                    "Document extraction result artifact identity mismatch."
                )
            with object_store.open(storage_key) as source:
                serialized = source.read(expected_size + 1)
            if (
                len(serialized) != expected_size
                or hashlib.sha256(serialized).hexdigest() != expected_sha256
            ):
                raise RuntimeError(
                    "Document extraction result artifact verification failed."
                )
            if expected_size <= _MAX_CACHED_DOCUMENT_EXTRACTION_RESULT_ARTIFACT_BYTES:
                _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_CACHE[cache_key] = (
                    object_store,
                    serialized,
                )
                _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_CACHE.move_to_end(cache_key)
                while (
                    len(_DOCUMENT_EXTRACTION_RESULT_ARTIFACT_CACHE)
                    > _MAX_CACHED_DOCUMENT_EXTRACTION_RESULT_ARTIFACTS
                ):
                    _DOCUMENT_EXTRACTION_RESULT_ARTIFACT_CACHE.popitem(last=False)
    payload = json.loads(serialized)
    if not isinstance(payload, dict):
        raise ValueError("Document extraction result artifact must be a JSON object.")
    plan = payload.get("processing_plan")
    plan_source_sha256 = (
        str(plan.get("source_sha256") or "") if isinstance(plan, dict) else ""
    )
    if (
        str(payload.get("project_id") or "") != project_id
        or str(payload.get("document_id") or "") != document_id
        or plan_source_sha256 != source_sha256
    ):
        raise ValueError("Document extraction result artifact binding mismatch.")
    return payload


def _artifact_key_token(value: str) -> str:
    normalized = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", normalized):
        return normalized
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validated_structure_revisions_for_document_rerun(
    *,
    project_id: str,
    document_id: str,
    document: dict[str, Any],
    visual_strategy: dict[str, Any] | None = None,
) -> tuple[StructureRevisionV1, ...]:
    """Recover the append-only Structure lineage before a fresh semantic run."""
    from reqpilotx_server.application.extraction.structure_workspace import (
        _rebuild_structure_revision_context,
    )
    from reqpilotx_server.application.runtime import STORAGE_DIR

    source_sha256 = str(document.get("source_sha256") or "")
    current = _get_llm_extraction_job(
        project_id,
        document_id,
        source_sha256=source_sha256,
    )
    candidates: list[dict[str, Any]] = []
    if (
        isinstance(current, dict)
        and str(current.get("status") or "").upper() == "COMPLETED"
        and isinstance(current.get("result"), dict)
    ):
        candidates.append(current["result"])
    cached = LlmExtractionResultCache(STORAGE_DIR).load(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
    )
    if isinstance(cached, dict):
        candidates.append(cached)
    for result in candidates:
        if not _llm_extraction_result_matches_runtime(
            result,
            expected_visual_strategy=visual_strategy,
        ):
            continue
        agent = (
            result.get("structure_agent")
            if isinstance(result.get("structure_agent"), dict)
            else None
        )
        raw_revisions = agent.get("revision_chain") if isinstance(agent, dict) else None
        if not isinstance(raw_revisions, list) or not raw_revisions:
            return ()
        _, _, revisions = _rebuild_structure_revision_context(
            document=document,
            agent=agent,
        )
        return revisions
    return ()


def _enqueue_durable_llm_document_extraction(
    *,
    project_id: str,
    document: dict[str, Any],
    processing_plan: ResolvedProcessingPlanV1,
    visual_provider: str,
    visual_strategy: dict[str, Any],
    structure_revision_chain_sha256: str = "",
    visual_repair: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS, _durable_jobs

    document_id = str(document.get("id") or "")
    source_sha256 = str(document.get("source_sha256") or "")
    strategy_json = json.dumps(
        visual_strategy,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    strategy_sha256 = hashlib.sha256(strategy_json.encode("utf-8")).hexdigest()
    result_runtime_sha256 = _document_extraction_result_runtime_sha256(processing_plan)
    payload = {
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "processing_plan": processing_plan.model_dump(mode="json"),
        "execution_requirements": (
            _document_extraction_execution_requirements(processing_plan)
        ),
        "result_runtime_sha256": result_runtime_sha256,
        "visual_provider": str(visual_provider or "off"),
        "visual_strategy": visual_strategy,
        "structure_revision_chain_sha256": structure_revision_chain_sha256,
        "stage": "queued",
        "total_clauses": 0,
        "total_targets": 0,
        "total_chunks": 0,
        "completed_chunks": 0,
        "completed_targets": 0,
        "successful_chunks": 0,
        "unresolved_chunks": 0,
        "blocked_chunks": 0,
        "partial_revision": 0,
    }
    if visual_repair is not None:
        payload["visual_repair"] = copy.deepcopy(visual_repair)
    durable_jobs = _durable_jobs()
    base_idempotency_key = _document_extraction_durable_idempotency_key(
        source_sha256=source_sha256,
        processing_plan_sha256=processing_plan.canonical_sha256,
        visual_strategy_sha256=strategy_sha256,
        structure_revision_chain_sha256=structure_revision_chain_sha256,
        result_runtime_sha256=result_runtime_sha256,
    )
    idempotency_key = base_idempotency_key
    latest = durable_jobs.get_latest(
        kind="document.extract",
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if visual_repair is not None:
        repair_identity_sha256 = str(visual_repair.get("repair_identity_sha256") or "")
        repair_of_job_id = str(visual_repair.get("repair_of_job_id") or "")
        if not re.fullmatch(
            r"[0-9a-f]{64}", repair_identity_sha256
        ) or not re.fullmatch(r"job-[A-Za-z0-9]+", repair_of_job_id):
            raise ValueError("Visual repair identity is invalid.")
        idempotency_key = _document_extraction_visual_repair_idempotency_key(
            base_idempotency_key=base_idempotency_key,
            repair_identity_sha256=repair_identity_sha256,
        )
        if latest is not None:
            latest_repair = (
                dict((latest.payload or {}).get("visual_repair") or {})
                if isinstance((latest.payload or {}).get("visual_repair"), dict)
                else {}
            )
            same_repair = bool(
                latest_repair.get("repair_identity_sha256") == repair_identity_sha256
            )
            if latest.status in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.CANCEL_REQUESTED,
            }:
                if same_repair:
                    return _llm_extraction_snapshot_from_durable(latest)
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": "document_extraction_visual_repair_conflict",
                        "message": (
                            "Another extraction attempt is already active for this "
                            "document. Wait for it to finish before retrying failed pages."
                        ),
                    },
                    headers=_ASSET_RESPONSE_HEADERS,
                )
            if latest.status in {JobStatus.FAILED, JobStatus.CANCELLED} and same_repair:
                payload["retry_of_job_id"] = latest.job_id
                idempotency_key = _document_extraction_retry_idempotency_key(
                    base_idempotency_key=idempotency_key,
                    retry_of_job_id=latest.job_id,
                )
            elif (
                latest.status is JobStatus.SUCCEEDED
                and latest.job_id != repair_of_job_id
            ):
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": "document_extraction_visual_repair_stale",
                        "message": (
                            "The completed extraction changed before failed-page repair "
                            "was submitted. Refresh the current result and try again."
                        ),
                    },
                    headers=_ASSET_RESPONSE_HEADERS,
                )
    else:
        lineage_job, lineage_key, lineage_payload = (
            _resolve_document_extraction_retry_lineage(
                durable_jobs,
                base_idempotency_key=base_idempotency_key,
                payload=payload,
            )
        )
        if lineage_job is not None:
            return _llm_extraction_snapshot_from_durable(lineage_job)
        if lineage_key:
            idempotency_key = lineage_key
            payload = lineage_payload
        elif latest is not None and _durable_document_extraction_matches_request(
            latest,
            payload=payload,
        ):
            # Compatibility for queue adapters that predate exact idempotency
            # lookup. Production PostgreSQL follows the complete lineage above.
            if latest.status in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.CANCEL_REQUESTED,
                JobStatus.SUCCEEDED,
            }:
                return _llm_extraction_snapshot_from_durable(latest)
            if latest.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
                payload["retry_of_job_id"] = latest.job_id
                idempotency_key = _document_extraction_retry_idempotency_key(
                    base_idempotency_key=base_idempotency_key,
                    retry_of_job_id=latest.job_id,
                )
    try:
        durable = durable_jobs.enqueue(
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id=document_id,
            idempotency_key=idempotency_key,
            payload=payload,
            priority=10,
            max_attempts=1 if visual_repair is not None else 3,
        )
    except JobConflictError as exc:
        current = durable_jobs.get_latest(
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id=document_id,
        )
        if current is not None:
            current_snapshot = _llm_extraction_snapshot_from_durable(current)
            current_plan = current_snapshot.get("processing_plan")
            if (
                _llm_job_visual_strategy(current_snapshot) == visual_strategy
                and isinstance(current_plan, dict)
                and current_plan.get("canonical_sha256")
                == processing_plan.canonical_sha256
                and current_snapshot.get("result_runtime_sha256")
                == result_runtime_sha256
            ):
                return current_snapshot
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "llm_document_extraction_visual_strategy_conflict",
                "message": (
                    "The active extraction uses a different visual strategy. "
                    "Wait for it to finish before starting another strategy."
                ),
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc
    return _llm_extraction_snapshot_from_durable(durable)


def _durable_document_extraction_matches_request(
    job: DurableJob,
    *,
    payload: dict[str, Any],
) -> bool:
    job_payload = dict(job.payload or {})
    expected_plan = payload.get("processing_plan")
    actual_plan = job_payload.get("processing_plan")
    return bool(
        str(job_payload.get("project_id") or "") == str(payload.get("project_id") or "")
        and str(job_payload.get("document_id") or "")
        == str(payload.get("document_id") or "")
        and str(job_payload.get("source_sha256") or "")
        == str(payload.get("source_sha256") or "")
        and isinstance(actual_plan, dict)
        and isinstance(expected_plan, dict)
        and str(actual_plan.get("canonical_sha256") or "")
        == str(expected_plan.get("canonical_sha256") or "")
        and job_payload.get("visual_strategy") == payload.get("visual_strategy")
        and str(job_payload.get("structure_revision_chain_sha256") or "")
        == str(payload.get("structure_revision_chain_sha256") or "")
        and str(job_payload.get("result_runtime_sha256") or "")
        == str(payload.get("result_runtime_sha256") or "")
    )


def _resolve_document_extraction_retry_lineage(
    durable_jobs: Any,
    *,
    base_idempotency_key: str,
    payload: dict[str, Any],
) -> tuple[DurableJob | None, str, dict[str, Any]]:
    """Find the exact plan's current attempt, independent of aggregate latest.

    Every terminal retry receives a key derived from the immutable base identity
    and the immediately preceding job ID. Walking those exact identities avoids
    permanently reusing a failed base job after another plan becomes the newest
    attempt for the same document.
    """

    get_by_idempotency = getattr(durable_jobs, "get_by_idempotency", None)
    if not callable(get_by_idempotency):
        return None, "", payload
    key = base_idempotency_key
    seen: set[str] = set()
    for _ in range(64):
        if key in seen:
            raise RuntimeError("Document extraction retry lineage contains a cycle.")
        seen.add(key)
        attempt = get_by_idempotency(
            kind="document.extract",
            idempotency_key=key,
        )
        if attempt is None:
            return None, key, payload
        if not _durable_document_extraction_matches_request(attempt, payload=payload):
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "document_extraction_retry_identity_conflict",
                    "message": (
                        "The extraction retry identity is bound to different input."
                    ),
                },
            )
        if attempt.status in {
            JobStatus.PENDING,
            JobStatus.RUNNING,
            JobStatus.CANCEL_REQUESTED,
            JobStatus.SUCCEEDED,
        }:
            return attempt, "", payload
        if attempt.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
            raise RuntimeError("Document extraction retry has an unsupported status.")
        payload = {**payload, "retry_of_job_id": attempt.job_id}
        key = _document_extraction_retry_idempotency_key(
            base_idempotency_key=base_idempotency_key,
            retry_of_job_id=attempt.job_id,
        )
    raise RuntimeError("Document extraction retry lineage exceeds 64 attempts.")


def _document_extraction_result_runtime_sha256(
    processing_plan: ResolvedProcessingPlanV1,
) -> str:
    if processing_plan.llm_provider == "none":
        runtime = {
            "mode": "LOCAL_STRUCTURE_PROJECTION",
            "content_role_runtime_version": _LOCAL_CONTENT_ROLE_RUNTIME_VERSION,
            "semantic_classification": "NOT_RUN",
            "semantic_atomization": "NOT_RUN",
        }
    else:
        runtime = {
            "mode": "EDITABLE_LLM_REVIEW",
            "semantic_planning_runtime_version": SEMANTIC_PLANNING_RUNTIME_VERSION,
            "semantic_classifier_runtime_version": (MODEL_SEMANTIC_CLASSIFIER_VERSION),
            "requirement_assembler_runtime_version": (
                MODEL_REQUIREMENT_ASSEMBLER_VERSION
            ),
            "document_runtime_version": LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION,
        }
    serialized = json.dumps(
        {
            "schema_version": (_DOCUMENT_EXTRACTION_RESULT_RUNTIME_SCHEMA_VERSION),
            "preview_schema_version": LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
            "source_ir_schema_version": SOURCE_IR_SCHEMA_VERSION,
            "source_asset_publication_runtime_version": (
                SOURCE_ASSET_PUBLICATION_RUNTIME_VERSION
            ),
            "extraction_result_artifact_publication_runtime_version": (
                EXTRACTION_RESULT_ARTIFACT_PUBLICATION_RUNTIME_VERSION
            ),
            # Bind every accepted SourceIR route, not merely the adapter used
            # by the current file. A queued/result artifact is invalidated
            # whenever any supported parser or controlled-conversion runtime
            # changes, so cross-format cache reuse cannot hide a stale route.
            "source_ir_adapters": {
                "pdf": {
                    "adapter_id": PDF_SOURCE_IR_ADAPTER_ID,
                    "adapter_version": PDF_SOURCE_IR_ADAPTER_VERSION,
                },
                "azure_document_intelligence_pdf": {
                    "adapter_id": AZURE_DI_SOURCE_IR_ADAPTER_ID,
                    "adapter_version": AZURE_DI_SOURCE_IR_ADAPTER_VERSION,
                },
                "docx": {
                    "adapter_id": DocxSourceIRAdapter.adapter_id,
                    "adapter_version": DocxSourceIRAdapter.adapter_version,
                },
                "xlsx": {
                    "adapter_id": XlsxSourceIRAdapter.adapter_id,
                    "adapter_version": XlsxSourceIRAdapter.adapter_version,
                },
                "xlsb": {
                    "adapter_id": XLSB_SOURCE_IR_ADAPTER_ID,
                    "adapter_version": XLSB_SOURCE_IR_ADAPTER_VERSION,
                    "converter_id": LIBREOFFICE_XLSB_CONVERTER_ID,
                    "converter_version": LIBREOFFICE_XLSB_CONVERTER_VERSION,
                    "visual_profile_schema_version": (
                        SPREADSHEET_VISUAL_PROFILE_SCHEMA_VERSION
                    ),
                    "direct_visual_renderer_version": (
                        LIBREOFFICE_DIRECT_VISUAL_RENDERER_VERSION
                    ),
                    "cell_image_visual_renderer_version": (
                        LIBREOFFICE_CELL_IMAGE_VISUAL_RENDERER_VERSION
                    ),
                    "visual_runtime_version": (
                        LIBREOFFICE_DIRECT_VISUAL_EXPECTED_VERSION
                    ),
                },
            },
            "composable_pdf_visual_chain": {
                "provider_id": COMPOSABLE_VISUAL_CHAIN_PROVIDER_ID,
                "version": COMPOSABLE_VISUAL_CHAIN_VERSION,
            },
            "processing_plan_visual_strategy": (
                _runtime_visual_strategy_for_plan(processing_plan)
            ),
            "structure_resolver_version": DEFAULT_STRUCTURE_RESOLVER_VERSION,
            "structure_inspection_runtime_version": (
                STRUCTURE_INSPECTION_RUNTIME_VERSION
            ),
            "structure_boundary_ruleset_version": (STRUCTURE_BOUNDARY_RULESET_VERSION),
            "structure_presentation_schema_version": (
                STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION
            ),
            "requirement_tree_projection_runtime_version": (
                REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
            ),
            "table_evidence_projection_runtime_version": (
                TABLE_EVIDENCE_PROJECTION_RUNTIME_VERSION
            ),
            "table_record_projection_runtime_version": (
                TABLE_RECORD_PROJECTION_RUNTIME_VERSION
            ),
            "table_row_group_review_index_schema_version": (
                TABLE_ROW_GROUP_REVIEW_INDEX_SCHEMA_VERSION
            ),
            **runtime,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _runtime_visual_strategy_for_plan(
    processing_plan: ResolvedProcessingPlanV1,
) -> dict[str, Any]:
    """Build plan-bound visual identity without constructing configured clients."""

    if processing_plan.document.primary_parser == "azure_document_intelligence":
        adapter_version = (
            f"{AZURE_DI_SOURCE_IR_ADAPTER_VERSION}+plan."
            f"{processing_plan.canonical_sha256[:12]}"
        )
    elif (
        processing_plan.document.enhancement_chain
        or processing_plan.document.complex_vision
        or processing_plan.force_visual_pages
    ):
        # The concrete +visual digest is also bound in the job's visual
        # strategy. The runtime hash records every code/provider-plan input
        # that defines that digest without loading credentials here.
        adapter_version = f"{PDF_SOURCE_IR_ADAPTER_VERSION}+visual.plan-bound"
    else:
        adapter_version = PDF_SOURCE_IR_ADAPTER_VERSION
    return {
        "primary_parser": processing_plan.document.primary_parser,
        "enhancement_chain": list(processing_plan.document.enhancement_chain),
        "complex_vision": processing_plan.document.complex_vision,
        "auto_escalation": processing_plan.document.auto_escalation,
        "force_visual_pages": list(processing_plan.force_visual_pages),
        "adapter_version_family": adapter_version,
        "plan_sha256": processing_plan.canonical_sha256,
    }


def _document_extraction_durable_idempotency_key(
    *,
    source_sha256: str,
    processing_plan_sha256: str,
    visual_strategy_sha256: str,
    structure_revision_chain_sha256: str,
    result_runtime_sha256: str,
) -> str:
    serialized = json.dumps(
        {
            "schema_version": "document-extraction-job-identity/v2",
            "source_sha256": source_sha256,
            "processing_plan_sha256": processing_plan_sha256,
            "visual_strategy_sha256": visual_strategy_sha256,
            "structure_revision_chain_sha256": (
                structure_revision_chain_sha256 or "base"
            ),
            "result_runtime_sha256": result_runtime_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _document_extraction_retry_idempotency_key(
    *,
    base_idempotency_key: str,
    retry_of_job_id: str,
) -> str:
    serialized = json.dumps(
        {
            "schema_version": "document-extraction-job-retry/v1",
            "base_idempotency_key": base_idempotency_key,
            "retry_of_job_id": retry_of_job_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _document_extraction_visual_repair_idempotency_key(
    *,
    base_idempotency_key: str,
    repair_identity_sha256: str,
) -> str:
    serialized = json.dumps(
        {
            "schema_version": "document-extraction-visual-repair-job/v1",
            "base_idempotency_key": base_idempotency_key,
            "repair_identity_sha256": repair_identity_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _queue_local_structure_document_extraction(
    *,
    project_id: str,
    document: dict[str, Any],
    processing_plan: ResolvedProcessingPlanV1,
    structure_revisions: tuple[StructureRevisionV1, ...] = (),
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    document_id = str(document.get("id") or "")
    source_sha256 = str(document.get("source_sha256") or "")
    from reqpilotx_server.application.extraction.processing_pipeline import (
        processing_plan_visual_strategy,
    )

    visual_strategy = processing_plan_visual_strategy(processing_plan)
    result_runtime_sha256 = _document_extraction_result_runtime_sha256(processing_plan)
    with _LLM_EXTRACTION_JOB_LOCK:
        current = _LLM_EXTRACTION_JOBS.get(document_id)
        if (
            current
            and str(current.get("project_id") or "") == project_id
            and str(current.get("source_sha256") or "") == source_sha256
            and str(current.get("status") or "").upper() in {"PENDING", "RUNNING"}
        ):
            current_plan = current.get("processing_plan")
            if (
                isinstance(current_plan, dict)
                and current_plan.get("canonical_sha256")
                == processing_plan.canonical_sha256
            ):
                if current.get("result_runtime_sha256") == result_runtime_sha256:
                    return dict(current)
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "reason_code": "document_extraction_runtime_conflict",
                        "message": (
                            "The active extraction uses an incompatible result "
                            "runtime. Wait for it to finish before starting a "
                            "replacement run."
                        ),
                    },
                    headers=_ASSET_RESPONSE_HEADERS,
                )
            raise ApplicationError(
                status_code=409,
                detail={
                    "reason_code": "document_extraction_plan_conflict",
                    "message": (
                        "The active extraction uses a different frozen processing plan."
                    ),
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        now = datetime.now(timezone.utc).isoformat()
        job_id = f"llm-extraction-{uuid.uuid4().hex}"
        job: dict[str, Any] = {
            "job_id": job_id,
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": source_sha256,
            "visual_strategy": visual_strategy,
            "processing_plan": processing_plan.model_dump(mode="json"),
            "result_runtime_sha256": result_runtime_sha256,
            "status": "PENDING",
            "stage": "queued",
            "total_clauses": 0,
            "total_targets": 0,
            "total_chunks": 0,
            "completed_chunks": 0,
            "completed_targets": 0,
            "successful_chunks": 0,
            "unresolved_chunks": 0,
            "blocked_chunks": 0,
            "current_chunk_index": None,
            "current_chunk_target_count": 0,
            "error_code": "",
            "error_stage": "",
            "error_cause_code": "",
            "result": None,
            "partial_result": None,
            "partial_revision": 0,
            "partial_completed_chunk_indexes": (),
            "final_result_sha256": "",
            "created_at_utc": now,
            "started_at_utc": "",
            "finished_at_utc": "",
            "updated_at_utc": now,
            "last_progress": "",
            "last_progress_en": "",
        }
        _purge_llm_extraction_job_checkpoints(
            project_id=project_id,
            document_id=document_id,
        )
        _persist_llm_extraction_job_checkpoint(job)
        _LLM_EXTRACTION_JOBS[document_id] = job
        thread = Thread(
            target=_run_local_structure_document_extraction_worker,
            kwargs={
                "job_id": job_id,
                "project_id": project_id,
                "document": dict(document),
                "processing_plan": processing_plan,
                "structure_revisions": structure_revisions,
            },
            name=f"rules-extraction-{document_id[:8]}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            _update_llm_extraction_job(
                document_id,
                job_id,
                persist_checkpoint=True,
                status="FAILED",
                stage="failed",
                error_code="document_extraction_worker_start_failed",
                partial_revision=0,
                partial_result=None,
                partial_completed_chunk_indexes=(),
                final_result_sha256="",
                finished_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            raise
        return dict(job)


def _queue_llm_document_extraction(
    *,
    project_id: str,
    document: dict[str, Any],
    provider: PreflightSemanticProviderPort,
    processing_plan: ResolvedProcessingPlanV1,
    structure_revisions: tuple[StructureRevisionV1, ...] = (),
    authorized_visual_runtime: AuthorizedPdfVisualRuntime | None = None,
) -> dict[str, Any]:
    """Create one process-local run while preserving a validated Structure head."""
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    document_id = str(document.get("id") or "")
    source_sha256 = str(document.get("source_sha256") or "")
    from reqpilotx_server.application.extraction.processing_pipeline import (
        processing_plan_visual_strategy,
    )

    visual_strategy = processing_plan_visual_strategy(processing_plan)
    result_runtime_sha256 = _document_extraction_result_runtime_sha256(processing_plan)
    with _LLM_EXTRACTION_JOB_LOCK:
        current = _LLM_EXTRACTION_JOBS.get(document_id)
        if (
            current
            and str(current.get("project_id") or "") == project_id
            and str(current.get("source_sha256") or "") == source_sha256
            and str(current.get("status") or "").upper() in {"PENDING", "RUNNING"}
        ):
            current_plan = current.get("processing_plan")
            if (
                not isinstance(current_plan, dict)
                or current_plan.get("canonical_sha256")
                != processing_plan.canonical_sha256
            ):
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "reason_code": "document_extraction_plan_conflict",
                        "message": (
                            "The active extraction uses a different frozen "
                            "processing plan."
                        ),
                    },
                    headers=_ASSET_RESPONSE_HEADERS,
                )
            if _llm_job_visual_strategy(current) != visual_strategy:
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": ("llm_document_extraction_visual_strategy_conflict"),
                        "message": (
                            "The active extraction uses a different visual "
                            "strategy. Wait for it to finish before starting "
                            "another strategy."
                        ),
                    },
                    headers=_ASSET_RESPONSE_HEADERS,
                )
            if current.get("result_runtime_sha256") != result_runtime_sha256:
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "reason_code": "document_extraction_runtime_conflict",
                        "message": (
                            "The active extraction uses an incompatible result "
                            "runtime. Wait for it to finish before starting a "
                            "replacement run."
                        ),
                    },
                    headers=_ASSET_RESPONSE_HEADERS,
                )
            return dict(current)
        now = datetime.now(timezone.utc).isoformat()
        job_id = f"llm-extraction-{uuid.uuid4().hex}"
        job: dict[str, Any] = {
            "job_id": job_id,
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": source_sha256,
            "visual_strategy": visual_strategy,
            "processing_plan": processing_plan.model_dump(mode="json"),
            "result_runtime_sha256": result_runtime_sha256,
            "status": "PENDING",
            "stage": "queued",
            "total_clauses": 0,
            "total_targets": 0,
            "total_chunks": 0,
            "completed_chunks": 0,
            "completed_targets": 0,
            "successful_chunks": 0,
            "unresolved_chunks": 0,
            "blocked_chunks": 0,
            "current_chunk_index": None,
            "current_chunk_target_count": 0,
            "error_code": "",
            "error_stage": "",
            "error_cause_code": "",
            "result": None,
            "partial_result": None,
            "partial_revision": 0,
            "partial_completed_chunk_indexes": (),
            "final_result_sha256": "",
            "created_at_utc": now,
            "started_at_utc": "",
            "finished_at_utc": "",
            "updated_at_utc": now,
            "last_progress": "",
            "last_progress_en": "",
        }
        _purge_llm_extraction_job_checkpoints(
            project_id=project_id,
            document_id=document_id,
        )
        _persist_llm_extraction_job_checkpoint(job)
        _LLM_EXTRACTION_JOBS[document_id] = job
        thread = Thread(
            target=_run_llm_document_extraction_worker,
            kwargs={
                "job_id": job_id,
                "project_id": project_id,
                "document": dict(document),
                "provider": provider,
                "processing_plan": processing_plan,
                "structure_revisions": structure_revisions,
                "authorized_visual_runtime": authorized_visual_runtime,
            },
            name=f"llm-extraction-{document_id[:8]}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            _update_llm_extraction_job(
                document_id,
                job_id,
                persist_checkpoint=True,
                status="FAILED",
                stage="failed",
                error_code="llm_document_extraction_worker_start_failed",
                partial_revision=0,
                partial_result=None,
                partial_completed_chunk_indexes=(),
                final_result_sha256="",
                finished_at_utc=datetime.now(timezone.utc).isoformat(),
            )
            raise
        return dict(job)


def start_document_extraction(
    project_id: str,
    document_id: str,
    response: ResponseSink,
    payload: DocumentExtractionStartRequest | None = None,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.provider_support import (
        _build_llm_shadow_semantic_provider,
    )
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.runtime import (
        _ASSET_RESPONSE_HEADERS,
        RUNTIME_SETTINGS,
        _runtime_record_path,
    )

    document = _require_project_document(project_id, document_id)
    path = _runtime_record_path(document)
    if not path.exists() or not path.is_file():
        raise ApplicationError(status_code=404, detail="SOR source file not found.")
    document = _require_llm_document_source_identity(document)
    source_sha256 = str(document.get("source_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "llm_document_source_identity_unavailable",
                "message": (
                    "This document has no trusted stored source identity for "
                    "remote semantic extraction."
                ),
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    request = payload or DocumentExtractionStartRequest()
    processing_plan = resolve_processing_plan(
        project_id=project_id,
        document_id=document_id,
        run_override=request.run_profile_override,
    )
    result_runtime_sha256 = _document_extraction_result_runtime_sha256(processing_plan)
    # The composable plan resolves its own page providers inside SourceIR.  The
    # legacy registry-bound visual runtime remains off for this new entrypoint.
    from reqpilotx_server.application.extraction.processing_pipeline import (
        processing_plan_visual_strategy,
    )

    authorized_visual_runtime = None
    visual_strategy = processing_plan_visual_strategy(processing_plan)
    current = _get_llm_extraction_job(
        project_id,
        document_id,
        source_sha256=source_sha256,
    )
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        current = _supersede_stale_pending_document_extraction(
            current,
            replacement_plan=processing_plan,
        )
    if current and str(current.get("status") or "").upper() in {"PENDING", "RUNNING"}:
        current_plan = current.get("processing_plan")
        if (
            not isinstance(current_plan, dict)
            or current_plan.get("canonical_sha256") != processing_plan.canonical_sha256
        ):
            raise ApplicationError(
                status_code=409,
                detail={
                    "reason_code": "document_extraction_plan_conflict",
                    "message": (
                        "The active extraction uses a different frozen processing plan."
                    ),
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        if _llm_job_visual_strategy(current) != visual_strategy:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "llm_document_extraction_visual_strategy_conflict",
                    "message": (
                        "The active extraction uses a different visual "
                        "strategy. Wait for it to finish before starting "
                        "another strategy."
                    ),
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        if current.get("result_runtime_sha256") != result_runtime_sha256:
            raise ApplicationError(
                status_code=409,
                detail={
                    "reason_code": "document_extraction_runtime_conflict",
                    "message": (
                        "The active extraction uses an incompatible result "
                        "runtime. Wait for it to finish before starting a "
                        "replacement run."
                    ),
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        return _build_llm_extraction_job_response(
            project_id,
            document_id,
            job=current,
        )
    structure_revisions = _validated_structure_revisions_for_document_rerun(
        project_id=project_id,
        document_id=document_id,
        document=document,
        visual_strategy=visual_strategy,
    )
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        _require_document_extraction_worker_fleet(processing_plan)
        if processing_plan.llm_provider != "none":
            _build_llm_shadow_semantic_provider(processing_plan)
        snapshot = _enqueue_durable_llm_document_extraction(
            project_id=project_id,
            document=document,
            processing_plan=processing_plan,
            visual_provider="off",
            visual_strategy=visual_strategy,
            structure_revision_chain_sha256=(
                structure_revisions[-1].revision_chain_sha256
                if structure_revisions
                else ""
            ),
        )
    elif processing_plan.llm_provider == "none":
        snapshot = _queue_local_structure_document_extraction(
            project_id=project_id,
            document=document,
            processing_plan=processing_plan,
            structure_revisions=structure_revisions,
        )
    else:
        provider = _build_llm_shadow_semantic_provider(processing_plan)
        snapshot = _queue_llm_document_extraction(
            project_id=project_id,
            document=document,
            provider=provider,
            processing_plan=processing_plan,
            structure_revisions=structure_revisions,
            authorized_visual_runtime=authorized_visual_runtime,
        )
    return _build_llm_extraction_job_response(
        project_id,
        document_id,
        job=snapshot,
    )


def repair_document_extraction_visual_pages(
    project_id: str,
    document_id: str,
    response: ResponseSink,
    payload: DocumentExtractionVisualRepairRequest,
) -> dict[str, Any]:
    """Retry only server-derived failed OCR pages from one immutable result."""

    from reqpilotx_server.application.extraction.processing_pipeline import (
        processing_plan_visual_strategy,
    )
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.runtime import (
        RUNTIME_SETTINGS,
        _ASSET_RESPONSE_HEADERS,
        _durable_jobs,
    )

    response.headers.update(_ASSET_RESPONSE_HEADERS)
    if RUNTIME_SETTINGS.database_backend is not DatabaseBackend.POSTGRESQL:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "visual_repair_plan_unsupported",
                "message": "Failed-page visual repair requires the durable runtime.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    document = _require_llm_document_source_identity(
        _require_project_document(project_id, document_id)
    )
    source_sha256 = str(document.get("source_sha256") or "")
    queue = _durable_jobs()
    get_latest_succeeded = getattr(queue, "get_latest_succeeded", None)
    base_job = (
        get_latest_succeeded(
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id=document_id,
        )
        if callable(get_latest_succeeded)
        else None
    )
    if (
        base_job is None
        or base_job.status is not JobStatus.SUCCEEDED
        or base_job.job_id != payload.expected_base_job_id
        or base_job.generation != payload.expected_base_job_generation
        or str((base_job.payload or {}).get("project_id") or "") != project_id
        or str((base_job.payload or {}).get("source_sha256") or "") != source_sha256
        or not isinstance(base_job.result, dict)
        or str(base_job.result.get("sha256") or "")
        != payload.expected_base_artifact_sha256
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "visual_repair_base_conflict",
                "message": (
                    "The completed extraction changed before failed-page repair "
                    "was submitted. Refresh the current result and try again."
                ),
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    base_snapshot = _llm_extraction_snapshot_from_durable(base_job)
    base_result = base_snapshot.get("result")
    if not isinstance(base_result, dict):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "visual_repair_provenance_incomplete",
                "message": "The completed extraction artifact is not available for repair.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    baseline = _visual_repair_baseline(base_result)
    try:
        processing_plan = ResolvedProcessingPlanV1.model_validate(
            base_result.get("processing_plan")
        )
    except (TypeError, ValueError) as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "visual_repair_provenance_incomplete",
                "message": "The completed extraction plan cannot be verified.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc
    if baseline is None:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "visual_repair_provenance_incomplete",
                "message": "The visual page receipts cannot be verified.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    failed_pages = tuple(baseline["failed_page_numbers"])
    if not failed_pages:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "visual_repair_not_required",
                "message": "The completed extraction has no failed visual pages.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    if not _visual_repair_plan_supported(processing_plan):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "visual_repair_plan_unsupported",
                "message": (
                    "This extraction plan cannot use failed-page-only OCR repair; "
                    "start a new full extraction with an explicit plan instead."
                ),
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    profile = get_project_processing_profile(project_id)
    if (
        processing_plan.project_profile_revision != profile.revision
        or processing_plan.project_profile_sha256 != profile.canonical_sha256
        or (
            processing_plan_requires_connection(processing_plan)
            and processing_plan.connection_revision != get_runtime_connection_revision()
        )
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "visual_repair_base_conflict",
                "message": (
                    "The project processing profile or AI connection changed after "
                    "the completed extraction. Start a new full extraction instead."
                ),
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    base_result_runtime_sha256 = str(
        base_result.get("result_runtime_sha256")
        or (base_job.payload or {}).get("result_runtime_sha256")
        or ""
    )
    if not re.fullmatch(r"[0-9a-f]{64}", base_result_runtime_sha256):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "visual_repair_provenance_incomplete",
                "message": "The completed extraction runtime identity is unavailable.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    repair_identity_payload = {
        "schema_version": "document-extraction-visual-repair-plan/v1",
        "policy": "FAILED_OCR_PAGES_ONLY",
        "base_job_id": base_job.job_id,
        "base_job_generation": base_job.generation,
        "base_artifact_sha256": payload.expected_base_artifact_sha256,
        "base_processing_plan_sha256": processing_plan.canonical_sha256,
        "base_result_runtime_sha256": base_result_runtime_sha256,
        "base_page_receipts_sha256": baseline["base_page_receipts_sha256"],
        "cache_required_page_numbers": list(baseline["cache_required_page_numbers"]),
        "remote_allowed_page_numbers": list(baseline["remote_allowed_page_numbers"]),
        "page_image_sha256_by_page": baseline["page_image_sha256_by_page"],
        "remote_request_ceiling": len(failed_pages),
        "deep_layout_request_ceiling": 0,
    }
    repair_identity_sha256 = hashlib.sha256(
        json.dumps(
            repair_identity_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    visual_repair = {
        **repair_identity_payload,
        "repair_of_job_id": base_job.job_id,
        "repair_identity_sha256": repair_identity_sha256,
    }
    visual_strategy = processing_plan_visual_strategy(processing_plan)
    structure_revisions = _validated_structure_revisions_for_document_rerun(
        project_id=project_id,
        document_id=document_id,
        document=document,
        visual_strategy=visual_strategy,
    )
    snapshot = _enqueue_durable_llm_document_extraction(
        project_id=project_id,
        document=document,
        processing_plan=processing_plan,
        visual_provider="off",
        visual_strategy=visual_strategy,
        structure_revision_chain_sha256=(
            structure_revisions[-1].revision_chain_sha256 if structure_revisions else ""
        ),
        visual_repair=visual_repair,
    )
    return _build_llm_extraction_job_response(
        project_id,
        document_id,
        job=snapshot,
    )


def start_llm_document_extraction(
    project_id: str,
    document_id: str,
    response: ResponseSink,
    payload: LlmDocumentExtractionStartRequest | None = None,
) -> dict[str, Any]:
    """Compatibility entrypoint mapped to the project's composable plan."""

    legacy = payload or LlmDocumentExtractionStartRequest()
    run_override: RunProfileOverrideV1 | None = None
    if legacy.visual_provider != "off":
        profile = get_project_processing_profile(project_id)
        document = profile.document
        if legacy.visual_provider == "azure-document-intelligence":
            enhancement = (
                ()
                if document.primary_parser == "azure_document_intelligence"
                else ("azure_document_intelligence",)
            )
            document = document.model_copy(update={"enhancement_chain": enhancement})
        else:
            document = document.model_copy(
                update={
                    "enhancement_chain": ("bailian_layout",),
                    "complex_vision": "bailian_vision",
                }
            )
        run_override = RunProfileOverrideV1(document=document)
    return start_document_extraction(
        project_id,
        document_id,
        response,
        DocumentExtractionStartRequest(run_profile_override=run_override),
    )


def cancel_document_extraction(
    project_id: str,
    document_id: str,
    response: ResponseSink,
    payload: DocumentExtractionCancelRequest,
) -> dict[str, Any]:
    """Cancel only the exact visible attempt; never create a replacement job."""

    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, _ASSET_RESPONSE_HEADERS

    document = _require_project_document(project_id, document_id)
    source_sha256 = str(document.get("source_sha256") or "")
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    current = _get_llm_extraction_job(
        project_id,
        document_id,
        source_sha256=source_sha256,
    )
    expected_job_id = payload.expected_job_id
    if current is None or str(current.get("job_id") or "") != expected_job_id:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "document_extraction_cancel_conflict",
                "message": "The visible extraction attempt is no longer current.",
                "current_job_id": str((current or {}).get("job_id") or ""),
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    current_status = str(current.get("status") or "").upper()
    if current_status == "CANCELLED":
        return _build_llm_extraction_job_response(
            project_id,
            document_id,
            job=current,
        )
    if current_status not in {"PENDING", "RUNNING", "CANCEL_REQUESTED"}:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "document_extraction_not_cancellable",
                "message": "The extraction attempt is no longer cancellable.",
                "job_status": current_status,
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )

    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        from reqpilotx_server.application.runtime import _durable_jobs

        queue = _durable_jobs()
        durable = queue.get(expected_job_id)
        if (
            durable is None
            or durable.kind != "document.extract"
            or durable.aggregate_type != "sor-document"
            or durable.aggregate_id != document_id
            or str((durable.payload or {}).get("project_id") or "") != project_id
            or str((durable.payload or {}).get("source_sha256") or "") != source_sha256
        ):
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "document_extraction_cancel_binding_mismatch",
                    "message": "The queued extraction binding is no longer current.",
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        cancelled = queue.request_cancel(expected_job_id)
        if cancelled is None:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "document_extraction_not_cancellable",
                    "message": "The extraction entered its publication boundary.",
                },
                headers=_ASSET_RESPONSE_HEADERS,
            )
        current = _llm_extraction_snapshot_from_durable(cancelled)
    else:
        with _LLM_EXTRACTION_JOB_LOCK:
            live = _LLM_EXTRACTION_JOBS.get(document_id)
            if not live or str(live.get("job_id") or "") != expected_job_id:
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": "document_extraction_cancel_conflict",
                        "message": "The visible extraction attempt is no longer current.",
                    },
                    headers=_ASSET_RESPONSE_HEADERS,
                )
            live.update(
                {
                    "status": "CANCELLED",
                    "stage": "cancelled",
                    "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                    "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                }
            )
            current = dict(live)
    return _build_llm_extraction_job_response(
        project_id,
        document_id,
        job=current,
    )


def get_llm_document_extraction_status(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    document = _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    source_sha256 = str(document.get("source_sha256") or "")
    job = _get_llm_extraction_job(
        project_id,
        document_id,
        source_sha256=source_sha256,
    )
    completed_result = (
        job.get("result")
        if (
            job
            and str(job.get("status") or "").upper() == "COMPLETED"
            and isinstance(job.get("result"), dict)
        )
        else None
    )
    incompatible_completed_job = isinstance(
        completed_result, dict
    ) and not _llm_extraction_result_matches_runtime(
        completed_result,
        expected_visual_strategy=_llm_job_visual_strategy(job),
    )
    trusted_source_identity = bool(re.fullmatch(r"[0-9a-f]{64}", source_sha256))
    if incompatible_completed_job:
        logger.info(
            "In-memory LLM extraction result requires regeneration. "
            "project_id=%s document_id=%s stored_schema=%s",
            project_id,
            document_id,
            completed_result.get("schema_version"),
        )
        restored = (
            _restored_llm_extraction_job(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
            )
            if trusted_source_identity
            else None
        )
        # Keep the exact durable snapshot until response projection.  The
        # response builder still hides an incompatible formal result, while
        # its immutable artifact may independently prove visual-repair
        # eligibility for a fresh current-runtime extraction.
        job = restored or job
    elif job is None and trusted_source_identity:
        job = _restored_llm_extraction_job(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
    return _build_llm_extraction_job_response(
        project_id,
        document_id,
        job=job,
    )
