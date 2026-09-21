from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from reqpilotx_server import db
from reqpilotx_server.api.errors import (
    application_error_handler,
    requirement_set_mutation_blocked_handler,
)
from reqpilotx_server.api.pages import build_pages_router
from reqpilotx_server.application import runtime
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.infrastructure.http_security import (
    LOCAL_CORS_ORIGIN_REGEX,
    RequestBoundaryMiddleware,
)
from reqpilotx_server.infrastructure.runtime_settings import DeploymentMode, RuntimeSettings


def create_http_app(
    *,
    settings: RuntimeSettings,
    static_dir: Path,
) -> FastAPI:
    app = FastAPI(title="ReqPilotX", version="2.0.0")
    local_mode = settings.deployment_mode in {
        DeploymentMode.LOCAL,
        DeploymentMode.TEST,
    }
    app.add_middleware(
        CORSMiddleware,
        allow_origins=(
            [] if local_mode else list(settings.allowed_origins)
        ),
        allow_origin_regex=LOCAL_CORS_ORIGIN_REGEX if local_mode else None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        RequestBoundaryMiddleware,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
        allow_loopback_origin_ports=local_mode,
    )
    frontend_dir = Path(static_dir)
    assets_dir = frontend_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    elif frontend_dir.is_dir():
        assets_dir = frontend_dir
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    app.include_router(build_pages_router(frontend_dir))
    app.state.runtime_settings = settings
    return app


def create_app(*, settings: RuntimeSettings | None = None) -> FastAPI:
    """Compose the production API from domain routers and lifecycle hooks."""

    from reqpilotx_server.api.routers import build_domain_routers

    runtime_settings = settings or runtime.RUNTIME_SETTINGS
    app = create_http_app(
        settings=runtime_settings,
        static_dir=runtime.STATIC_DIR,
    )
    app.add_exception_handler(
        ApplicationError,
        application_error_handler,
    )
    app.add_exception_handler(
        db.RequirementSetMutationBlocked,
        requirement_set_mutation_blocked_handler,
    )
    app.add_event_handler("startup", runtime.startup)
    app.add_event_handler("shutdown", runtime.shutdown)
    domain_routers = build_domain_routers()
    for router in domain_routers:
        app.include_router(router)
    app.state.domain_routers = tuple(
        str((router.tags or ["untagged"])[0])
        for router in domain_routers
    )
    return app


__all__ = ["create_app", "create_http_app"]
