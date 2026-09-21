from __future__ import annotations

import logging

from reqpilotx_server import db
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.projects_contracts import (
    CreateProjectRequest,
)

logger = logging.getLogger(__name__)

def create_project(payload: CreateProjectRequest) -> dict:
    return db.create_project(
        name=payload.name.strip(), customer_name=payload.customer_name.strip()
    )

def list_projects() -> list[dict]:
    return db.list_projects()

def _require_project(project_id: str) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise ApplicationError(status_code=404, detail="Project not found.")
    return project
