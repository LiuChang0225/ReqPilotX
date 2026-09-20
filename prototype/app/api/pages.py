from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
_ENTRY_ROUTES = (
    "/",
    "/home",
    "/workspace",
    "/extraction-review",
    "/pdf-viewer",
)


def frontend_page(frontend_dir: Path = FRONTEND_DIST) -> FileResponse:
    """Serve the single React entrypoint for every formal product route."""

    entrypoint = Path(frontend_dir) / "index.html"
    if not entrypoint.is_file():
        raise HTTPException(
            status_code=503,
            detail=(
                "ReqPilotX frontend build is missing. "
                "Run `npm --prefix frontend run build`."
            ),
        )
    return FileResponse(
        entrypoint,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def build_pages_router(frontend_dir: Path) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    def serve_frontend() -> FileResponse:
        return frontend_page(frontend_dir)

    for route in _ENTRY_ROUTES:
        router.add_api_route(route, serve_frontend, methods=["GET"])
    return router


def home_page() -> FileResponse:
    return frontend_page()


def workspace_page() -> FileResponse:
    return frontend_page()


def home_alias_page() -> FileResponse:
    return frontend_page()


def extraction_review_page() -> FileResponse:
    return frontend_page()


def pdf_viewer_page() -> FileResponse:
    return frontend_page()


router = build_pages_router(FRONTEND_DIST)


__all__ = [
    "FRONTEND_DIST",
    "build_pages_router",
    "extraction_review_page",
    "frontend_page",
    "home_alias_page",
    "home_page",
    "pdf_viewer_page",
    "router",
    "workspace_page",
]
