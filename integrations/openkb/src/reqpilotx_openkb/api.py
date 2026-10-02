"""Opt-in routes; the caller must supply existing host authentication/access checks.

No anonymous app or automatic mounting on the main server is provided. All paths
refer to the host-bound project, KB and baseline, never client filesystem paths.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .errors import AdapterError
from .knowledge_store import MAX_FILES, MAX_FILE_BYTES


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: str = Field(pattern=r"^generation-[0-9a-f]{64}$")
    expected_baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    unit_ids: list[str] | None = Field(default=None, max_length=2048)


def create_router(service, *, authorize: Callable):
    """authorize is an existing host dependency that rejects unauthorized access.

    Supplying authentication only without project authorization is insufficient.
    The concrete deployment must bind this dependency to the service's project.
    """
    if not callable(authorize):
        raise TypeError("Existing host project authorization dependency required")
    router = APIRouter(prefix="/api/v1/openkb-evidence", dependencies=[Depends(authorize)])

    def invoke(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (AdapterError, ValueError) as exc:
            raise HTTPException(status_code=409, detail={"code": "OPENKB_EVIDENCE_BOUNDARY", "message": str(exc)}) from exc
        except (OSError, KeyError) as exc:
            raise HTTPException(status_code=404, detail={"code": "OPENKB_EVIDENCE_ARTIFACT_UNAVAILABLE"}) from exc

    @router.post("/sources/upload")
    async def upload(files: list[UploadFile] = File(...), metadata: str = Form("{}"), declaration: str = Form(...)):
        if not files or len(files) > MAX_FILES or len(metadata) > 65536 or not declaration.strip() or len(declaration) > 4000:
            raise HTTPException(status_code=413, detail="Explicit upload limits exceeded")
        try:
            metadata_value = json.loads(metadata)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="metadata must be JSON") from exc
        if not isinstance(metadata_value, dict):
            raise HTTPException(status_code=422, detail="metadata must be an object")
        uploaded = {}
        total = 0
        for file in files:
            try:
                data = await file.read(MAX_FILE_BYTES + 1)
            finally:
                await file.close()
            total += len(data)
            if not file.filename or file.filename in uploaded:
                raise HTTPException(status_code=422, detail="A unique logical filename is required")
            if len(data) > MAX_FILE_BYTES or total > 20 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Upload exceeds explicit byte budget; no documents registered")
            uploaded[file.filename] = data
        generation = invoke(service.upload, uploaded, metadata=metadata_value, declaration=declaration)
        return {"generation": generation, "project_id": service.store.project_id, "kb": service.store.kb,
            "original_registration": "NATIVE_REQPILOTX_RECAPTURE", "human_acceptance": "NOT_CLAIMED"}

    @router.post("/sessions")
    def begin(request: SessionRequest):
        return invoke(service.begin, **request.model_dump())

    @router.get("/sessions/{session_id}/sources")
    def sources(session_id: str):
        return invoke(service.source_index, session_id)

    @router.get("/sessions/{session_id}/originals/{document_id}")
    def original(session_id: str, document_id: str):
        return invoke(service.read_original, session_id, document_id)

    @router.post("/sessions/{session_id}/assessments")
    def compile_assessments(session_id: str, envelope: dict[str, Any]):
        if len(json.dumps(envelope, ensure_ascii=False).encode()) > 1024 * 1024:
            raise HTTPException(status_code=413, detail="Assessment exceeds explicit input budget")
        return invoke(service.compile, session_id, envelope)

    @router.get("/sessions/{session_id}/assessments/{assessment_sha256}/export-receipt")
    def export_receipt(session_id: str, assessment_sha256: str):
        _, receipt = invoke(service.export, session_id, assessment_sha256)
        return receipt

    @router.get("/sessions/{session_id}/assessments/{assessment_sha256}/matrix.xlsx")
    def export_xlsx(session_id: str, assessment_sha256: str):
        directory, _ = invoke(service.export, session_id, assessment_sha256)
        return FileResponse(directory / "matrix.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="reqpilotx-matrix-" + assessment_sha256[:12] + ".xlsx")

    return router
