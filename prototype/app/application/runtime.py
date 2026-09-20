from __future__ import annotations

import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app import db
from app.application.errors import ApplicationError
from app.application.processing_profiles import (
    build_runtime_capabilities,
    get_runtime_aliyun_connection_probe_receipt,
    get_runtime_capabilities_route,
    invalidate_runtime_capabilities,
    probe_runtime_aliyun_connection,
)
from app.application.runtime_contracts import (
    RuntimeAIProviderSetupRequest,
)
from app.application.reading_visual_configuration import (
    build_reading_visual_setup,
    validate_reading_visual_setup_payload,
)
from app.application.storage.runtime_files import RuntimeObjectFiles
from app.application.source_runtime import bind_source_runtime, clear_source_runtime
from app.application.transport import (
    BackgroundTaskScheduler,
    JsonArtifact,
    RequestContext,
)
from app.infrastructure.runtime_container import (
    RuntimeContainer,
    build_runtime_container,
)
from app.infrastructure.runtime_health import build_target_runtime_readiness
from app.infrastructure.runtime_settings import DatabaseBackend, RuntimeSettings
from app.infrastructure.storage_lock import (
    StorageLease,
    StorageLockError,
    acquire_storage_lease,
)
from app.ports.llm_requirement_review_repository import (
    LlmRequirementReviewRepositoryPort,
)
from app.ports.requirement_enrichment_repository import (
    RequirementEnrichmentRepositoryPort,
)
from app.runtime_paths import resolve_storage_dir
from app.runtime_readiness import (
    build_ai_profile_check,
    build_readiness_report,
    ensure_runtime_directories,
    runtime_directory_map,
)
from app.services.ai_copilot import (
    apply_runtime_ai_provider_setup,
    build_runtime_ai_provider_setup,
    get_runtime_connection_revision,
    is_ascii_runtime_secret,
    list_ai_copilot_profiles,
    load_ai_copilot_config,
    probe_runtime_azure_rbac,
    validate_dashscope_compatible_base_url,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplicationRuntimeState:
    container: RuntimeContainer | None = None
    files: RuntimeObjectFiles | None = None
    test_llm_review_repository_factory: (
        Callable[[], LlmRequirementReviewRepositoryPort] | None
    ) = None
    test_requirement_enrichment_repository_factory: (
        Callable[[], RequirementEnrichmentRepositoryPort] | None
    ) = None


RUNTIME_STATE = ApplicationRuntimeState()

APP_DIR = Path(__file__).resolve().parents[1]

PROTOTYPE_DIR = APP_DIR.parent

STORAGE_DIR = resolve_storage_dir(PROTOTYPE_DIR)

UPLOADS_DIR = STORAGE_DIR / "uploads"

KNOWLEDGE_DIR = STORAGE_DIR / "knowledge"

EXPORTS_DIR = STORAGE_DIR / "exports"

TABLE_SNAPSHOT_DIR = STORAGE_DIR / "table_snapshots"

STATIC_DIR = PROTOTYPE_DIR / "frontend" / "dist"

MAX_DOCUMENT_UPLOAD_BYTES = 128 * 1024 * 1024

UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


def _load_config_json_into_env() -> None:
    """Load config.json values into environment variables.

    This mirrors the config loading logic in app_desktop.py so that
    the app works correctly whether launched via the desktop wrapper
    or directly via uvicorn (web mode).
    """
    config_file = STORAGE_DIR / "config.json"
    if not config_file.exists():
        return
    try:
        import json as _json

        config_data = _json.loads(config_file.read_text(encoding="utf-8"))
        loaded_keys: list[str] = []
        for key, val in config_data.items():
            if val and key not in os.environ:
                os.environ[key] = str(val)
                loaded_keys.append(key)
        if loaded_keys:
            print(
                f"[ReqPilotX] Loaded {len(loaded_keys)} config values from {config_file}"
            )
    except Exception as exc:
        print(f"[ReqPilotX] Warning: Failed to load {config_file}: {exc}")


_load_config_json_into_env()

_APP_STARTUP_STATE = {
    "initialized": False,
    "error": "",
    "checked_at": "",
}

_STORAGE_LEASE: StorageLease | None = None

_STORAGE_LEASE_RELEASE_ON_SHUTDOWN = True

_ARTIFACT_SMOKE_SHUTDOWN_CALLBACK: Callable[[], None] | None = None

_ASSET_RESPONSE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

RUNTIME_SETTINGS = RuntimeSettings.from_environment(prototype_dir=PROTOTYPE_DIR)


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


def acquire_runtime_storage_lease() -> StorageLease:
    global _STORAGE_LEASE
    if _STORAGE_LEASE is None:
        _STORAGE_LEASE = acquire_storage_lease(STORAGE_DIR, blocking=False)
    return _STORAGE_LEASE


def release_runtime_storage_lease() -> None:
    global _STORAGE_LEASE
    if _STORAGE_LEASE is not None:
        _STORAGE_LEASE.release()
        _STORAGE_LEASE = None


def manage_runtime_storage_lease_from_launcher(enabled: bool) -> None:
    global _STORAGE_LEASE_RELEASE_ON_SHUTDOWN
    _STORAGE_LEASE_RELEASE_ON_SHUTDOWN = not bool(enabled)


def register_artifact_smoke_shutdown(callback: Callable[[], None] | None) -> None:
    global _ARTIFACT_SMOKE_SHUTDOWN_CALLBACK
    _ARTIFACT_SMOKE_SHUTDOWN_CALLBACK = callback


def _runtime_record_path(record: dict[str, Any] | None) -> Path:
    if not record:
        raise ValueError("A persisted record is required.")
    storage_key = str(record.get("storage_key") or "").strip()
    if storage_key:
        if RUNTIME_STATE.files is None:
            raise RuntimeError("Runtime object materialization is not initialized.")
        return RUNTIME_STATE.files.resolve_record(record)
    if RUNTIME_SETTINGS.database_backend is not DatabaseBackend.SQLITE_TEST:
        raise RuntimeError("Production records must use a storage_key.")
    file_path = str(record.get("file_path") or "").strip()
    if not file_path:
        raise ValueError("Test record has no file path.")
    return Path(file_path)


def configure_worker_runtime(container: RuntimeContainer) -> None:
    """Bind the shared application use cases to a worker-owned container."""

    if container.database is None or container.jobs is None:
        raise RuntimeError("Worker runtime requires PostgreSQL durable jobs.")
    RUNTIME_STATE.container = container
    db.configure_runtime(
        container.settings,
        postgres_database=container.database,
    )
    RUNTIME_STATE.files = RuntimeObjectFiles(
        container.object_store,
        container.settings.scratch_root / "materialized",
        allow_legacy_paths=False,
    )
    bind_source_runtime(
        settings=container.settings,
        object_store=container.object_store,
        files=RUNTIME_STATE.files,
    )


def _durable_jobs():
    if RUNTIME_STATE.container is None or RUNTIME_STATE.container.jobs is None:
        raise RuntimeError("Durable job runtime is not initialized.")
    return RUNTIME_STATE.container.jobs


def _agent_structure_proposals():
    if (
        RUNTIME_STATE.container is None
        or RUNTIME_STATE.container.agent_structure_proposal_repository is None
    ):
        raise RuntimeError("Agent Structure proposal repository is not initialized.")
    return RUNTIME_STATE.container.agent_structure_proposal_repository


def _agent_structure_loop():
    if (
        RUNTIME_STATE.container is None
        or RUNTIME_STATE.container.agent_structure_loop_repository is None
    ):
        raise RuntimeError("Agent Structure loop repository is not initialized.")
    return RUNTIME_STATE.container.agent_structure_loop_repository


def _agent_structure_handoff_executor():
    if (
        RUNTIME_STATE.container is None
        or RUNTIME_STATE.container.agent_structure_handoff_executor is None
    ):
        raise RuntimeError("Agent Structure handoff executor is not initialized.")
    return RUNTIME_STATE.container.agent_structure_handoff_executor


def _agent_table_materializations():
    if (
        RUNTIME_STATE.container is None
        or RUNTIME_STATE.container.agent_table_materialization_repository is None
    ):
        raise RuntimeError("Agent table materialization repository is not initialized.")
    return RUNTIME_STATE.container.agent_table_materialization_repository


def _agent_structure_steers():
    if (
        RUNTIME_STATE.container is None
        or RUNTIME_STATE.container.agent_structure_steer_repository is None
    ):
        raise RuntimeError("Agent Structure steer repository is not initialized.")
    return RUNTIME_STATE.container.agent_structure_steer_repository


def _agent_runtime():
    if RUNTIME_STATE.container is None or RUNTIME_STATE.container.agent_runtime is None:
        raise RuntimeError("Agent Runtime is not initialized.")
    return RUNTIME_STATE.container.agent_runtime


def _codex_semantic_provider():
    if (
        RUNTIME_STATE.container is None
        or RUNTIME_STATE.container.semantic_provider is None
    ):
        raise RuntimeError("Codex semantic provider is not initialized.")
    return RUNTIME_STATE.container.semantic_provider


def _codex_requirement_atomization_provider():
    if (
        RUNTIME_STATE.container is None
        or RUNTIME_STATE.container.requirement_atomization_provider is None
    ):
        raise RuntimeError("Codex atomization provider is not initialized.")
    return RUNTIME_STATE.container.requirement_atomization_provider


def _agent_customer_data_enabled() -> bool:
    if RUNTIME_STATE.container is None:
        return False
    settings = getattr(RUNTIME_STATE.container, "settings", None)
    return bool(getattr(settings, "agent_customer_data_enabled", False))


def _public_storage_record(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    payload.pop("storage_key", None)
    payload.pop("file_path", None)
    payload.pop("payload_storage_key", None)
    payload.pop("payload_path", None)
    return payload


def _build_health_response(*, runtime_only: bool = False) -> JsonArtifact:
    ai_profiles = list_ai_copilot_profiles()
    startup_initialized = bool(_APP_STARTUP_STATE.get("initialized"))
    startup_error = str(_APP_STARTUP_STATE.get("error") or "")
    startup_checked_at = str(_APP_STARTUP_STATE.get("checked_at") or "")
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        payload = build_target_runtime_readiness(
            container=RUNTIME_STATE.container,
            ai_profiles=ai_profiles,
            startup_initialized=startup_initialized,
            startup_error=startup_error,
            startup_checked_at=startup_checked_at,
        )
    else:
        ai_config = ai_profiles[0] if ai_profiles else load_ai_copilot_config()
        payload = build_readiness_report(
            storage_root=STORAGE_DIR,
            runtime_directories=_runtime_directories(ai_config),
            db_path=db.DB_PATH,
            ai_profiles=ai_profiles,
            startup_initialized=startup_initialized,
            startup_error=startup_error,
            startup_checked_at=startup_checked_at,
        )
    readiness_field = "runtime_ready" if runtime_only else "ready"
    status_code = 200 if payload[readiness_field] else 503
    return JsonArtifact(status_code=status_code, content=payload)


def _artifact_smoke_identity(request: RequestContext) -> dict[str, Any]:
    expected_nonce = str(os.getenv("REQPILOT_ARTIFACT_SMOKE_NONCE") or "")
    supplied_nonce = str(request.headers.get("X-ReqPilotX-Smoke-Nonce") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_nonce) or not secrets.compare_digest(
        supplied_nonce, expected_nonce
    ):
        raise ApplicationError(status_code=404, detail="Not found.")
    host = str(os.getenv("REQPILOT_ARTIFACT_SMOKE_HOST") or "")
    try:
        port = int(os.getenv("REQPILOT_ARTIFACT_SMOKE_PORT") or "0")
    except ValueError as exc:
        raise ApplicationError(status_code=404, detail="Not found.") from exc
    if host != "127.0.0.1" or not 1 <= port <= 65535:
        raise ApplicationError(status_code=404, detail="Not found.")
    return {
        "pid": os.getpid(),
        "port": port,
        "nonce": expected_nonce,
        "host": host,
    }


def startup() -> None:
    ai_config = load_ai_copilot_config()
    try:
        if RUNTIME_SETTINGS.database_backend is DatabaseBackend.SQLITE_TEST:
            acquire_runtime_storage_lease()
            ensure_runtime_directories(_runtime_directories(ai_config))
        RUNTIME_STATE.container = build_runtime_container(
            RUNTIME_SETTINGS,
            apply_migrations=True,
            sqlite_database_path=(db.DB_PATH if RUNTIME_SETTINGS.is_test else None),
        )
        db.configure_runtime(
            RUNTIME_SETTINGS,
            postgres_database=RUNTIME_STATE.container.database,
        )
        if RUNTIME_SETTINGS.database_backend is DatabaseBackend.SQLITE_TEST:
            db.init_db()
        RUNTIME_STATE.files = RuntimeObjectFiles(
            RUNTIME_STATE.container.object_store,
            RUNTIME_SETTINGS.scratch_root / "materialized",
            allow_legacy_paths=RUNTIME_SETTINGS.is_test,
        )
        bind_source_runtime(
            settings=RUNTIME_SETTINGS,
            object_store=RUNTIME_STATE.container.object_store,
            files=RUNTIME_STATE.files,
        )
    except StorageLockError:
        _record_startup_state(initialized=False, error="StorageLockError")
        logger.error(
            "Application startup aborted because the storage root is already active."
        )
        raise
    except Exception as exc:
        error_type = type(exc).__name__
        if RUNTIME_STATE.container is not None:
            RUNTIME_STATE.container.close()
            RUNTIME_STATE.container = None
        RUNTIME_STATE.files = None
        clear_source_runtime()
        _record_startup_state(initialized=False, error=error_type)
        logger.error(
            "Application startup initialization failed. error_type=%s",
            error_type,
        )
        if not RUNTIME_SETTINGS.is_test:
            raise
        return
    _record_startup_state(initialized=True)
    logger.info(
        "Application startup initialization completed. storage_root=%s db_path=%s assessment_mode=%s retrieval_backend=%s",
        STORAGE_DIR,
        db.DB_PATH,
        ai_config.assessment_mode,
        ai_config.retrieval_backend,
    )


def shutdown() -> None:
    if _STORAGE_LEASE_RELEASE_ON_SHUTDOWN:
        release_runtime_storage_lease()
    if RUNTIME_STATE.container is not None:
        RUNTIME_STATE.container.close()
        RUNTIME_STATE.container = None
    RUNTIME_STATE.files = None
    clear_source_runtime()


def get_runtime_ai_provider_setup_route() -> dict[str, Any]:
    return _build_runtime_ai_provider_setup_response()


def get_runtime_worker_fleet_route() -> dict[str, Any]:
    """Return the public worker acknowledgement state for current settings.

    This settings-level projection is deliberately revision-bound even when no
    specific extraction plan is supplied.  Plan-specific preflight separately
    exempts a fully local plan from connection revision changes.
    """

    desired_revision = get_runtime_connection_revision()
    unavailable = {
        "schema_version": "runtime-worker-fleet-status/v1",
        "state": "UNAVAILABLE",
        "desired_connection_revision": desired_revision,
        "required_kind": "document.extract",
        "revision_required": True,
        "registered_worker_count": 0,
        "live_worker_count": 0,
        "eligible_worker_count": 0,
        "idle_worker_count": 0,
        "active_worker_count": 0,
        "draining_worker_count": 0,
        "stale_revision_worker_count": 0,
        "workers": [],
    }
    try:
        jobs = _durable_jobs()
    except RuntimeError:
        return unavailable
    worker_fleet_status = getattr(jobs, "worker_fleet_status", None)
    if not callable(worker_fleet_status):
        return unavailable
    fleet = worker_fleet_status(
        desired_revision=desired_revision,
        required_kind="document.extract",
        require_connection_revision=True,
        plan={
            "source_revision": str(os.getenv("REQPILOT_RUNTIME_SOURCE_REVISION") or ""),
            "runtime_instance_id": str(
                os.getenv("REQPILOT_LOCAL_RUNTIME_INSTANCE_ID") or ""
            ),
        },
    )
    return fleet if isinstance(fleet, dict) else unavailable


def update_runtime_ai_provider_setup_route(
    payload: RuntimeAIProviderSetupRequest,
) -> dict[str, Any]:
    payload_dict = payload.model_dump(exclude_none=True)
    _validate_runtime_ai_provider_setup_payload(payload_dict)
    apply_runtime_ai_provider_setup(payload_dict)
    invalidate_runtime_capabilities()
    build_runtime_capabilities(probe=True)
    return _build_runtime_ai_provider_setup_response()


def probe_runtime_azure_rbac_route() -> dict[str, Any]:
    return probe_runtime_azure_rbac()


def probe_runtime_aliyun_connection_route() -> dict[str, Any]:
    _validate_dashscope_base_url_or_raise(
        load_ai_copilot_config("aliyun").dashscope_base_url
    )
    return probe_runtime_aliyun_connection()


def _build_runtime_ai_provider_setup_response() -> dict[str, Any]:
    payload = build_runtime_ai_provider_setup()
    connection_revision = int(payload.get("connection_revision") or 0)
    aliyun_setup = payload.get("aliyun")
    if isinstance(aliyun_setup, dict):
        aliyun_setup["reading_visual"] = build_reading_visual_setup(
            connection_revision=connection_revision,
        )
    aliyun_probe = get_runtime_aliyun_connection_probe_receipt()
    if aliyun_probe is not None:
        payload["aliyun_probe"] = aliyun_probe
    runtime_checks = {
        config.profile_id: build_ai_profile_check(config)
        for config in list_ai_copilot_profiles()
    }
    merged_profiles = [
        _merge_ai_profile_runtime_state(
            dict(profile), runtime_checks.get(str(profile.get("id") or "").strip(), {})
        )
        for profile in payload.get("profiles") or []
    ]
    payload["profiles"] = merged_profiles
    for profile_id in ("azure", "aliyun"):
        section = payload.get(profile_id)
        if isinstance(section, dict):
            payload[profile_id] = _merge_ai_profile_runtime_state(
                dict(section), runtime_checks.get(profile_id, {})
            )
    capability_payload = get_runtime_capabilities_route()["capabilities"]
    payload["capabilities"] = capability_payload
    payload["worker_fleet"] = get_runtime_worker_fleet_route()
    aliyun_required = {"aliyun.llm", "aliyun.embedding", "qdrant"}
    aliyun_by_id = {
        str(item.get("capability_id") or ""): item
        for item in capability_payload
        if str(item.get("capability_id") or "") in aliyun_required
        and int(item.get("connection_revision") or -1) == connection_revision
    }
    aliyun_llm_ready = bool(aliyun_by_id.get("aliyun.llm", {}).get("ready") is True)
    aliyun_retrieval_ready = bool(
        aliyun_by_id.get("aliyun.embedding", {}).get("ready") is True
        and aliyun_by_id.get("qdrant", {}).get("ready") is True
    )
    aliyun_ready = aliyun_llm_ready and aliyun_retrieval_ready
    for profile in merged_profiles:
        if str(profile.get("id") or "").strip() == "aliyun":
            profile.update(
                {
                    "ready": aliyun_ready,
                    "llm_ready": aliyun_llm_ready,
                    "retrieval_ready": aliyun_retrieval_ready,
                }
            )
    aliyun_section = payload.get("aliyun")
    if isinstance(aliyun_section, dict):
        aliyun_section.update(
            {
                "ready": aliyun_ready,
                "llm_ready": aliyun_llm_ready,
                "retrieval_ready": aliyun_retrieval_ready,
            }
        )
    return payload


def _validate_runtime_ai_provider_setup_payload(payload: dict[str, Any]) -> None:
    aliyun_payload = payload.get("aliyun") or {}
    reading_visual_payload = aliyun_payload.get("reading_visual")
    if reading_visual_payload is not None:
        try:
            validate_reading_visual_setup_payload(reading_visual_payload)
        except ValueError as exc:
            raise ApplicationError(
                status_code=422,
                detail="Reading visual provider settings are fixed by policy.",
            ) from exc
    _validate_ascii_runtime_secret(
        "DashScope API Key",
        aliyun_payload.get("api_key"),
    )
    _validate_ascii_runtime_secret(
        "Qdrant API Key",
        aliyun_payload.get("qdrant_api_key"),
    )
    default_profile_id = str(payload.get("default_profile_id") or "").strip().lower()
    if default_profile_id == "aliyun" or aliyun_payload:
        _validate_dashscope_base_url_or_raise(
            str(
                aliyun_payload.get("base_url")
                or load_ai_copilot_config("aliyun").dashscope_base_url
            )
        )
    azure_payload = payload.get("azure") or {}
    _validate_ascii_runtime_secret(
        "Azure Document Intelligence API Key",
        azure_payload.get("document_intelligence_api_key"),
    )


def _validate_ascii_runtime_secret(label: str, value: Any) -> None:
    text = str(value or "").strip()
    if not text:
        return
    if is_ascii_runtime_secret(text):
        if (
            label == "DashScope API Key"
            and not _is_test_runtime()
            and (
                len(text) < 20
                or len(text) > 256
                or any(character.isspace() for character in text)
            )
        ):
            raise ApplicationError(
                status_code=422,
                detail=(
                    "DashScope API Key looks like a placeholder or truncated value. "
                    "Re-enter the complete provider key."
                ),
            )
        return
    raise ApplicationError(
        status_code=422,
        detail=f"{label} contains non-ASCII characters. Re-enter the raw key without Chinese punctuation or full-width characters.",
    )


def _is_test_runtime() -> bool:
    return str(os.getenv("REQPILOT_TEST_MODE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _validate_dashscope_base_url_or_raise(value: Any) -> str:
    try:
        return validate_dashscope_compatible_base_url(str(value or ""))
    except ValueError as exc:
        raise ApplicationError(
            status_code=422,
            detail=(
                "Aliyun Base URL must be an approved DashScope HTTPS "
                "compatible-mode endpoint."
            ),
        ) from exc


def _merge_ai_profile_runtime_state(
    profile_payload: dict[str, Any],
    runtime_check: dict[str, Any] | None,
) -> dict[str, Any]:
    check = runtime_check or {}
    runtime_missing = [
        str(item).strip() for item in check.get("missing") or [] if str(item).strip()
    ]
    configured_missing = [
        str(item).strip()
        for item in profile_payload.get("missing") or []
        if str(item).strip()
    ]
    # Static configuration and the live runtime probe must both agree.  This
    # keeps an Azure heuristic fallback from masquerading as a ready remote
    # profile and also fails closed when a configured Aliyun Qdrant is down.
    base_ready = profile_payload.get("ready")
    check_ready = check.get("runnable")
    if base_ready is None:
        effective_ready = bool(check_ready)
    elif check_ready is None:
        effective_ready = bool(base_ready)
    else:
        effective_ready = bool(base_ready) and bool(check_ready)
    return {
        **profile_payload,
        "ready": effective_ready,
        "runtime_status": str(check.get("status") or ""),
        "runtime_detail": str(check.get("detail") or ""),
        "runtime_missing": runtime_missing,
        "missing": runtime_missing or configured_missing,
    }


def artifact_smoke_identity(request: RequestContext) -> dict[str, Any]:
    return _artifact_smoke_identity(request)


def artifact_smoke_shutdown(
    request: RequestContext,
    background_tasks: BackgroundTaskScheduler,
) -> dict[str, Any]:
    identity = _artifact_smoke_identity(request)
    callback = _ARTIFACT_SMOKE_SHUTDOWN_CALLBACK
    if callback is None:
        raise ApplicationError(status_code=404, detail="Not found.")
    background_tasks.add_task(callback)
    return {"shutting_down": True, "pid": identity["pid"]}
