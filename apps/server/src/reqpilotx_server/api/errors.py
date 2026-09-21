from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from reqpilotx_server import db
from reqpilotx_server.application.errors import ApplicationError


async def application_error_handler(
    _request: Request,
    exc: ApplicationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


async def requirement_set_mutation_blocked_handler(
    _request: Request,
    exc: db.RequirementSetMutationBlocked,
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


__all__ = [
    "application_error_handler",
    "requirement_set_mutation_blocked_handler",
]
