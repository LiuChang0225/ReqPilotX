from __future__ import annotations

from fastapi import APIRouter

from reqpilotx_server.api.runtime_routes import build_runtime_router
from reqpilotx_server.application import runtime

from ._factory import RouteSpec, build_router

SPECS = (
    RouteSpec(
        "/api/runtime/worker-fleet",
        runtime.get_runtime_worker_fleet_route,
        "GET",
    ),
    RouteSpec(
        "/api/runtime/ai-provider-setup",
        runtime.get_runtime_ai_provider_setup_route,
        "GET",
    ),
    RouteSpec(
        "/api/runtime/ai-provider-setup",
        runtime.update_runtime_ai_provider_setup_route,
        "POST",
    ),
    RouteSpec(
        "/api/runtime/ai-provider-setup/azure-rbac-probe",
        runtime.probe_runtime_azure_rbac_route,
        "POST",
    ),
    RouteSpec(
        "/api/runtime/ai-provider-setup/aliyun-probe",
        runtime.probe_runtime_aliyun_connection_route,
        "POST",
    ),
    RouteSpec(
        "/api/runtime/capabilities",
        runtime.get_runtime_capabilities_route,
        "GET",
    ),
    RouteSpec(
        "/api/artifact-smoke/identity",
        runtime.artifact_smoke_identity,
        "GET",
    ),
    RouteSpec(
        "/api/artifact-smoke/shutdown",
        runtime.artifact_smoke_shutdown,
        "POST",
    ),
)


def build() -> APIRouter:
    router = build_router(tag="runtime", specs=SPECS)
    router.include_router(build_runtime_router(runtime._build_health_response))
    return router


__all__ = ["build"]
