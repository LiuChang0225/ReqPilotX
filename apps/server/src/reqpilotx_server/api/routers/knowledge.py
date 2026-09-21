from reqpilotx_server.application import knowledge
from reqpilotx_server.application.knowledge_facts import get_project_knowledge_facts

from ._factory import RouteSpec, build_router

ROOT = "/api/projects/{project_id}"
SPECS = (
    RouteSpec(f"{ROOT}/knowledge-facts", get_project_knowledge_facts, "GET"),
    RouteSpec(
        f"{ROOT}/knowledge-documents",
        knowledge.upload_knowledge_document,
        "POST",
    ),
    RouteSpec(
        f"{ROOT}/knowledge-documents",
        knowledge.list_knowledge_documents,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/knowledge-documents/{{document_id}}",
        knowledge.delete_knowledge_document,
        "DELETE",
    ),
    RouteSpec(
        f"{ROOT}/knowledge-documents/{{document_id}}/file",
        knowledge.get_knowledge_document_file,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/knowledge-documents/{{document_id}}/resync",
        knowledge.resync_knowledge_document,
        "POST",
    ),
    RouteSpec(
        f"{ROOT}/knowledge-sync-jobs/{{job_id}}",
        knowledge.get_knowledge_sync_job,
        "GET",
    ),
)


def build():
    return build_router(tag="knowledge", specs=SPECS)


__all__ = ["build"]
