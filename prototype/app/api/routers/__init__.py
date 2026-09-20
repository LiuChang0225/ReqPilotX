from __future__ import annotations

from fastapi import APIRouter

from . import (
    agent_workflow,
    assessment,
    export,
    extraction,
    ingestion,
    knowledge,
    matrix,
    projects,
    reading,
    review,
    runtime,
    source_capture,
    source_item_delivery,
    source_item_images,
)


def build_domain_routers() -> tuple[APIRouter, ...]:
    return (
        runtime.build(),
        projects.build(),
        ingestion.build(),
        reading.build(),
        source_capture.build(),
        source_item_delivery.build(),
        source_item_images.build(),
        agent_workflow.build(),
        extraction.build(),
        review.build(),
        matrix.build(),
        assessment.build(),
        knowledge.build(),
        export.build(),
    )


__all__ = ["build_domain_routers"]
