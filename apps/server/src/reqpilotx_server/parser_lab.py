"""Run the local Parser Lab without a database, worker, Agent or cloud service.

python -m reqpilotx_server.parser_lab --storage-root /tmp/reqpilotx-parser-lab \
    --frontend-dir apps/web/dist --port 8765
"""
from __future__ import annotations

import argparse
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles

from reqpilotx_server.api.pages import build_pages_router
from reqpilotx_server.api.routers.parser_lab import build
from reqpilotx_server.infrastructure.http_security import RequestBoundaryMiddleware


def create_parser_lab_app(*, root: Path, frontend: Path) -> FastAPI:
    app = FastAPI(title="ReqPilotX local Parser Lab")
    app.add_middleware(RequestBoundaryMiddleware,
        allowed_hosts=("127.0.0.1", "localhost", "[::1]"),
        allowed_origins=("http://127.0.0.1", "http://localhost", "http://[::1]"),
        allow_loopback_origin_ports=True)
    @app.get("/api/auth/session")
    def local_session(response: Response):
        # Shared web shell asks every host for its authentication mode. This
        # standalone loopback tool has no accounts or business API routes.
        response.headers["Cache-Control"] = "no-store"
        return {"enabled": False, "user": None, "csrf_token": None}

    app.include_router(build(root))
    app.include_router(build_pages_router(frontend))
    if (frontend / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--frontend-dir", type=Path, default=Path("apps/web/dist"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_parser_lab_app(root=args.storage_root, frontend=args.frontend_dir.resolve()),
        host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
