from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

from app.services.ai_copilot import (
    ALIYUN_API_KEY_NON_ASCII_REASON,
    ALIYUN_QDRANT_API_KEY_NON_ASCII_REASON,
    ALIYUN_RETRIEVAL_DISABLED_MESSAGE,
    ALIYUN_RETRIEVAL_DISABLED_REASON,
    ALIYUN_QDRANT_UNREACHABLE_REASON,
    AICopilotConfig,
    DISABLED_RETRIEVAL_BACKEND,
    is_ascii_runtime_secret,
)


_REQUIRED_DB_TABLES = ("projects", "sor_documents", "requirements")


def runtime_directory_map(
    *,
    storage_dir: Path,
    uploads_dir: Path,
    knowledge_dir: Path,
    exports_dir: Path,
    table_snapshot_dir: Path,
    ai_trace_dir: Path,
) -> dict[str, Path]:
    return {
        "storage_root": storage_dir,
        "uploads": uploads_dir,
        "knowledge": knowledge_dir,
        "exports": exports_dir,
        "table_snapshots": table_snapshot_dir,
        "ai_traces": ai_trace_dir,
    }


def ensure_runtime_directories(directory_map: dict[str, Path]) -> None:
    seen: set[str] = set()
    for path in directory_map.values():
        key = str(path)
        if key in seen:
            continue
        path.mkdir(parents=True, exist_ok=True)
        seen.add(key)


def build_readiness_report(
    *,
    storage_root: Path,
    runtime_directories: dict[str, Path],
    db_path: Path,
    ai_profiles: list[AICopilotConfig],
    startup_initialized: bool,
    startup_error: str,
    startup_checked_at: str,
) -> dict[str, Any]:
    checks = {
        "startup": _build_startup_check(
            startup_initialized=startup_initialized,
            startup_error=startup_error,
            startup_checked_at=startup_checked_at,
        ),
        "storage": _build_storage_check(storage_root=storage_root, runtime_directories=runtime_directories),
        "sqlite": _build_sqlite_check(db_path=db_path),
    }
    profile_checks: dict[str, dict[str, Any]] = {}
    runnable_profiles: list[str] = []
    for config in ai_profiles:
        check = _build_ai_profile_check(config)
        profile_checks[config.profile_id] = check
        checks[config.profile_id] = check
        if bool(check.get("runnable")):
            runnable_profiles.append(config.profile_id)

    default_profile_id = ai_profiles[0].profile_id if ai_profiles else ""
    default_profile_ready = bool(
        default_profile_id
        and profile_checks.get(default_profile_id, {}).get("runnable")
    )
    core_ready = all(
        checks[name].get("status") in {"ok", "not_applicable"}
        for name in ("startup", "storage", "sqlite")
    )
    ready = core_ready and default_profile_ready
    return {
        "status": "ok" if ready else "not_ready",
        "ready": ready,
        "runtime_ready": core_ready,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "storage_root": "redacted",
        "db_path": "redacted",
        "ai": {
            "default_profile_id": default_profile_id,
            "default_profile_ready": default_profile_ready,
            "runnable_profiles": runnable_profiles,
            "profiles": [
                {
                    "profile_id": config.profile_id,
                    "provider": config.provider,
                    "assessment_mode": config.assessment_mode,
                    "retrieval_backend": config.retrieval_backend,
                    "auth_mode": config.auth_mode,
                    "runnable": bool(profile_checks.get(config.profile_id, {}).get("runnable")),
                }
                for config in ai_profiles
            ],
        },
        "checks": checks,
    }


def build_ai_profile_check(config: AICopilotConfig) -> dict[str, Any]:
    return _build_ai_profile_check(config)


def _build_ai_profile_check(config: AICopilotConfig) -> dict[str, Any]:
    if config.provider == "aliyun":
        return _build_aliyun_check(config)
    return _build_azure_check(config)


