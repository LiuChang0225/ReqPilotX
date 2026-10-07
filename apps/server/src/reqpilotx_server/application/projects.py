from __future__ import annotations

import logging

from reqpilotx_server import db
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.adapters.persistence.entry_commands import (
    EntryCommandConflict,
    EntryCommandGone,
)
from reqpilotx_server.application.projects_contracts import (
    CreateProjectRequest,
)

logger = logging.getLogger(__name__)


def create_project(payload: CreateProjectRequest) -> dict:
    if not payload.name.strip() or not payload.customer_name.strip():
        raise ApplicationError(
            status_code=422, detail="Project name and customer name are required."
        )
    try:
        return db.create_project(
            name=payload.name,
            customer_name=payload.customer_name,
            command_id=payload.command_id,
        )
    except EntryCommandConflict as exc:
        raise ApplicationError(status_code=409, detail=str(exc)) from exc
    except EntryCommandGone as exc:
        raise ApplicationError(status_code=410, detail=str(exc)) from exc


def get_project_creation_command(command_id: str) -> dict:
    try:
        result = db.get_entry_command_result(
            kind="project.create", scope="", command_id=command_id
        )
    except EntryCommandGone as exc:
        raise ApplicationError(status_code=410, detail=str(exc)) from exc
    except ValueError as exc:
        raise ApplicationError(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise ApplicationError(status_code=404, detail="entry_command_not_found")
    return result


def list_projects() -> list[dict]:
    return db.list_projects()


def _require_project(project_id: str) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise ApplicationError(status_code=404, detail="Project not found.")
    return project
