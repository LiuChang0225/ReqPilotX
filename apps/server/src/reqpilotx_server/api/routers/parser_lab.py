"""Local-only transport; no project baseline mutations or private-path input."""
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse

from reqpilotx_server.application.parser_lab import MAX_UPLOAD_BYTES, ParserLabStore, parser_catalog


def build(root: Path) -> APIRouter:
    store = ParserLabStore(root)
    router = APIRouter(prefix="/api/parser-lab", tags=["parser-lab"])

    @router.get("/parsers")
    def parsers(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return parser_catalog()

    @router.get("/runs")
    def runs(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return store.list_runs()

    @router.post("/runs")
    def run(response: Response, file: UploadFile = File(...), parser: str = Form("pymupdf"),
            language: str = Form("eng")):
        response.headers["Cache-Control"] = "no-store"
        content = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "PDF 超过 25 MiB")
        try:
            return store.run(content, filename=file.filename or "source.pdf", parser=parser, language=language)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/runs/{run_id}")
    def get(run_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            return store.get(run_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/runs/{run_id}/artifacts/{artifact_id}")
    def artifact(run_id: str, artifact_id: str):
        try:
            path, media_type = store.artifact(run_id, artifact_id)
            return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff"})
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
