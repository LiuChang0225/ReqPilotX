from __future__ import annotations

from fastapi import APIRouter



def build_domain_routers() -> tuple[APIRouter, ...]:
    from reqpilotx_server.saas.auth import build_auth_router
    from . import (
        agent_workflow, assessment, export, extraction, ingestion, knowledge,
        openkb_evidence, matrix, projects, reading, review, runtime, source_capture,
        source_item_delivery, source_item_images,
    )
    return (
        runtime.build(),
        build_auth_router(),
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
        openkb_evidence.build(),
        export.build(),
    )


__all__ = ["build_domain_routers"]
