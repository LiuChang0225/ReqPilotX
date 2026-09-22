from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from reqpilotx_server.infrastructure.runtime_container import RuntimeContainer
from reqpilotx_server.runtime_readiness import build_ai_profile_check


def build_target_runtime_readiness(
    *,
    container: RuntimeContainer | None,
    ai_profiles: list[Any],
    startup_initialized: bool,
    startup_error: str,
    startup_checked_at: str,
) -> dict[str, Any]:
    """Build a redacted readiness report for the PostgreSQL/object runtime."""

    checks: dict[str, dict[str, Any]] = {
        "startup": _startup_check(
            initialized=startup_initialized,
            error=startup_error,
            checked_at=startup_checked_at,
        ),
        "postgresql": _database_check(container),
        "object_store": _object_store_check(container),
    }
    profile_checks: dict[str, dict[str, Any]] = {}
    runnable_profiles: list[str] = []
    for config in ai_profiles:
        check = build_ai_profile_check(config)
        profile_checks[config.profile_id] = check
        checks[config.profile_id] = check
        if bool(check.get("runnable")):
            runnable_profiles.append(config.profile_id)

    default_profile_id = ai_profiles[0].profile_id if ai_profiles else ""
    default_profile_ready = bool(
        default_profile_id
        and profile_checks.get(default_profile_id, {}).get("runnable")
    )
    runtime_ready = all(
        checks[name].get("status") == "ok"
        for name in ("startup", "postgresql", "object_store")
    )
    ready = runtime_ready and default_profile_ready
    settings = container.settings if container is not None else None
    return {
        "status": "ok" if ready else "not_ready",
        "ready": ready,
        "runtime_ready": runtime_ready,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "deployment_mode": (
            settings.deployment_mode.value if settings is not None else "unknown"
        ),
        "database_backend": (
            settings.database_backend.value if settings is not None else "unknown"
        ),
        "object_store_backend": (
            settings.object_store_backend.value if settings is not None else "unknown"
        ),
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
                    "runnable": bool(
                        profile_checks.get(config.profile_id, {}).get("runnable")
                    ),
                }
                for config in ai_profiles
            ],
        },
        "checks": checks,
    }


def _startup_check(
    *,
    initialized: bool,
    error: str,
    checked_at: str,
) -> dict[str, Any]:
    if initialized:
        return {
            "status": "ok",
            "checked_at": checked_at,
            "detail": "Application startup completed.",
        }
    return {
        "status": "error",
        "checked_at": checked_at,
        "detail": (
            f"Application startup failed ({error})."
            if error
            else "Application startup has not completed."
        ),
    }


def _database_check(container: RuntimeContainer | None) -> dict[str, Any]:
    if container is None or container.database is None:
        return {
            "status": "error",
            "detail": "PostgreSQL runtime is not initialized.",
        }
    try:
        container.database.healthcheck()
    except Exception as exc:
        return {
            "status": "error",
            "detail": f"PostgreSQL healthcheck failed ({type(exc).__name__}).",
        }
    return {
        "status": "ok",
        "detail": "PostgreSQL connection and schema initialization are available.",
    }


def _object_store_check(container: RuntimeContainer | None) -> dict[str, Any]:
    if container is None:
        return {
            "status": "error",
            "detail": "Object storage runtime is not initialized.",
        }
    try:
        # A HEAD/stat against a reserved absent key is read-only and verifies
        # the configured local root or private OSS access boundary.
        container.object_store.stat("_runtime/healthcheck")
    except Exception as exc:
        return {
            "status": "error",
            "detail": f"Object storage healthcheck failed ({type(exc).__name__}).",
        }
    return {
        "status": "ok",
        "detail": "Object storage is reachable.",
    }


__all__ = ["build_target_runtime_readiness"]
