from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from reqpilotx_server import db
from reqpilotx_server.adapters.persistence.postgres_jobs import JobConflictError
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.export_contracts import (
    ExportRequest,
)
from reqpilotx_server.application.extraction.source_asset_objects import (
    runtime_source_asset_loader,
)
from reqpilotx_server.application.runtime import RUNTIME_STATE
from reqpilotx_server.application.transport import (
    BinaryArtifact,
    FileArtifact,
    JsonArtifact,
    ResponseSink,
)
from reqpilotx_server.domain.requirement_matrix import (
    count_missing_source_business_nodes,
    is_analysis_target,
    summarize_effective_conclusions,
)
from reqpilotx_server.infrastructure.runtime_settings import DatabaseBackend
from reqpilotx_server.ports.job_queue import DurableJob, JobExecutionContext, JobStatus
from reqpilotx_server.services.exporter import export_compliance_matrix

logger = logging.getLogger(__name__)


def _matrix_manual_decision_value(node: dict[str, Any]) -> str:
    decision = node.get("manual_decision")
    if isinstance(decision, dict):
        return str(decision.get("decision") or "").strip()
    return str(decision or "").strip()


def _matrix_decision_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [node for node in nodes if is_analysis_target(node)]


def _matrix_snapshot_for_export(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    # The frozen snapshot stays schema-valid and immutable. DRAFT is only a
    # presentation rule applied by the renderer; it must not erase lineage.
    return json.loads(json.dumps(snapshot, ensure_ascii=False, default=str))


def _dump_matrix_export_snapshot(snapshot: dict[str, Any]) -> str:
    return json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _parse_matrix_export_snapshot(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        payload = value
    else:
        try:
            payload = json.loads(str(value or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("schema_version") or "") != "requirement-matrix-snapshot/v1":
        return None
    if not isinstance(payload.get("nodes"), list):
        return None
    return payload

def _export_job_snapshot(job: DurableJob) -> dict[str, Any]:
    payload = {
        "job_id": job.job_id,
        "status": job.status.value,
        "poll_url": (
            f"/api/projects/{job.payload.get('project_id', '')}/"
            f"export-jobs/{job.job_id}"
        ),
        "progress": dict(job.progress or {}),
        "error": (
            {
                "code": job.error_code,
                "message": job.error_message,
            }
            if job.status is JobStatus.FAILED
            else {}
        ),
    }
    if job.status is JobStatus.SUCCEEDED and isinstance(job.result, dict):
        payload["result"] = dict(job.result)
    return payload

def _enqueue_durable_project_export(
    project_id: str,
    payload: ExportRequest | None,
) -> dict[str, Any]:
    from reqpilotx_server.application.projects import _require_project
    from reqpilotx_server.application.runtime import _durable_jobs

    _require_project(project_id)
    if payload is None:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "requirement_set_id_required",
                "message": "创建矩阵前必须明确选择一个需求版本。",
            },
        )
    request_payload = payload.model_dump(mode="json")
    document_id = str(request_payload.get("document_id") or "").strip()
    requirement_set_id = str(
        request_payload.get("requirement_set_id") or ""
    ).strip()
    if not document_id and requirement_set_id:
        selected = db.get_requirement_set(requirement_set_id)
        document_id = str((selected or {}).get("sor_document_id") or "")
    document = db.get_sor_document(document_id) if document_id else None
    if not document or str(document.get("project_id") or "") != project_id:
        raise ApplicationError(
            status_code=404,
            detail="SOR document not found for this project.",
        )
    requirement_set_id = requirement_set_id or str(
        document.get("active_requirement_set_id") or ""
    )
    selected = db.get_requirement_set(requirement_set_id)
    if (
        not selected
        or str(selected.get("project_id") or "") != project_id
        or str(selected.get("sor_document_id") or "") != document_id
    ):
        raise ApplicationError(
            status_code=404,
            detail="Requirement set not found for this SOR document.",
        )
    request_payload["document_id"] = document_id
    request_payload["requirement_set_id"] = requirement_set_id
    try:
        durable = _durable_jobs().enqueue(
            kind="project.export",
            aggregate_type="requirement-set",
            aggregate_id=requirement_set_id,
            idempotency_key=uuid.uuid4().hex,
            payload={
                "project_id": project_id,
                "request": request_payload,
            },
            priority=10,
            max_attempts=2,
        )
    except JobConflictError:
        durable = _durable_jobs().get_latest(
            kind="project.export",
            aggregate_type="requirement-set",
            aggregate_id=requirement_set_id,
        )
        if durable is None:
            raise
    return _export_job_snapshot(durable)

def export_project(
    project_id: str,
    payload: ExportRequest,
) -> Any:
    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS

    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        return JsonArtifact(
            status_code=202,
            content=_enqueue_durable_project_export(project_id, payload),
        )
    return _execute_project_export(project_id, payload)

def _execute_project_export(
    project_id: str,
    payload: ExportRequest | None = None,
    *,
    job_context: JobExecutionContext | None = None,
) -> dict[str, Any]:
    from reqpilotx_server.application.ingestion import _hash_file, _publish_staged_object
    from reqpilotx_server.application.projects import _require_project
    from reqpilotx_server.application.requirement_matrix import (
        build_requirement_matrix_snapshot,
    )
    from reqpilotx_server.application.requirements import (
        _safe_name,
    )
    from reqpilotx_server.application.runtime import (
        EXPORTS_DIR,
        RUNTIME_SETTINGS,
        _runtime_record_path,
    )

    project = _require_project(project_id)
    if payload is None:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "requirement_set_id_required",
                "message": "创建矩阵前必须明确选择一个需求版本。",
            },
        )
    document_id = (
        str(payload.document_id).strip() if payload and payload.document_id else ""
    )
    requested_set_id = (
        str(payload.requirement_set_id).strip()
        if payload and payload.requirement_set_id
        else ""
    )
    mode = str(payload.mode if payload else "FORMAL").strip().upper() or "FORMAL"
    if not document_id and requested_set_id:
        requested_set = db.get_requirement_set(requested_set_id)
        document_id = str((requested_set or {}).get("sor_document_id") or "")
    if not document_id:
        raise ApplicationError(
            status_code=422,
            detail="document_id is required; exports are scoped to one SOR document.",
        )
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )
    requirement_set_id = requested_set_id or str(
        document.get("active_requirement_set_id") or ""
    )
    requirement_set = db.get_requirement_set(requirement_set_id)
    if (
        not requirement_set
        or requirement_set.get("project_id") != project_id
        or requirement_set.get("sor_document_id") != document_id
    ):
        raise ApplicationError(
            status_code=404, detail="Requirement set not found for this SOR document."
        )
    matrix_model = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requirement_set_id,
        require_current_runtime=True,
    )
    matrix_snapshot = matrix_model.model_dump(mode="json")
    if str(matrix_snapshot.get("state") or "").strip().upper() != "READY":
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "matrix_requires_reextraction",
                "message": "该需求版本没有可信的统一需求矩阵，请重新提取生成新版本。",
            },
        )

    matrix_nodes = [
        node
        for node in matrix_snapshot.get("nodes") or []
        if isinstance(node, dict)
    ]
    decision_nodes = _matrix_decision_nodes(matrix_nodes)
    conclusion_summary = summarize_effective_conclusions(matrix_model.nodes)
    human_decision_count = sum(
        1 for node in decision_nodes if _matrix_manual_decision_value(node)
    )
    human_coverage = (
        human_decision_count / len(decision_nodes) if decision_nodes else 1.0
    )
    hard_blockers: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    if str(requirement_set.get("status") or "").strip().upper() != "LOCKED":
        hard_blockers.append({"code": "REQUIREMENT_SET_NOT_LOCKED", "hard": True})
    # Historical sets cannot reuse the currently rendered preview implicitly.
    # Their own generation identifier proves that a stable preview existed
    # when the immutable set was published.
    if (
        not str(requirement_set.get("preview_generation_id") or "").strip()
        and str(requirement_set.get("preview_status_at_lock") or "").strip().upper()
        != "READY"
    ):
        hard_blockers.append(
            {
                "code": "PREVIEW_NOT_STABLE",
                "status": "MISSING_REQUIREMENT_SET_PREVIEW",
                "hard": True,
            }
        )
    missing_source_count = count_missing_source_business_nodes(
        matrix_model.nodes
    )
    if missing_source_count:
        warnings.append(
            {"code": "MISSING_SOURCE", "count": missing_source_count, "hard": False}
        )
    if conclusion_summary.missing_effective_conclusion_count:
        hard_blockers.append(
            {
                "code": "MISSING_EFFECTIVE_CONCLUSIONS",
                "count": conclusion_summary.missing_effective_conclusion_count,
                "hard": True,
            }
        )
    formal_readiness = {
        "ready": not hard_blockers,
        "blockers": hard_blockers,
        "hard_blockers": hard_blockers,
        "warnings": warnings,
    }
    if mode == "FORMAL" and formal_readiness.get("hard_blockers"):
        raise ApplicationError(
            status_code=409,
            detail={
                "message": (
                    "Formal export requires a locked requirement set with a stable preview "
                    "and a current effective conclusion for every analysis target."
                ),
                "readiness": formal_readiness,
            },
        )
    export_snapshot = _matrix_snapshot_for_export(matrix_snapshot)

    version_label = (
        str(payload.version_label).strip() if payload and payload.version_label else ""
    )
    compare_export_id = (
        str(payload.compare_export_id).strip()
        if payload and payload.compare_export_id
        else ""
    )
    compare_matrix_snapshot: dict[str, Any] | None = None
    compare_version_label = ""
    if compare_export_id:
        compare_job = db.get_export_job(compare_export_id)
        if not compare_job or compare_job.get("project_id") != project_id:
            raise ApplicationError(
                status_code=404,
                detail="Compare export version not found for this project.",
            )
        if str(compare_job.get("sor_document_id") or "") != document_id:
            raise ApplicationError(
                status_code=409,
                detail="Export comparison is only allowed within the same SOR document.",
            )
        compare_matrix_snapshot = _parse_matrix_export_snapshot(
            compare_job.get("snapshot_json")
        )
        if compare_matrix_snapshot is None:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "compare_export_uses_legacy_snapshot",
                    "message": "该历史导出不含统一需求矩阵快照，仍可下载，但不能作为新矩阵比较基线。",
                },
            )
        compare_version_label = str(
            compare_job.get("version_label") or compare_job.get("file_name") or ""
        )

    export_dir = (
        EXPORTS_DIR
        if RUNTIME_SETTINGS.is_sqlite
        else RUNTIME_SETTINGS.scratch_root / "export-staging"
    )
    export_dir.mkdir(parents=True, exist_ok=True)
    if job_context is not None:
        job_context.heartbeat(progress={"stage": "rendering"})
    file_name, file_path = export_compliance_matrix(
        project_name=project["name"],
        customer_name=project["customer_name"],
        source_file_name=str(document.get("file_name") or ""),
        export_dir=export_dir,
        matrix_snapshot=export_snapshot,
        version_label=version_label,
        compare_matrix_snapshot=compare_matrix_snapshot,
        compare_version_label=compare_version_label,
        export_mode=mode,
        source_file_path=_runtime_record_path(document),
        source_sha256=str(requirement_set.get("source_hash") or ""),
        source_asset_loader=runtime_source_asset_loader(),
    )
    latest_ai_job = (
        db.get_latest_completed_ai_analysis_job_for_set(requirement_set_id) or {}
    )
    export_knowledge_base_version = (
        int(latest_ai_job.get("knowledge_base_version") or 0) if latest_ai_job else 0
    )
    generated_path = Path(file_path)
    storage_identity = str(file_path)
    object_sha256 = ""
    object_size = 0
    object_created = False
    export_media_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        if job_context is not None and job_context.cancellation_requested():
            generated_path.unlink(missing_ok=True)
            return {"cancelled": True}
        if job_context is not None:
            job_context.begin_publication()
        object_sha256 = _hash_file(generated_path)
        object_size = generated_path.stat().st_size
        storage_identity = (
            f"exports/{project_id}/{requirement_set_id}/"
            f"{object_sha256}/{_safe_name(file_name)}"
        )
        object_created = _publish_staged_object(
            target=generated_path,
            storage_key=storage_identity,
            expected_sha256=object_sha256,
            media_type=export_media_type,
        )
    try:
        job = db.create_export_job(
            project_id=project_id,
            file_name=file_name,
            file_path=storage_identity,
            version_label=version_label,
            compare_export_id=compare_export_id,
            snapshot_json=_dump_matrix_export_snapshot(export_snapshot),
            sor_document_id=document_id,
            requirement_set_id=requirement_set_id,
            export_mode=mode,
            source_hash=str(requirement_set.get("source_hash") or ""),
            knowledge_base_version=export_knowledge_base_version,
            ai_run_id=str(latest_ai_job.get("id") or ""),
            human_coverage=human_coverage,
            object_sha256=object_sha256,
            object_size=object_size,
            media_type=export_media_type,
        )
    except Exception as exc:
        if object_created and RUNTIME_STATE.container is not None:
            RUNTIME_STATE.container.object_store.delete(storage_identity)
        if "locked" in str(exc).lower():
            raise ApplicationError(
                status_code=503, detail="Export storage is busy. Please retry."
            ) from exc
        raise
    except BaseException:
        if object_created and RUNTIME_STATE.container is not None:
            RUNTIME_STATE.container.object_store.delete(storage_identity)
        raise
    finally:
        if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
            generated_path.unlink(missing_ok=True)
    response = {
        "export_id": job["id"],
        "file_name": file_name,
        "download_url": f"/api/exports/{file_name}",
        "version_label": version_label,
        "compare_export_id": compare_export_id,
        "document_id": document_id,
        "requirement_set_id": requirement_set_id,
        "requirement_set_version": int(requirement_set.get("version") or 0),
        "mode": mode,
        "source_hash": str(requirement_set.get("source_hash") or ""),
        "knowledge_base_version": export_knowledge_base_version,
        "ai_run_id": str(latest_ai_job.get("id") or ""),
        "ai_lineage_status": "COMPLETED" if latest_ai_job else "NOT_AVAILABLE",
        "human_coverage": human_coverage,
        "decision_eligible_count": len(decision_nodes),
        "human_decision_count": human_decision_count,
        "effective_conclusion_count": (
            conclusion_summary.effective_conclusion_count
        ),
        "effective_conclusion_coverage": (
            conclusion_summary.effective_conclusion_coverage
        ),
        "missing_effective_conclusion_count": (
            conclusion_summary.missing_effective_conclusion_count
        ),
        "ai_default_count": conclusion_summary.ai_default_count,
        "human_override_count": conclusion_summary.human_override_count,
        "human_native_count": conclusion_summary.human_native_count,
    }
    if (
        _is_desktop_embedded()
        and RUNTIME_SETTINGS.database_backend is DatabaseBackend.SQLITE_TEST
    ):
        response["local_file_path"] = file_path
        response["local_export_dir"] = str(Path(file_path).parent)
        response["reveal_url"] = f"/api/exports/{file_name}/reveal"
    return response


