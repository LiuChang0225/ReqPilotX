from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.adapters.documents.bailian_semantic_provider import (
    BailianQwenSemanticProvider,
)
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.ports.requirement_refinement_provider import (
    RequirementRefinementProviderError,
    RequirementRefinementProviderRequest,
    RequirementRefinementProviderResult,
)


REQUIREMENT_REFINEMENT_PROMPT_VERSION = "reqpilotx-requirement-refinement/v1"
REQUIREMENT_REFINEMENT_RESPONSE_SCHEMA_VERSION = "qwen-requirement-refinement/v1"
_MACHINE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _RefinementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suggested_statement: str = Field(min_length=1, max_length=500_000)
    rationale: str = Field(min_length=1, max_length=20_000)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _MACHINE_KEY_PATTERN.fullmatch(item) for item in value):
            raise ValueError("refinement reason codes must be machine-readable")
        if value != tuple(sorted(set(value))):
            raise ValueError("refinement reason codes must be unique and sorted")
        return value


class BailianRequirementRefinementProvider:
    """Source-bound refinement over the existing controlled Bailian transport."""

    def __init__(self, semantic_provider: BailianQwenSemanticProvider) -> None:
        if not isinstance(semantic_provider, BailianQwenSemanticProvider):
            raise TypeError("semantic_provider must be BailianQwenSemanticProvider")
        self._semantic_provider = semantic_provider

    def suggest(
        self,
        request: RequirementRefinementProviderRequest,
    ) -> RequirementRefinementProviderResult:
        if not isinstance(request, RequirementRefinementProviderRequest):
            raise TypeError("request must be RequirementRefinementProviderRequest")
        invocation: Any = self._semantic_provider._invoke_with_invalid_output_retry(
            operation="refine_atomic_obligation",
            prompt_version=REQUIREMENT_REFINEMENT_PROMPT_VERSION,
            response_schema_version=(REQUIREMENT_REFINEMENT_RESPONSE_SCHEMA_VERSION),
            system_prompt=_refinement_system_prompt(),
            user_payload={
                "source_lineage_sha256": request.lineage_sha256,
                "atomization_run_id": request.atomization_run_id,
                "atomization_revision": request.atomization_revision,
                "atomic_node_id": request.atomic_node_id,
                "requirement_subtype": request.requirement_subtype,
                "original_statement": request.original_statement,
                "source_context_statement": request.source_context_statement,
                "parent_context_statement": request.parent_context_statement,
            },
            images=(),
            payload_type=_RefinementPayload,
        )
        run = invocation.run
        if run.status is not SemanticProviderRunStatus.SUCCESS or not isinstance(
            invocation.payload, _RefinementPayload
        ):
            raise RequirementRefinementProviderError(
                str(run.error_code or "refinement_provider_failed"),
                "The controlled refinement provider did not return a valid suggestion.",
            )
        payload = invocation.payload
        if payload.suggested_statement.strip() == request.original_statement.strip():
            raise RequirementRefinementProviderError(
                "refinement_provider_copied_source",
                "The provider copied the source instead of producing a suggestion.",
            )
        return RequirementRefinementProviderResult(
            suggested_statement=payload.suggested_statement.strip(),
            rationale=payload.rationale.strip(),
            reason_codes=payload.reason_codes,
            run=run,
        )


def _refinement_system_prompt() -> str:
    return (
        "You are a requirements-engineering refinement capability. "
        "Return one independent, source-faithful improvement for the supplied "
        "atomic obligation. The source_context_statement is the immutable full "
        "source statement before atomization; parent_context_statement is its "
        "structural parent when one exists. Use those contexts to preserve shared "
        "conditions and qualifiers, but do not import unrelated duties. Preserve "
        "modality, subject, action, object, limits, conditions, units, and "
        "exceptions. Do not split the obligation, invent "
        "facts, add a verification method, or silently resolve ambiguity. The "
        "suggested statement must not be a verbatim copy. Return strict JSON "
        "matching the supplied schema. reason_codes must be sorted machine keys."
    )


__all__ = [
    "BailianRequirementRefinementProvider",
    "REQUIREMENT_REFINEMENT_PROMPT_VERSION",
    "REQUIREMENT_REFINEMENT_RESPONSE_SCHEMA_VERSION",
]