def _build_startup_check(
    *,
    startup_initialized: bool,
    startup_error: str,
    startup_checked_at: str,
) -> dict[str, Any]:
    if startup_initialized:
        return {
            "status": "ok",
            "checked_at": startup_checked_at,
            "detail": "Application startup initialization completed successfully.",
        }
    if startup_error:
        return {
            "status": "error",
            "checked_at": startup_checked_at,
            "detail": "Application startup initialization failed.",
        }
    return {
        "status": "error",
        "checked_at": startup_checked_at,
        "detail": "Application startup initialization has not completed.",
    }


def _build_storage_check(*, storage_root: Path, runtime_directories: dict[str, Path]) -> dict[str, Any]:
    root_probe = _probe_directory(storage_root)
    directory_probes = {
        name: _probe_directory(path)
        for name, path in runtime_directories.items()
        if name != "storage_root"
    }
    failed = [name for name, probe in directory_probes.items() if probe.get("status") != "ok"]
    if root_probe.get("status") != "ok":
        failed.insert(0, "storage_root")
    status = "ok" if not failed else "error"
    detail = "Storage root and runtime directories are writable." if not failed else (
        "Storage readiness failed for: " + ", ".join(failed)
    )
    return {
        "status": status,
        "detail": detail,
        "root": root_probe,
        "directories": directory_probes,
    }


def _probe_directory(path: Path) -> dict[str, Any]:
    probe_path = path / f".reqpilotx-readiness-{uuid.uuid4().hex}.tmp"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe_path.write_text("ready", encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        return {
            "status": "ok",
            "detail": "Directory is writable.",
        }
    except Exception as exc:
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "status": "error",
            "detail": "Directory is not writable.",
            "error_type": type(exc).__name__,
        }


def _build_sqlite_check(*, db_path: Path) -> dict[str, Any]:
    missing_tables = list(_REQUIRED_DB_TABLES)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path), timeout=2.0) as conn:
            conn.execute("SELECT 1")
            missing_tables = [
                table
                for table in _REQUIRED_DB_TABLES
                if conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                is None
            ]
            if missing_tables:
                return {
                    "status": "error",
                    "detail": "SQLite schema is not initialized.",
                    "missing_tables": missing_tables,
                }
            probe_table = f"_reqpilotx_readiness_{uuid.uuid4().hex}"
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    f'CREATE TABLE "{probe_table}" (probe_value INTEGER NOT NULL)'
                )
                conn.execute(
                    f'INSERT INTO "{probe_table}" (probe_value) VALUES (1)'
                )
            finally:
                conn.rollback()
        return {
            "status": "ok",
            "detail": "SQLite read/write probe succeeded and required tables are present.",
            "missing_tables": [],
        }
    except Exception as exc:
        return {
            "status": "error",
            "detail": "SQLite read/write probe failed.",
            "error_type": type(exc).__name__,
            "missing_tables": missing_tables,
        }


