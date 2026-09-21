"""Read-only original Office view, separate from Agent-owned Map semantics."""
from __future__ import annotations

from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.transport import BinaryArtifact
from reqpilotx_server.application.agent_workflow.office_source_evidence import (
    build_native_office_source_view, read_native_office_source_image,
)


def _source(project_id, document_id, source_sha256):
    from reqpilotx_server.application.source_capture import require_current_source_document
    from reqpilotx_server.application.source_runtime import resolve_source_record_path
    document = require_current_source_document(project_id=project_id, sor_document_id=document_id,
        expected_source_sha256=source_sha256)
    return resolve_source_record_path(document)


def get_native_source_view(project_id: str, document_id: str, source_sha256: str) -> dict:
    source = _source(project_id, document_id, source_sha256)
    try:
        return build_native_office_source_view(source, source_sha256,
            image_url_prefix=f'/api/projects/{project_id}/sor-documents/{document_id}/source-capture/native-objects')
    except ValueError as exc:
        raise ApplicationError(status_code=422, detail={'code': 'NATIVE_SOURCE_VIEW_UNAVAILABLE', 'message': str(exc)}) from exc


def get_native_source_object(project_id: str, document_id: str, source_node_id: str,
                            source_sha256: str, content_sha256: str) -> BinaryArtifact:
    source = _source(project_id, document_id, source_sha256)
    try:
        image = read_native_office_source_image(source, source_sha256, source_node_id, content_sha256)
    except ValueError as exc:
        raise ApplicationError(status_code=422, detail={'code': 'NATIVE_SOURCE_OBJECT_UNAVAILABLE', 'message': str(exc)}) from exc
    return BinaryArtifact(content=image['bytes'], media_type=image['media_type'], headers={
        'Content-Disposition': 'inline', 'X-Content-Type-Options': 'nosniff',
        'Cache-Control': 'private, max-age=31536000, immutable', 'ETag': f'"{content_sha256}"',
        'X-Source-Sha256': source_sha256, 'X-Source-Object': source_node_id,
    })
