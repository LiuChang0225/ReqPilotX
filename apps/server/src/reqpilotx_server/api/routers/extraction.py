from reqpilotx_server.application.extraction import (
    agent_table_materializations,
    boundary_calibration,
    document_jobs,
    entry_classification,
    geometry_evidence,
    parsing_diagnostics,
    restoration,
    source_page_facts,
    structure_optimization_jobs,
    structure_workspace,
    table_projection_revisions,
    working_tree_publication,
)
from reqpilotx_server.application.transport import RequestContext, ResponseSink

from ._factory import RouteSpec, build_router

ROOT = "/api/projects/{project_id}/sor-documents/{document_id}/extraction"


def get_document_structure(
    project_id: str,
    document_id: str,
    request: RequestContext,
    response: ResponseSink,
):
    """HTTP adapter that enables conditional immutable Structure reads."""

    return structure_workspace.get_document_structure_agent(
        project_id,
        document_id,
        response,
        request=request,
    )


SPECS = (
    RouteSpec(
        f"{ROOT}/start",
        document_jobs.start_document_extraction,
        "POST",
        status_code=202,
    ),
    RouteSpec(
        f"{ROOT}/llm-document/start",
        document_jobs.start_llm_document_extraction,
        "POST",
        status_code=202,
    ),
    RouteSpec(
        f"{ROOT}/llm-document/status",
        document_jobs.get_llm_document_extraction_status,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/cancel",
        document_jobs.cancel_document_extraction,
        "POST",
    ),
    RouteSpec(
        f"{ROOT}/visual-repair",
        document_jobs.repair_document_extraction_visual_pages,
        "POST",
        status_code=202,
    ),
    RouteSpec(
        f"{ROOT}/structure",
        get_document_structure,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/structure/patch",
        structure_workspace.apply_document_structure_patch,
        "POST",
    ),
    RouteSpec(
        f"{ROOT}/structure/patch/preview",
        boundary_calibration.preview_document_boundary_calibration,
        "POST",
    ),
    RouteSpec(
        f"{ROOT}/structure/undo",
        structure_workspace.undo_document_structure_revision,
        "POST",
    ),
    RouteSpec(
        f"{ROOT}/structure/optimization",
        structure_optimization_jobs.start_document_structure_optimization,
        "POST",
        status_code=202,
    ),
    RouteSpec(
        f"{ROOT}/structure/optimization/status",
        structure_optimization_jobs.get_document_structure_optimization_status,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/runs",
        restoration.start_document_restoration_run,
        "POST",
    ),
    RouteSpec(
        f"{ROOT}/visual-capabilities",
        restoration.get_document_restoration_capabilities,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/runs/latest",
        restoration.get_latest_document_restoration_run,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/runs/{{run_id}}",
        restoration.get_document_restoration_run,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/runs/{{run_id}}/pages/{{page_number}}",
        restoration.get_document_restoration_page,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/runs/{{run_id}}/details",
        restoration.get_document_restoration_details,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/source-pages/{{page_number}}",
        source_page_facts.get_document_source_page,
        "GET",
    ),
    RouteSpec(
        (
            f"{ROOT}/attempts/{{job_id}}/generations/{{generation}}/"
            "source-pages/{page_number}"
        ),
        source_page_facts.get_document_extraction_staged_source_page,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/parse-risks",
        parsing_diagnostics.get_document_parsing_diagnostics,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/parse-risks/acknowledgement",
        parsing_diagnostics.acknowledge_document_parsing_risks,
        "PUT",
    ),
    RouteSpec(
        f"{ROOT}/entry-classifications",
        entry_classification.get_document_entry_classifications,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/entry-classifications",
        entry_classification.patch_document_entry_classifications,
        "PATCH",
    ),
    RouteSpec(
        f"{ROOT}/entry-classifications/acceptance",
        entry_classification.accept_document_entry_classifications,
        "PUT",
    ),
    RouteSpec(
        f"{ROOT}/geometry-focus/{{node_id}}",
        geometry_evidence.get_document_geometry_focus,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/boundary-calibration/pages/{{page_number}}",
        boundary_calibration.get_document_boundary_calibration_page,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/requirement-tree/publish-draft",
        working_tree_publication.publish_working_requirement_tree_draft,
        "POST",
    ),
    RouteSpec(
        f"{ROOT}/table-record-projections/{{projection_id}}/row-groups",
        table_projection_revisions.get_document_table_record_projection_row_groups,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/table-record-projections/{{projection_id}}/revise",
        table_projection_revisions.revise_document_table_record_projection,
        "POST",
    ),
    RouteSpec(
        (
            f"{ROOT}/table-record-projections/{{projection_id}}/"
            "agent-materializations"
        ),
        agent_table_materializations.commit_document_agent_table_materialization,
        "POST",
    ),
    RouteSpec(
        f"{ROOT}/agent-table-materializations/{{record_id}}",
        agent_table_materializations.get_document_agent_table_materialization,
        "GET",
    ),
    RouteSpec(
        f"{ROOT}/source-crop",
        restoration.get_document_source_crop,
        "GET",
    ),
)


def build():
    return build_router(tag="extraction", specs=SPECS)


__all__ = ["build"]