def run_durable_project_export(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    payload = dict(durable_job.payload or {})
    project_id = str(payload.get("project_id") or "")
    request_payload = payload.get("request")
    if not project_id or not isinstance(request_payload, dict):
        raise RuntimeError("Durable export input is invalid.")
    context.heartbeat(progress={"stage": "validating"})
    return _execute_project_export(
        project_id,
        ExportRequest.model_validate(request_payload),
        job_context=context,
    )

def get_project_export_job(project_id: str, job_id: str) -> dict[str, Any]:
    from reqpilotx_server.application.projects import _require_project
    from reqpilotx_server.application.runtime import _durable_jobs

    _require_project(project_id)
    durable = _durable_jobs().get(job_id)
    if (
        durable is None
        or durable.kind != "project.export"
        or str(durable.payload.get("project_id") or "") != project_id
    ):
        raise ApplicationError(status_code=404, detail="Export job not found.")
    return _export_job_snapshot(durable)

def download_export(file_name: str) -> ResponseSink:
    from reqpilotx_server.application.requirements import _safe_name
    from reqpilotx_server.application.runtime import EXPORTS_DIR, RUNTIME_SETTINGS

    safe = _safe_name(file_name)
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        if RUNTIME_STATE.container is None:
            raise ApplicationError(
                status_code=503,
                detail="Runtime object storage is not initialized.",
            )
        record = db.get_export_job_by_file_name(safe)
        storage_key = str((record or {}).get("storage_key") or "").strip()
        if not storage_key:
            raise ApplicationError(status_code=404, detail="Export file not found.")
        stored = RUNTIME_STATE.container.object_store.stat(storage_key)
        if stored is None or int(stored.size) > 256 * 1024 * 1024:
            raise ApplicationError(status_code=404, detail="Export file not found.")
        with RUNTIME_STATE.container.object_store.open(storage_key) as source:
            payload = source.read(int(stored.size) + 1)
        if not isinstance(payload, bytes) or len(payload) != int(stored.size):
            raise ApplicationError(
                status_code=409,
                detail="Export object verification failed.",
            )
        if stored.sha256 and hashlib.sha256(payload).hexdigest() != stored.sha256:
            raise ApplicationError(
                status_code=409,
                detail="Export object verification failed.",
            )
        return BinaryArtifact(
            content=payload,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(safe)}"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )
    path = EXPORTS_DIR / safe
    if not path.exists():
        raise ApplicationError(status_code=404, detail="Export file not found.")
    return FileArtifact(
        path,
        filename=safe,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

def _is_desktop_embedded() -> bool:
    return os.environ.get("REQPILOT_DESKTOP_EMBEDDED") == "1"

def _reveal_in_file_manager(path: Path) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{path}"])
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
        return
    target_dir = path.parent if path.is_file() else path
    opener = shutil.which("xdg-open")
    if not opener:
        raise RuntimeError("No file manager opener is available on this system.")
    subprocess.Popen([opener, str(target_dir)])

def reveal_export(file_name: str) -> dict:
    from reqpilotx_server.application.requirements import _safe_name
    from reqpilotx_server.application.runtime import EXPORTS_DIR, RUNTIME_SETTINGS

    if (
        not _is_desktop_embedded()
        or RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL
    ):
        raise ApplicationError(
            status_code=404,
            detail="Reveal action is only available in the desktop app.",
        )
    safe = _safe_name(file_name)
    path = EXPORTS_DIR / safe
    if not path.exists():
        raise ApplicationError(status_code=404, detail="Export file not found.")
    try:
        _reveal_in_file_manager(path)
    except Exception as exc:
        raise ApplicationError(
            status_code=500, detail=f"Failed to open export location: {exc}"
        ) from exc
    return {
        "ok": True,
        "file_name": safe,
        "file_path": str(path),
        "export_dir": str(path.parent),
    }

def list_project_exports(project_id: str) -> list[dict]:
    from reqpilotx_server.application.projects import _require_project
    from reqpilotx_server.application.runtime import _public_storage_record

    _require_project(project_id)
    output: list[dict[str, Any]] = []
    for record in db.list_export_jobs(project_id):
        item = _public_storage_record(record)
        file_name = str(item.get("file_name") or "")
        item["download_url"] = (
            f"/api/exports/{quote(file_name)}" if file_name else ""
        )
        output.append(item)
    return output
