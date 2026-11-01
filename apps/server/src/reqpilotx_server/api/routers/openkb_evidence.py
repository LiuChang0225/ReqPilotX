"""Native local-only routes over the existing application project boundary."""
from io import BytesIO

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile

from reqpilotx_server.application import openkb_evidence as evidence
from reqpilotx_server.application.knowledge import upload_knowledge_document
from reqpilotx_server.application.projects import _require_project
from reqpilotx_server.application.agent_workflow.native_structure_bridge import _require_project_document
from reqpilotx_server.infrastructure.http_security import is_allowed_host, is_allowed_origin
from reqpilotx_server.infrastructure.runtime_settings import DeploymentMode
from reqpilotx_server.application.errors import ApplicationError

PROJECT = "/api/projects/{project_id}/openkb-evidence"
SOR = "/api/projects/{project_id}/sor-documents/{document_id}/agent-structure/delivery/openkb-evidence"


def require_local_scope(request: Request, project_id: str, response: Response, document_id: str | None = None):
    response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    settings = request.app.state.runtime_settings
    if settings.deployment_mode not in {DeploymentMode.LOCAL, DeploymentMode.TEST}:
        raise HTTPException(status_code=403, detail="此原件准备入口仅适用于现有本地单机模式。")
    if not is_allowed_host(request.headers.get("host", ""), settings.allowed_hosts):
        raise HTTPException(status_code=400, detail="Invalid request host.")
    origin = request.headers.get("origin", "")
    if origin and not is_allowed_origin(origin, settings.allowed_origins, allow_loopback_ports=True):
        raise HTTPException(status_code=403, detail="Cross-origin request blocked.")
    peer = request.client.host if request.client else ""
    if settings.deployment_mode is DeploymentMode.LOCAL and peer not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="本地原件操作必须来自受控单机入口。")
    _require_project(project_id)
    if document_id is not None:
        _require_project_document(project_id, document_id)
    # Trusted fixed server context, never X-User/subject/actor from a request.
    return {"identity_mode": "LOCAL_SINGLE_USER", "subject": "LOCAL_APPLICATION_OWNER", "project_id": project_id}


class BufferedUpload:
    def __init__(self, file, data):
        self.filename, self.content_type = file.filename, file.content_type
        self.stream = BytesIO(data)

    async def read(self, size=-1):
        return self.stream.read(size)


def build():
    router = APIRouter(tags=["openkb-evidence"], dependencies=[Depends(require_local_scope)])

    def invoke(operation, *args):
        try:
            return operation(*args)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"code": "OPENKB_ORIGINAL_BOUNDARY", "message": str(exc)}) from exc

    @router.get(PROJECT + "/status")
    def status(project_id: str):
        return invoke(evidence.status, project_id)

    @router.post(PROJECT + "/upload")
    async def upload(project_id: str, files: list[UploadFile] = File(...), doc_type: str = Form("REFERENCE"),
                     authority: str = Form("UNKNOWN"), claim_role: str = Form("UNKNOWN"), applicability: str = Form("UNKNOWN")):
        if not 1 <= len(files) <= 64:
            raise HTTPException(status_code=413, detail="每次登记需1至64份文件。")
        buffered, total, names = [], 0, set()
        for file in files:
            name = file.filename or ""
            if not name or len(name) > 240 or "\\" in name or "\0" in name or any(part in {"", ".", ".."} for part in name.split("/")):
                raise HTTPException(status_code=422, detail="文件需使用安全的相对名称，不能指定服务器路径。")
            if name in names:
                raise HTTPException(status_code=422, detail="同次登记包含重复文件名称。")
            names.add(name)
            data = await file.read(10 * 1024 * 1024 + 1)
            await file.close()
            total += len(data)
            if not data or len(data) > 10 * 1024 * 1024 or total > 20 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="登记超出单文件10MiB/单次20MiB范围；本次尚未登记文件。")
            buffered.append(BufferedUpload(file, data))
        documents = []
        for file in buffered:
            try:
                value = await upload_knowledge_document(project_id, file=file, doc_type=doc_type, doc_version="", status="ACTIVE",
                    effective_from="", effective_to="", authority=authority, claim_role=claim_role, applicability=applicability, source_only=True)
                documents.append({"file_name": file.filename, "status": "REGISTERED", "document_id": value["id"]})
            except ApplicationError as exc:
                documents.append({"file_name": file.filename, "status": "FAILED", "message": str(exc.detail)})
        registered = sum(item["status"] == "REGISTERED" for item in documents)
        return {"documents": documents, "registered_count": registered, "failed_count": len(documents) - registered,
            "knowledge_base_version": int(_require_project(project_id).get("knowledge_base_version") or 0),
            "index_status": "NOT_REQUESTED", "live_openkb": "NOT_RUN", "human_acceptance": "NOT_CLAIMED"}

    @router.post(SOR + "/sessions")
    def session(project_id: str, document_id: str, payload: evidence.PrepareSession):
        return invoke(evidence.prepare_session, project_id, document_id, payload)

    @router.get(SOR + "/sessions/{session_id}/originals/{knowledge_document_id}")
    def original(project_id: str, document_id: str, session_id: str, knowledge_document_id: str):
        return invoke(evidence.read_original, project_id, document_id, session_id, knowledge_document_id)

    @router.post(SOR + "/sessions/{session_id}/prepare-review")
    def review(project_id: str, document_id: str, session_id: str, payload: evidence.PrepareReview):
        return invoke(evidence.prepare_review, project_id, document_id, session_id, payload)

    return router