def _build_azure_check(config: AICopilotConfig) -> dict[str, Any]:
    llm_required = config.llm_enabled
    azure_sdk_required = config.retrieval_backend == "azure_sdk"
    azure_haystack_required = config.retrieval_backend == "azure_haystack"
    azure_required = llm_required or azure_sdk_required or azure_haystack_required
    if not azure_required:
        return {
            "status": "not_applicable",
            "detail": "Current AI mode uses heuristic/local dependencies only.",
            "provider": config.provider,
            "profile_id": config.profile_id,
            "runnable": True,
            "required_for": [],
            "missing": [],
            "assessment_mode": config.assessment_mode,
            "retrieval_backend": config.retrieval_backend,
            "azure_auth_mode": config.azure_auth_mode,
        }

    required_for: list[str] = []
    missing: list[str] = []

    if llm_required:
        required_for.append("llm_assessment")
        _append_missing(missing, "AZURE_OPENAI_ENDPOINT", config.azure_openai_endpoint)
        _append_missing(missing, "AZURE_OPENAI_CHAT_DEPLOYMENT", config.azure_openai_chat_deployment)
        if config.azure_auth_mode == "api_key":
            _append_missing(missing, "AZURE_OPENAI_API_KEY", config.azure_openai_api_key)

    if azure_sdk_required or azure_haystack_required:
        required_for.append("azure_retrieval")
        _append_missing(missing, "AZURE_OPENAI_ENDPOINT", config.azure_openai_endpoint)
        _append_missing(missing, "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", config.azure_openai_embedding_deployment)
        _append_missing(missing, "AZURE_AI_SEARCH_ENDPOINT", config.azure_search_endpoint)
        _append_missing(missing, "AZURE_AI_SEARCH_INDEX_NAME", config.azure_search_index_name)
        if config.azure_auth_mode == "api_key":
            _append_missing(missing, "AZURE_OPENAI_API_KEY", config.azure_openai_api_key)
        if azure_haystack_required:
            _append_missing(missing, "AZURE_AI_SEARCH_API_KEY", config.azure_search_api_key)

    status = "ok" if not missing else "error"
    detail = "Azure runtime configuration required by the selected AI mode is present." if not missing else (
        "Azure runtime configuration is incomplete for the selected AI mode."
    )
    missing = _dedupe_missing(missing)
    return {
        "status": status,
        "detail": detail,
        "provider": config.provider,
        "profile_id": config.profile_id,
        "runnable": status in {"ok", "not_applicable"},
        "required_for": required_for,
        "missing": missing,
        "assessment_mode": config.assessment_mode,
        "retrieval_backend": config.retrieval_backend,
        "azure_auth_mode": config.azure_auth_mode,
        "azure_openai_chat_ready": config.azure_openai_chat_ready,
        "azure_openai_embedding_ready": config.azure_openai_embedding_ready,
        "azure_search_ready": config.azure_search_ready,
    }


def _build_aliyun_check(config: AICopilotConfig) -> dict[str, Any]:
    llm_required = config.llm_enabled
    required_for: list[str] = []
    missing: list[str] = []
    qdrant_probe: dict[str, Any] = {
        "status": "not_configured",
        "detail": "Qdrant local POC is not enabled for the Aliyun profile.",
        "url": _safe_endpoint_label(config.qdrant_url),
    }

    if llm_required:
        required_for.append("llm_assessment")
        if not config.dashscope_api_key:
            missing.append("REQPILOT_ALIYUN_API_KEY")
        elif not is_ascii_runtime_secret(config.dashscope_api_key):
            missing.append(ALIYUN_API_KEY_NON_ASCII_REASON)
        _append_missing(missing, "REQPILOT_DASHSCOPE_BASE_URL", config.dashscope_base_url)
        _append_missing(missing, "REQPILOT_ALIYUN_CHAT_MODEL", config.chat_model)

    if config.retrieval_backend == DISABLED_RETRIEVAL_BACKEND:
        required_for.append("local_qdrant_poc_enablement")
        missing.append(ALIYUN_RETRIEVAL_DISABLED_REASON)
    elif config.retrieval_backend == "qdrant":
        required_for.append("local_qdrant_retrieval")
        if not config.dashscope_api_key:
            missing.append("REQPILOT_ALIYUN_API_KEY")
        elif not is_ascii_runtime_secret(config.dashscope_api_key):
            missing.append(ALIYUN_API_KEY_NON_ASCII_REASON)
        _append_missing(missing, "REQPILOT_DASHSCOPE_BASE_URL", config.dashscope_base_url)
        _append_missing(missing, "REQPILOT_ALIYUN_EMBEDDING_MODEL", config.embedding_model)
        _append_missing(missing, "REQPILOT_ALIYUN_QDRANT_URL", config.qdrant_url)
        _append_missing(missing, "REQPILOT_ALIYUN_QDRANT_COLLECTION", config.qdrant_collection_name)
        if config.qdrant_api_key and not is_ascii_runtime_secret(config.qdrant_api_key):
            missing.append(ALIYUN_QDRANT_API_KEY_NON_ASCII_REASON)
        if not missing:
            qdrant_probe = _probe_qdrant_endpoint(config)
            if qdrant_probe.get("status") != "ok":
                missing.append(ALIYUN_QDRANT_UNREACHABLE_REASON)
    else:
        required_for.append("local_qdrant_poc_enablement")
        missing.append("REQPILOT_ALIYUN_RETRIEVAL_BACKEND=qdrant")

    status = "ok" if not missing else "error"
    if not missing:
        detail = "Aliyun DashScope + local Qdrant POC runtime is ready."
    elif ALIYUN_RETRIEVAL_DISABLED_REASON in missing:
        detail = ALIYUN_RETRIEVAL_DISABLED_MESSAGE
    elif ALIYUN_QDRANT_UNREACHABLE_REASON in missing:
        detail = str(qdrant_probe.get("detail") or "Aliyun local Qdrant service is unreachable.")
    elif ALIYUN_API_KEY_NON_ASCII_REASON in missing:
        detail = "Aliyun DashScope API key contains non-ASCII characters."
    elif ALIYUN_QDRANT_API_KEY_NON_ASCII_REASON in missing:
        detail = "Qdrant API key contains non-ASCII characters."
    else:
        detail = "Aliyun local Qdrant POC configuration is incomplete."
    missing = _dedupe_missing(missing)
    return {
        "status": status,
        "detail": detail,
        "provider": config.provider,
        "profile_id": config.profile_id,
        "runnable": status in {"ok", "not_applicable"},
        "required_for": required_for,
        "missing": missing,
        "assessment_mode": config.assessment_mode,
        "retrieval_backend": config.retrieval_backend,
        "auth_mode": config.auth_mode,
        "dashscope_chat_ready": config.dashscope_chat_ready,
        "dashscope_embedding_ready": config.dashscope_embedding_ready,
        "dashvector_ready": False,
        "qdrant_ready": config.qdrant_ready,
        "qdrant_probe": qdrant_probe,
        "retrieval_blocked": ALIYUN_RETRIEVAL_DISABLED_REASON in missing,
    }


