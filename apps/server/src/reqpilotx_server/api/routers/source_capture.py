from reqpilotx_server.application import source_visual_capture, native_source_view
from reqpilotx_server.application.fast_source_capture_api import capture_project_source_fast
from reqpilotx_server.domain.source_visual_capture import (
    CaptureQualityManifestV1,
    VisualPageCapturePublicV1,
)

from ._factory import RouteSpec, build_router


DOCUMENT = "/api/projects/{project_id}/sor-documents/{document_id}"
SOURCE_CAPTURE = f"{DOCUMENT}/source-capture"
VISUAL_PAGE = f"{SOURCE_CAPTURE}/visual/pages/{{page_number}}"

SPECS = (
    RouteSpec(f"{SOURCE_CAPTURE}/native-view", native_source_view.get_native_source_view, "GET"),
    RouteSpec(f"{SOURCE_CAPTURE}/native-objects/{{source_node_id}}", native_source_view.get_native_source_object, "GET"),
    RouteSpec(f"{SOURCE_CAPTURE}/fast", capture_project_source_fast, "POST"),
    RouteSpec(
        f"{SOURCE_CAPTURE}/quality",
        source_visual_capture.get_capture_quality,
        "GET",
        response_model=CaptureQualityManifestV1,
    ),
    RouteSpec(
        VISUAL_PAGE,
        source_visual_capture.get_visual_page,
        "GET",
        response_model=VisualPageCapturePublicV1,
    ),
    RouteSpec(
        VISUAL_PAGE,
        source_visual_capture.capture_visual_page,
        "POST",
        response_model=VisualPageCapturePublicV1,
    ),
)


def build():
    return build_router(tag="source-capture", specs=SPECS)


__all__ = ["build"]
