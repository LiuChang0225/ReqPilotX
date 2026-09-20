from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.api.application_adapter import render_application_result
from app.adapters.models.reading_provider import get_reading_provider_readiness
from app.application.agent_workflow.workflow_readiness import (
    build_agent_workflow_readiness,
)
from app.adapters.agents.codex_app_server.lifecycle import (
    LifecycleProbeError,
    load_protocol_lock,
    verify_protocol_binary_identity,
)

HealthResponseBuilder = Callable[..., Any]


def build_runtime_router(
    health_response: HealthResponseBuilder,
) -> APIRouter:
    router = APIRouter(tags=["runtime"])

    @router.get("/api/health")
    @router.get("/api/readiness")
    def health():
        return render_application_result(health_response())

    @router.get("/api/runtime-readiness")
    def runtime_health():
        return render_application_result(health_response(runtime_only=True))

    @router.get("/api/runtime/codex-readiness")
    def codex_readiness():
        readiness = get_reading_provider_readiness()
        protocol_reason = _configured_codex_protocol_reason()
        return {
            "schema_version": "codex-runtime-readiness/v1",
            "ready": readiness.ready and protocol_reason is None,
            "runtime_id": readiness.provider_id,
            "model_name": readiness.model_name,
            "configuration_source": readiness.configuration_source,
            "reason_code": readiness.reason_code or protocol_reason,
        }

    @router.get("/api/runtime/agent-workflow-readiness")
    def agent_workflow_readiness():
        return _build_agent_workflow_readiness_response()

    return router


def _build_agent_workflow_readiness_response() -> dict[str, Any]:
    from app.application import runtime

    container = runtime.RUNTIME_STATE.container
    try:
        codex_readiness = get_reading_provider_readiness()
        codex_auth_ready = bool(codex_readiness.ready)
        codex_auth_reason = codex_readiness.reason_code
    except Exception:
        codex_auth_ready = False
        codex_auth_reason = "reading_codex_unavailable"

    protocol_reason = _configured_codex_protocol_reason()
    settings = None if container is None else getattr(container, "settings", None)
    if settings is None or getattr(settings, "codex_bin", None) is None:
        protocol_reason = protocol_reason or "CODEX_BINARY_NOT_CONFIGURED"

    return build_agent_workflow_readiness(
        container=container,
        codex_auth_ready=codex_auth_ready,
        codex_auth_reason=codex_auth_reason,
        protocol_reason=protocol_reason,
        source_reader_ready=runtime.RUNTIME_STATE.files is not None,
        source_revision=str(
            os.getenv("REQPILOT_RUNTIME_SOURCE_REVISION") or ""
        ),
        runtime_instance_id=str(
            os.getenv("REQPILOT_LOCAL_RUNTIME_INSTANCE_ID") or ""
        ),
    )


def _configured_codex_protocol_reason() -> str | None:
    from app.application.runtime import RUNTIME_STATE

    container = RUNTIME_STATE.container
    codex_bin = None if container is None else container.settings.codex_bin
    if codex_bin is None:
        return None
    try:
        stat = codex_bin.stat()
    except OSError:
        return "CODEX_BINARY_INVALID"
    return _cached_codex_protocol_reason(
        str(codex_bin),
        stat.st_size,
        stat.st_mtime_ns,
    )


@lru_cache(maxsize=4)
def _cached_codex_protocol_reason(
    raw_codex_bin: str,
    binary_size: int,
    binary_mtime_ns: int,
) -> str | None:
    del binary_size, binary_mtime_ns
    try:
        verify_protocol_binary_identity(
            Path(raw_codex_bin),
            lock=load_protocol_lock(),
        )
    except LifecycleProbeError as exc:
        return str(exc)
    return None


__all__ = ["build_runtime_router"]
