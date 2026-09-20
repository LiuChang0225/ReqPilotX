from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter

from app.api.application_adapter import adapt_application_endpoint


@dataclass(frozen=True, slots=True)
class RouteSpec:
    path: str
    endpoint: Callable[..., Any]
    method: str
    status_code: int | None = None
    response_model: Any | None = None


def build_router(
    *,
    tag: str,
    specs: tuple[RouteSpec, ...],
) -> APIRouter:
    router = APIRouter(tags=[tag])
    for spec in specs:
        endpoint = adapt_application_endpoint(spec.endpoint)
        options: dict[str, object] = {
            "methods": [spec.method],
            "name": spec.endpoint.__name__,
        }
        if spec.status_code is not None:
            options["status_code"] = spec.status_code
        if spec.response_model is not None:
            options["response_model"] = spec.response_model
        router.add_api_route(
            spec.path,
            endpoint,
            **options,
        )
    return router


__all__ = ["RouteSpec", "build_router"]
