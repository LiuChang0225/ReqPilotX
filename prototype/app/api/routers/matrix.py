from app.application import requirement_matrix

from ._factory import RouteSpec, build_router


REQUIREMENT_SET = (
    "/api/projects/{project_id}/sor-documents/{document_id}/"
    "requirement-sets/{requirement_set_id}"
)
MATRIX = f"{REQUIREMENT_SET}/matrix"
SPECS = (
    RouteSpec(MATRIX, requirement_matrix.get_requirement_matrix, "GET"),
    RouteSpec(
        f"{MATRIX}/nodes/{{node_id}}/detail",
        requirement_matrix.get_requirement_matrix_node_detail,
        "GET",
    ),
    RouteSpec(
        f"{MATRIX}/tables/{{logical_table_id}}",
        requirement_matrix.get_requirement_matrix_logical_table,
        "GET",
    ),
    RouteSpec(
        f"{MATRIX}/dispositions",
        requirement_matrix.patch_requirement_matrix_dispositions,
        "PATCH",
    ),
    RouteSpec(
        f"{MATRIX}/nodes/{{node_id}}/disposition",
        requirement_matrix.patch_requirement_matrix_node_disposition,
        "PATCH",
    ),
    RouteSpec(
        f"{MATRIX}/nodes/{{node_id}}/decision",
        requirement_matrix.post_requirement_matrix_node_decision,
        "POST",
    ),
)


def build():
    return build_router(tag="matrix", specs=SPECS)


__all__ = ["build"]
