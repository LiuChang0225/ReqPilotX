from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from reqpilotx_server.api.application_adapter import render_application_result
from reqpilotx_server.adapters.models.reading_provider import get_reading_provider_readiness
from reqpilotx_server.application.agent_workflow.workflow_readiness import (
    build_agent_workflow_readiness,
)
from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import (
    LifecycleProbeError,
    load_protocol_lock,
    verify_protocol_binary_identity,
)
from reqpilotx_server.infrastructure.runtime_settings import (
    AgentProvider,
    AgentRuntimeBackend,
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
        return _build_codex_readiness_response()

    @router.get("/api/runtime/agent-readiness")
    def agent_readiness():
        return _build_selected_runtime_readiness_response()

    @router.get("/api/runtime/agent-workflow-readiness")
    def agent_workflow_readiness():
        return _build_agent_workflow_readiness_response()

    return router


def _build_agent_workflow_readiness_response() -> dict[str, Any]:
    from reqpilotx_server.application import runtime

    container = runtime.RUNTIME_STATE.container
    settings = None if container is None else getattr(container, "settings", None)
    selected_runtime = None
    codex_auth_ready = False
    codex_auth_reason = None
    protocol_reason = None
    if getattr(settings, "agent_runtime_backend", None) is AgentRuntimeBackend.CODEX_APP_SERVER:
        # Keep account/model and binary checks separate in the Codex contract.
        try:
            codex_readiness = get_reading_provider_readiness()
            codex_auth_ready = bool(codex_readiness.ready)
            codex_auth_reason = codex_readiness.reason_code
        except Exception:
            codex_auth_reason = "reading_codex_unavailable"
        protocol_reason = _configured_codex_protocol_reason()
        if getattr(settings, "codex_bin", None) is None:
            protocol_reason = protocol_reason or "CODEX_BINARY_NOT_CONFIGURED"
    else:
        selected_runtime = _build_selected_runtime_readiness_response()

    return build_agent_workflow_readiness(
        container=container,
        codex_auth_ready=codex_auth_ready,
        codex_auth_reason=codex_auth_reason,
        protocol_reason=protocol_reason,
        runtime_readiness=selected_runtime,
        source_reader_ready=runtime.RUNTIME_STATE.files is not None,
        source_revision=str(
            os.getenv("REQPILOT_RUNTIME_SOURCE_REVISION") or ""
        ),
        runtime_instance_id=str(
            os.getenv("REQPILOT_LOCAL_RUNTIME_INSTANCE_ID") or ""
        ),
    )


def _build_codex_readiness_response() -> dict[str, Any]:
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


def _build_selected_runtime_readiness_response() -> dict[str, Any]:
    """Project the composed runtime, without calling a provider or creating a client."""
    from reqpilotx_server.application.runtime import RUNTIME_STATE

    container = RUNTIME_STATE.container
    settings = None if container is None else getattr(container, "settings", None)
    backend = getattr(settings, "agent_runtime_backend", None)
    if backend is AgentRuntimeBackend.CODEX_APP_SERVER:
        try:
            response = _build_codex_readiness_response()
        except Exception:
            response = {
                "schema_version": "codex-runtime-readiness/v1",
                "ready": False,
                "runtime_id": backend.value,
                "model_name": str(getattr(settings, "agent_model_id", "") or "").strip() or None,
                "configuration_source": "CODEX_RUNTIME",
                "reason_code": "reading_codex_unavailable",
            }
        response["runtime_id"] = backend.value
        return response

    response: dict[str, Any] = {
        "schema_version": "agent-runtime-readiness/v1",
        "ready": False,
        "configured": False,
        "runtime_id": str(getattr(backend, "value", backend) or "none"),
        "provider_id": None,
        "model_name": None,
        "configuration_source": "NONE",
        "readiness_scope": "configuration",
        "authentication_verified": False,
        "reason_code": "AGENT_RUNTIME_NOT_CONFIGURED",
    }
    if backend is not AgentRuntimeBackend.PROVIDER_TOOL_LOOP:
        if backend is not None and backend is not AgentRuntimeBackend.NONE:
            response["reason_code"] = "AGENT_RUNTIME_UNSUPPORTED"
        return response

    provider = getattr(settings, "agent_provider", None)
    response.update(
        provider_id=provider.value if provider is AgentProvider.AZURE or provider is AgentProvider.ALIYUN else None,
        model_name=str(getattr(settings, "agent_model_id", "") or "").strip() or None,
        configuration_source="AGENT_PROVIDER_PROFILE",
        reason_code="PROVIDER_CONFIGURATION_UNAVAILABLE",
    )
    try:
        # This is the same resolver used by the execution driver. Its factory is
        # deliberately not invoked: configuration does not verify live auth.
        from reqpilotx_server.adapters.agents.provider_tool_loop import _configuration

        provider_id, model_name, _client_factory = _configuration(settings)
    except Exception:
        return response
    response.update(
        ready=True,
        configured=True,
        provider_id=provider_id,
        model_name=model_name,
        reason_code=None,
    )
    return response


def _configured_codex_protocol_reason() -> str | None:
    from reqpilotx_server.application.runtime import RUNTIME_STATE

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
