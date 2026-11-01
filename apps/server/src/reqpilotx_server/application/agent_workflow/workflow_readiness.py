"""Read-only readiness projection for the Agent-native document workflow.

This projection is independent from the legacy project AI-profile report. The
composed Agent backend selects its runtime checks. Provider configuration is
checked offline and never establishes live authentication or model availability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from reqpilotx_server.infrastructure.runtime_settings import AgentRuntimeBackend


AGENT_WORKFLOW_REQUIRED_JOB_KINDS = (
    "agent.document-map",
    "agent.source-item.decompose",
    "agent.source-item.analyze",
    "agent.source-item.classify",
    "agent.source-item.images",
    "agent.source-requirement.baseline",
    "agent.source-item-text.baseline",
    "agent.source-item.scope-revision",
)
PROVIDER_WORKFLOW_REQUIRED_JOB_KINDS = AGENT_WORKFLOW_REQUIRED_JOB_KINDS + (
    "agent.source-item.extract-loop",
    "agent.source-item.loop",
)

_CODEX_REASON_CODES = {
    "reading_codex_auth_required": "CODEX_AUTH_REQUIRED",
    "reading_codex_model_unavailable": "CODEX_MODEL_UNAVAILABLE",
    "reading_codex_not_configured": "CODEX_RUNTIME_NOT_CONFIGURED",
    "reading_codex_usage_limited": "CODEX_USAGE_LIMITED",
    "reading_codex_unavailable": "CODEX_RUNTIME_UNAVAILABLE",
}


def build_agent_workflow_readiness(
    *,
    container: Any | None,
    codex_auth_ready: bool,
    codex_auth_reason: str | None,
    protocol_reason: str | None,
    source_reader_ready: bool,
    source_revision: str = "",
    runtime_instance_id: str = "",
    runtime_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build core and selected-runtime readiness, with explicit evidence scope."""

    settings = None if container is None else getattr(container, "settings", None)
    backend = getattr(settings, "agent_runtime_backend", None)
    checks = {
        "postgresql": _postgresql_check(container),
        "object_store": _object_store_check(container),
        "durable_worker": _durable_worker_check(
            container,
            source_revision=source_revision,
            runtime_instance_id=runtime_instance_id,
            required_job_kinds=(
                PROVIDER_WORKFLOW_REQUIRED_JOB_KINDS
                if backend is AgentRuntimeBackend.PROVIDER_TOOL_LOOP
                else AGENT_WORKFLOW_REQUIRED_JOB_KINDS
            ),
        ),
    }
    runtime_id = str(getattr(backend, "value", backend) or "none")
    provider_id = None
    model_name = str(getattr(settings, "agent_model_id", "") or "").strip() or None
    scope = "authentication_and_core"
    if backend is AgentRuntimeBackend.CODEX_APP_SERVER:
        checks["codex_auth"] = _codex_auth_check(
            ready=codex_auth_ready,
            reason=codex_auth_reason,
        )
        checks["protocol_lock"] = _protocol_lock_check(protocol_reason)
    elif backend is AgentRuntimeBackend.PROVIDER_TOOL_LOOP:
        scope = "configuration_and_core"
        projection = runtime_readiness or {}
        provider_id = projection.get("provider_id")
        model_name = projection.get("model_name")
        common = {
            "runtime_id": runtime_id,
            "provider_id": provider_id,
            "model_name": model_name,
            "readiness_scope": "configuration",
            "authentication_verified": False,
        }
        configured = (
            projection.get("configured") is True
            and projection.get("runtime_id") == runtime_id
            and provider_id in ("azure", "aliyun")
            and isinstance(model_name, str)
            and bool(model_name.strip())
            and projection.get("readiness_scope") == "configuration"
            and projection.get("authentication_verified") is False
        )
        if configured:
            checks["provider_configuration"] = _ready_check(
                "Provider profile is configured; live authentication and model availability have not been verified.",
                **common,
            )
        else:
            checks["provider_configuration"] = _failed_check(
                "PROVIDER_CONFIGURATION_UNAVAILABLE",
                "The selected provider profile or configured model is unavailable.",
                **common,
            )
        authorized = getattr(settings, "agent_customer_data_enabled", False) is True
        checks["customer_data_authorization"] = (
            _ready_check("Customer-document processing is enabled in runtime settings.")
            if authorized
            else _failed_check(
                "AGENT_CUSTOMER_DATA_DISABLED",
                "Customer-document processing is disabled in runtime settings.",
            )
        )
    else:
        scope = "configuration_and_core"
        missing = backend is None or backend is AgentRuntimeBackend.NONE
        checks["agent_runtime"] = _failed_check(
            "AGENT_RUNTIME_NOT_CONFIGURED" if missing else "AGENT_RUNTIME_UNSUPPORTED",
            "No supported Agent runtime is selected.",
        )
    checks["source_reader"] = _source_reader_check(source_reader_ready)
    blocking_reason_codes = [
        str(check["reason_code"])
        for check in checks.values()
        if not check["ready"]
    ]
    ready = not blocking_reason_codes
    return {
        "schema_version": "agent-workflow-readiness/v1",
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "reason_code": blocking_reason_codes[0] if blocking_reason_codes else None,
        "blocking_reason_codes": blocking_reason_codes,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "runtime_id": runtime_id,
        "provider_id": provider_id,
        "model_name": model_name,
        "readiness_scope": scope,
        "authentication_verified": codex_auth_ready if backend is AgentRuntimeBackend.CODEX_APP_SERVER else False,
        "checks": checks,
    }


