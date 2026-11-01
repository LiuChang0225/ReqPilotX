"""Fail-closed, audited pilot surface over the existing shared business core.

Server-side project ownership is immutable. HTTP callers cannot select an actor,
and durable jobs restore the actor recorded at enqueue, never a browser header.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

from fastapi import HTTPException, Request

from reqpilotx_server import db
from reqpilotx_server.saas.auth import require_csrf, require_session
from reqpilotx_server.saas.config import SaaSSettings
from reqpilotx_server.saas.context import account_scope, current_scope
from reqpilotx_server.saas.store import owns_project
from reqpilotx_server.saas.routes import SUPPORTED_ROUTES


def _missing():
    raise HTTPException(404, "saas_resource_not_found")


def _allowed(request: Request) -> bool:
    route = request.scope.get("route")
    path = str(getattr(route, "path", ""))
    return (path, request.method) in SUPPORTED_ROUTES


def check_document(project_id: str, document_id: str) -> None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT project_id FROM sor_documents WHERE id=?", (document_id,)).fetchone()
    if row is None or row["project_id"] != project_id:
        _missing()


def _validate_reference(key: str, value, project_id: str, document_id: str) -> None:
    if key in {"saas_scope", "account_id", "tenant_id", "owner_id"}:
        raise HTTPException(422, "saas_actor_is_server_owned")
    if value in (None, "", [], {}):
        return
    if key in {"project_id", "source_project_id", "target_project_id"}:
        if value != project_id:
            _missing()
    if key in {"document_id", "sor_document_id", "source_document_id"}:
        if not isinstance(value, str):
            _missing()
        check_document(project_id, value)
        if document_id and value != document_id:
            _missing()
    if key in {"requirement_set_id", "expected_active_requirement_set_id", "source_requirement_set_id", "target_requirement_set_id"}:
        with db.get_conn() as conn:
            row = conn.execute("SELECT project_id,sor_document_id FROM requirement_sets WHERE id=?", (str(value),)).fetchone()
        if row is None or row["project_id"] != project_id or (document_id and row["sor_document_id"] != document_id):
            _missing()
    if key in {"job_id", "expected_job_id", "source_job_id"}:
        with db.get_conn() as conn:
            row = conn.execute("SELECT payload,aggregate_type,aggregate_id FROM runtime_jobs WHERE id=?", (str(value),)).fetchone()
        if row is None:
            _missing()
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        scope = current_scope()
        bound = payload.get("saas_scope", {})
        if (payload.get("project_id") != project_id
                or (document_id and (payload.get("document_id") or (
                    row["aggregate_id"] if row["aggregate_type"] == "sor-document" else None)) != document_id)
                or not scope or bound.get("account_id") != scope.account_id
                or bound.get("project_id") != project_id):
            _missing()


def _validate_body(value, project_id: str, document_id: str, *, depth: int = 0):
    if depth > 32:
        raise HTTPException(422, "saas_payload_too_deep")
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_reference(key, child, project_id, document_id)
            _validate_body(child, project_id, document_id, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_body(child, project_id, document_id, depth=depth + 1)


async def authorize(request: Request):
    settings = request.app.state.saas_settings
    if not settings.enabled:
        yield
        return
    path = request.url.path
    if not path.startswith("/api/") or (path, request.method) in {
        ("/api/healthz", "GET"),
        ("/api/auth/session", "GET"), ("/api/auth/register", "POST"),
        ("/api/auth/login", "POST"), ("/api/auth/logout", "POST"),
    }:
        yield
        return
    session = require_session(request)
    if not _allowed(request):
        raise HTTPException(403, "saas_route_not_enabled")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_csrf(request, session)
    project_id = str(request.path_params.get("project_id", ""))
    document_id = str(request.path_params.get("document_id", ""))
    if project_id and not owns_project(session["id"], project_id):
        _missing()
    with account_scope(session["id"], project_id):
        if document_id:
            check_document(project_id, document_id)
        for key, value in request.path_params.items():
            if key not in {"project_id", "document_id"}:
                _validate_reference(key, value, project_id, document_id)
        for key, value in request.query_params.multi_items():
            _validate_reference(key, value, project_id, document_id)
        if "application/json" in request.headers.get("content-type", ""):
            # FastAPI caches this body for the endpoint. No upload bytes are
            # buffered by the authorization layer.
            try:
                value = await request.json()
            except (ValueError, UnicodeDecodeError):
                raise HTTPException(422, "saas_invalid_json") from None
            _validate_body(value, project_id, document_id)
        yield


def bind_job_payload(payload: dict, *, aggregate_type: str, aggregate_id: str) -> dict:
    if not SaaSSettings.from_environment().enabled:
        return payload
    scope = current_scope()
    if scope is None or not scope.project_id or not owns_project(scope.account_id, scope.project_id):
        raise PermissionError("saas_job_requires_trusted_owner")
    if payload.get("project_id") != scope.project_id:
        raise PermissionError("saas_job_project_mismatch")
    if aggregate_type == "sor-document":
        check_document(scope.project_id, aggregate_id)
        if payload.get("document_id") not in {None, aggregate_id}:
            raise PermissionError("saas_job_document_mismatch")
    supplied = payload.get("saas_scope")
    bound = {"account_id": scope.account_id, "project_id": scope.project_id}
    if supplied is not None and supplied != bound:
        raise PermissionError("saas_job_actor_mismatch")
    return {**payload, "saas_scope": bound}


@contextmanager
def durable_job_scope(job):
    if not SaaSSettings.from_environment().enabled:
        yield
        return
    from reqpilotx_server.ports.job_queue import NonRetryableJobError
    payload = job.payload
    bound = payload.get("saas_scope") or {}
    if not isinstance(bound, dict):
        bound = {}
    account_id, project_id = bound.get("account_id"), bound.get("project_id")
    valid = (isinstance(account_id, str) and isinstance(project_id, str)
             and payload.get("project_id") == project_id
             and owns_project(account_id, project_id))
    if valid and job.aggregate_type == "sor-document":
        try:
            check_document(project_id, job.aggregate_id)
            valid = payload.get("document_id") in {None, job.aggregate_id}
        except HTTPException:
            valid = False
    if not valid:
        raise NonRetryableJobError(error_code="SAAS_OWNER_BINDING_REJECTED",
                                  message="Durable job has no valid immutable account/project binding")
    with account_scope(account_id, project_id):
        yield
