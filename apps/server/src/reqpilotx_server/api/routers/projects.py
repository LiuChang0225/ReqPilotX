from reqpilotx_server.application import projects
from reqpilotx_server.application import processing_profiles

from ._factory import RouteSpec, build_router

SPECS = (
    RouteSpec("/api/projects", projects.create_project, "POST"),
    RouteSpec("/api/projects", projects.list_projects, "GET"),
    RouteSpec(
        "/api/projects/{project_id}/processing-profile",
        processing_profiles.get_project_processing_profile_route,
        "GET",
    ),
    RouteSpec(
        "/api/projects/{project_id}/processing-profile",
        processing_profiles.update_project_processing_profile_route,
        "PUT",
    ),
)


def build():
    return build_router(tag="projects", specs=SPECS)


__all__ = ["build"]
