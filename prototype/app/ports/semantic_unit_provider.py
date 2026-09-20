from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.extraction.semantic_unit_decisions import (
    SemanticUnitDecision,
    SemanticUnitDecisionRequest,
)


@runtime_checkable
class ControlledSemanticUnitProviderPort(Protocol):
    """Inactive provider boundary for one-request/one-unit contract proof."""

    provider_id: str
    provider_version: str
    model_id: str

    def classify_semantic_unit(
        self,
        request: SemanticUnitDecisionRequest,
    ) -> SemanticUnitDecision: ...


__all__ = ["ControlledSemanticUnitProviderPort"]
