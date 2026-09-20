from app.application import exports

from ._factory import RouteSpec, build_router

SPECS = (
    RouteSpec("/api/projects/{project_id}/export", exports.export_project, "POST"),
    RouteSpec(
        "/api/projects/{project_id}/export-jobs/{job_id}",
        exports.get_project_export_job,
        "GET",
    ),
    RouteSpec("/api/exports/{file_name}", exports.download_export, "GET"),
    RouteSpec(
        "/api/exports/{file_name}/reveal",
        exports.reveal_export,
        "POST",
    ),
    RouteSpec(
        "/api/projects/{project_id}/exports",
        exports.list_project_exports,
        "GET",
    ),
)


def build():
    return build_router(tag="export", specs=SPECS)


__all__ = ["build"]
