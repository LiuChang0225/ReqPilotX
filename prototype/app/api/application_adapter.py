from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, get_type_hints

from fastapi import BackgroundTasks, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.application.transport import (
    UPLOAD_REQUIRED,
    BackgroundTaskScheduler,
    BinaryArtifact,
    FileArtifact,
    FormValue,
    JsonArtifact,
    RequestContext,
    ResponseSink,
    UploadInput,
)

_INJECTION_TYPES = {
    BackgroundTaskScheduler: BackgroundTasks,
    RequestContext: Request,
    ResponseSink: Response,
    UploadInput: UploadFile,
}


def adapt_application_endpoint(endpoint):
    """Expose a framework-neutral application callable as a FastAPI endpoint."""

    signature = inspect.signature(endpoint)
    try:
        type_hints = get_type_hints(endpoint)
    except (NameError, TypeError):
        type_hints = {}
    parameters: list[inspect.Parameter] = []
    for parameter in signature.parameters.values():
        annotation = type_hints.get(parameter.name, parameter.annotation)
        annotation = _INJECTION_TYPES.get(annotation, annotation)
        default = parameter.default
        if default is UPLOAD_REQUIRED:
            default = File(...)
        elif isinstance(default, FormValue):
            default = Form(default.default)
        parameters.append(
            parameter.replace(annotation=annotation, default=default)
        )

    adapted_signature = signature.replace(
        parameters=parameters,
        return_annotation=Any,
    )

    if inspect.iscoroutinefunction(endpoint):

        @wraps(endpoint)
        async def adapted(*args, **kwargs):
            return render_application_result(await endpoint(*args, **kwargs))

    else:

        @wraps(endpoint)
        def adapted(*args, **kwargs):
            return render_application_result(endpoint(*args, **kwargs))

    adapted.__signature__ = adapted_signature
    return adapted


def render_application_result(result: Any) -> Any:
    if isinstance(result, FileArtifact):
        return FileResponse(
            result.path,
            filename=result.filename,
            media_type=result.media_type,
            headers=dict(result.headers or {}),
        )
    if isinstance(result, BinaryArtifact):
        return Response(
            content=result.content,
            status_code=result.status_code,
            media_type=result.media_type,
            headers=dict(result.headers or {}),
        )
    if isinstance(result, JsonArtifact):
        return JSONResponse(
            content=result.content,
            status_code=result.status_code,
            headers=dict(result.headers or {}),
        )
    return result


__all__ = ["adapt_application_endpoint", "render_application_result"]
