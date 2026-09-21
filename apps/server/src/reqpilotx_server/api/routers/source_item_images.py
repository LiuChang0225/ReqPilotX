from reqpilotx_server.application.agent_workflow import source_item_image_jobs as images
from ._factory import RouteSpec, build_router

ROOT = "/api/projects/{project_id}/sor-documents/{document_id}/agent-structure/requirement-baseline/image-runs"
SPECS = (
    RouteSpec(f"{ROOT}/start", images.start_source_item_images, "POST", status_code=202),
    RouteSpec(f"{ROOT}/{{job_id}}", images.get_source_item_image_job, "GET"),
)


def build():
    return build_router(tag="source-item-images", specs=SPECS)
