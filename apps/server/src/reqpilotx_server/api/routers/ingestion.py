from reqpilotx_server.application import ingestion

from ._factory import RouteSpec, build_router

ROOT = "/api/projects/{project_id}/sor-documents"
DOCUMENT = f"{ROOT}/{{document_id}}"
SPECS = (
    RouteSpec(ROOT, ingestion.upload_sor_document, "POST"),
    RouteSpec(ROOT, ingestion.list_sor_documents, "GET"),
    RouteSpec(DOCUMENT, ingestion.delete_sor_document, "DELETE"),
    RouteSpec(
        f"{DOCUMENT}/table-assets/{{table_id}}",
        ingestion.get_document_table_asset,
        "GET",
    ),
    RouteSpec(
        f"{DOCUMENT}/table-assets/{{table_id}}/snapshot",
        ingestion.get_document_table_asset_snapshot,
        "GET",
    ),
    RouteSpec(
        f"{DOCUMENT}/table-assets/{{table_id}}/source-snapshot",
        ingestion.get_document_table_asset_source_snapshot,
        "GET",
    ),
    RouteSpec(f"{DOCUMENT}/file", ingestion.get_sor_document_file, "GET"),
)


def build():
    return build_router(tag="ingestion", specs=SPECS)


__all__ = ["build"]
