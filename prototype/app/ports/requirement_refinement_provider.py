from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.extraction.semantic_provider import SemanticProviderRunRecord


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class RequirementRefinementProviderRequest(_StrictFrozenModel):
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    atomization_run_id: str = Field(pattern=r"^atomization-run-[0-9a-f]{32}$")
    atomization_revision: int = Field(ge=0)
    atomic_node_id: str = Field(pattern=r"^atomic-obligation-[0-9a-f]{32}$")
    requirement_subtype: str = Field(min_length=1, max_length=120)
    original_statement: str = Field(min_length=1, max_length=500_000)
    source_context_statement: str = Field(default="", max_length=500_000)
    parent_context_statement: str = Field(default="", max_length=500_000)


class RequirementRefinementProviderResult(_StrictFrozenModel):
    suggested_statement: str = Field(min_length=1, max_length=500_000)
    rationale: str = Field(default="", max_length=20_000)
    reason_codes: tuple[str, ...]
    run: SemanticProviderRunRecord

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("refinement provider results require reason codes")
        if value != tuple(sorted(set(value))):
            raise ValueError(
                "refinement provider reason codes must be unique and sorted"
            )
        return value


class RequirementRefinementProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@runtime_checkable
class RequirementRefinementProviderPort(Protocol):
    def suggest(
        self,
        request: RequirementRefinementProviderRequest,
    ) -> RequirementRefinementProviderResult: ...


__all__ = [
    "RequirementRefinementProviderError",
    "RequirementRefinementProviderPort",
    "RequirementRefinementProviderRequest",
    "RequirementRefinementProviderResult",
]
