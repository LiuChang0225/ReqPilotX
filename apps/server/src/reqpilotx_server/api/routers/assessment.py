from reqpilotx_server.application import assessment

from ._factory import RouteSpec, build_router

DOCUMENT = "/api/projects/{project_id}/sor-documents/{document_id}"
SPECS = (
    RouteSpec(
        f"{DOCUMENT}/ai-analysis/scope-preview",
        assessment.preview_document_ai_analysis_scope,
        "POST",
    ),
    RouteSpec(
        f"{DOCUMENT}/run-ai-analysis/start",
        assessment.start_document_ai_analysis,
        "POST",
    ),
    RouteSpec(
        f"{DOCUMENT}/run-ai-analysis/stop",
        assessment.stop_document_ai_analysis,
        "POST",
    ),
    RouteSpec(
        f"{DOCUMENT}/ai-analysis-status",
        assessment.get_document_ai_analysis_status,
        "GET",
    ),
    RouteSpec(
        "/api/evidences/{evidence_id}/context",
        assessment.get_evidence_context,
        "GET",
    ),
    RouteSpec(
        "/api/evidences/{evidence_id}/file",
        assessment.open_evidence_file,
        "GET",
    ),
)


def build():
    return build_router(tag="assessment", specs=SPECS)


__all__ = ["build"]