def _ready_check(detail: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "ok",
        "ready": True,
        "reason_code": None,
        "detail": detail,
        **extra,
    }


def _failed_check(reason_code: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "error",
        "ready": False,
        "reason_code": reason_code,
        "detail": detail,
        **extra,
    }


def _postgresql_check(container: Any | None) -> dict[str, Any]:
    database = None if container is None else getattr(container, "database", None)
    if database is None:
        return _failed_check(
            "POSTGRESQL_UNAVAILABLE",
            "PostgreSQL runtime is not initialized.",
        )
    try:
        database.healthcheck()
    except Exception:
        return _failed_check(
            "POSTGRESQL_UNAVAILABLE",
            "PostgreSQL healthcheck failed.",
        )
    return _ready_check("PostgreSQL is reachable.")


def _object_store_check(container: Any | None) -> dict[str, Any]:
    object_store = (
        None if container is None else getattr(container, "object_store", None)
    )
    if object_store is None:
        return _failed_check(
            "OBJECT_STORE_UNAVAILABLE",
            "ObjectStore runtime is not initialized.",
        )
    try:
        object_store.stat("_runtime/agent-workflow-readiness")
    except Exception:
        return _failed_check(
            "OBJECT_STORE_UNAVAILABLE",
            "ObjectStore healthcheck failed.",
        )
    return _ready_check("ObjectStore is reachable.")


def _durable_worker_check(
    container: Any | None,
    *,
    source_revision: str,
    runtime_instance_id: str,
    required_job_kinds: tuple[str, ...] = AGENT_WORKFLOW_REQUIRED_JOB_KINDS,
) -> dict[str, Any]:
    jobs = None if container is None else getattr(container, "jobs", None)
    worker_fleet_status = (
        None if jobs is None else getattr(jobs, "worker_fleet_status", None)
    )
    if not callable(worker_fleet_status):
        return _failed_check(
            "DURABLE_WORKER_UNAVAILABLE",
            "Durable worker fleet status is unavailable.",
            required_job_kinds=list(required_job_kinds),
            unavailable_job_kinds=list(required_job_kinds),
            fleets={},
        )

    plan = {
        "source_revision": str(source_revision or "").strip(),
        "runtime_instance_id": str(runtime_instance_id or "").strip(),
    }
    fleets: dict[str, dict[str, Any]] = {}
    unavailable: list[str] = []
    for job_kind in required_job_kinds:
        try:
            raw_fleet = worker_fleet_status(
                desired_revision=0,
                required_kind=job_kind,
                plan=plan,
                require_connection_revision=False,
            )
            if not isinstance(raw_fleet, dict):
                raise TypeError("worker fleet projection must be an object")
            eligible_count = max(
                0,
                int(raw_fleet.get("eligible_worker_count") or 0),
            )
            live_count = max(0, int(raw_fleet.get("live_worker_count") or 0))
            state = str(raw_fleet.get("state") or "UNAVAILABLE")
        except Exception:
            eligible_count = 0
            live_count = 0
            state = "UNAVAILABLE"
        fleets[job_kind] = {
            "state": state,
            "live_worker_count": live_count,
            "eligible_worker_count": eligible_count,
        }
        if eligible_count < 1:
            unavailable.append(job_kind)

    common = {
        "required_job_kinds": list(required_job_kinds),
        "unavailable_job_kinds": unavailable,
        "fleets": fleets,
        "connection_revision_required": False,
    }
    if unavailable:
        return _failed_check(
            "DURABLE_WORKER_UNAVAILABLE",
            "No current durable worker can execute every Agent workflow job kind.",
            **common,
        )
    return _ready_check("Durable Agent workflow workers are available.", **common)


def _codex_auth_check(*, ready: bool, reason: str | None) -> dict[str, Any]:
    if ready:
        return _ready_check("Codex App Server account authentication is available.")
    normalized = str(reason or "reading_codex_unavailable").strip()
    reason_code = _CODEX_REASON_CODES.get(
        normalized,
        "CODEX_RUNTIME_UNAVAILABLE",
    )
    return _failed_check(
        reason_code,
        "Codex App Server account authentication is unavailable.",
        runtime_reason_code=normalized,
    )


def _protocol_lock_check(reason: str | None) -> dict[str, Any]:
    if reason is None:
        return _ready_check("Codex binary and protocol lock match.")
    reason_code = str(reason or "CODEX_PROTOCOL_LOCK_INVALID").strip()
    return _failed_check(
        reason_code,
        "Codex binary or protocol lock does not match.",
    )


def _source_reader_check(ready: bool) -> dict[str, Any]:
    if ready:
        return _ready_check("Immutable source document reader is bound.")
    return _failed_check(
        "SOURCE_READER_UNAVAILABLE",
        "Immutable source document reader is not initialized.",
    )


__all__ = [
    "AGENT_WORKFLOW_REQUIRED_JOB_KINDS",
    "PROVIDER_WORKFLOW_REQUIRED_JOB_KINDS",
    "build_agent_workflow_readiness",
]
