from typing import Literal

from pydantic import BaseModel, Field


class ExtractionShadowPreviewRequest(BaseModel):
    """Historical read-only evaluator request; never registered as a route."""

    model_config = {"extra": "forbid"}

    visual_provider: Literal[
        "off",
        "bailian",
        "bailian-qwen3.7-plus-layout",
        "bailian-qwen3.6-flash-layout",
        "azure-document-intelligence",
    ] = "off"
    collection: Literal[
        "full",
        "document_outline",
        "llm_requirement_batch",
        "llm_requirement_document",
    ] = "full"
    section_paths: tuple[tuple[str, ...], ...] = Field(
        default=(),
        max_length=12,
    )
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=500, ge=1, le=5000)


__all__ = ["ExtractionShadowPreviewRequest"]