def _append_missing(missing: list[str], env_name: str, value: str) -> None:
    if str(value or "").strip():
        return
    if env_name not in missing:
        missing.append(env_name)


def _dedupe_missing(missing: list[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in missing if str(item)))


def _probe_qdrant_endpoint(config: AICopilotConfig) -> dict[str, Any]:
    url = str(config.qdrant_url or "").strip().rstrip("/")
    safe_url = _safe_endpoint_label(url)
    if not url:
        return {
            "status": "not_configured",
            "detail": "Qdrant URL is not configured.",
            "url": "",
        }

    request = urllib_request.Request(f"{url}/collections")
    if str(config.qdrant_api_key or "").strip():
        request.add_header("api-key", str(config.qdrant_api_key or "").strip())
    try:
        with urllib_request.urlopen(request, timeout=2.0) as response:
            status_code = int(getattr(response, "status", 200) or 200)
        if 200 <= status_code < 300:
            return {
                "status": "ok",
                "detail": "Qdrant local service responded successfully.",
                "url": safe_url,
                "http_status": status_code,
            }
        return {
            "status": "error",
            "detail": f"Qdrant local service returned HTTP {status_code}.",
            "url": safe_url,
            "http_status": status_code,
        }
    except urllib_error.HTTPError as exc:
        return {
            "status": "error",
            "detail": f"Qdrant local service returned HTTP {exc.code}.",
            "url": safe_url,
            "http_status": int(exc.code),
        }
    except Exception as exc:
        return {
            "status": "error",
            "detail": "Qdrant local service is unreachable.",
            "url": safe_url,
            "error_type": type(exc).__name__,
        }


def _safe_endpoint_label(value: str) -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    try:
        parsed = urlsplit(raw_value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "configured"
        hostname = parsed.hostname
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{hostname}{port}"
    except ValueError:
        return "configured"
