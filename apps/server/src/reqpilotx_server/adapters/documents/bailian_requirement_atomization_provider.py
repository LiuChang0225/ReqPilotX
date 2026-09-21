from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from reqpilotx_server.adapters.documents.bailian_semantic_provider import (
    BailianQwenSemanticProvider,
)
from reqpilotx_server.domain.extraction.semantic_provider import SemanticProviderRunStatus
from reqpilotx_server.ports.requirement_atomization_provider import (
    RequirementAtomizationObligation,
    RequirementAtomizationProviderError,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationProviderResult,
)


REQUIREMENT_ATOMIZATION_PROMPT_VERSION = "reqpilotx-requirement-atomization/v3"
REQUIREMENT_ATOMIZATION_RESPONSE_SCHEMA_VERSION = "qwen-requirement-atomization/v2"
_MACHINE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _AtomizationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    obligations: tuple[RequirementAtomizationObligation, ...] = Field(
        min_length=1,
        max_length=128,
    )

    @field_validator("obligations")
    @classmethod
    def validate_obligations(
        cls,
        value: tuple[RequirementAtomizationObligation, ...],
    ) -> tuple[RequirementAtomizationObligation, ...]:
        if any(
            any(not _MACHINE_KEY_PATTERN.fullmatch(code) for code in item.reason_codes)
            for item in value
        ):
            raise ValueError("atomization reason codes must be machine-readable")
        if len(value) > 1:
            normalized = tuple(
                " ".join(item.atomic_statement.split()).casefold() for item in value
            )
            if len(normalized) != len(set(normalized)):
                raise ValueError(
                    "multi-obligation output requires independent atomic statements"
                )
        return value


class BailianRequirementAtomizationProvider:
    """Extract exact evidence ranges and source-faithful standalone obligations."""

    def __init__(self, semantic_provider: BailianQwenSemanticProvider) -> None:
        if not isinstance(semantic_provider, BailianQwenSemanticProvider):
            raise TypeError("semantic_provider must be BailianQwenSemanticProvider")
        self._semantic_provider = semantic_provider

    def atomize(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult:
        if not isinstance(request, RequirementAtomizationProviderRequest):
            raise TypeError("request must be RequirementAtomizationProviderRequest")
        invocation: Any = self._semantic_provider._invoke_with_invalid_output_retry(
            operation="atomize_requirement_obligation",
            prompt_version=REQUIREMENT_ATOMIZATION_PROMPT_VERSION,
            response_schema_version=(REQUIREMENT_ATOMIZATION_RESPONSE_SCHEMA_VERSION),
            system_prompt=_atomization_system_prompt(),
            user_payload=_atomization_user_payload(request),
            images=(),
            payload_type=_AtomizationPayload,
        )
        run = invocation.run
        if run.status is not SemanticProviderRunStatus.SUCCESS or not isinstance(
            invocation.payload, _AtomizationPayload
        ):
            raise RequirementAtomizationProviderError(
                str(run.error_code or "atomization_provider_failed"),
                "The controlled atomization provider did not return valid "
                "source-bound obligations.",
            )
        return RequirementAtomizationProviderResult(
            obligations=invocation.payload.obligations,
            run=run,
        )


def _atomization_user_payload(
    request: RequirementAtomizationProviderRequest,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_lineage_sha256": request.lineage_sha256,
        "review_node_id": request.review_node_id,
        "requirement_subtype": request.requirement_subtype,
        "parent_context_statement": request.parent_context_statement,
        "source_segments": [
            item.model_dump(mode="json") for item in request.source_segments
        ],
    }
    if request.table_context is not None:
        payload["table_context"] = request.table_context.model_dump(mode="json")
    return payload


def _atomization_system_prompt() -> str:
    return (
        "You are a requirements-engineering atomization capability. For every "
        "obligation return both source_ranges over the supplied immutable "
        "source_segments and one atomic_statement. source_ranges are immutable "
        "evidence: they must cover every source character exactly once, in order, "
        "without gaps, overlap, or out-of-range offsets. atomic_statement is a "
        "separate source-faithful projection and must be independently readable "
        "and independently verifiable. It may repeat a shared subject, modality, "
        "condition, qualifier, unit, limit, or exception from the supplied source "
        "segments or parent context when needed for standalone meaning. Never "
        "invent, relax, strengthen, generalize, or omit facts. Do not turn "
        "atomization into refinement or stylistic rewriting; preserve source "
        "wording wherever possible. A descriptive statement or a statement "
        "containing only one independently verifiable obligation must return "
        "exactly one obligation with split_basis=single_or_descriptive. Split "
        "only when the source contains multiple independently verifiable "
        "obligations; then each output uses split_basis=independent_obligation "
        "and every atomic_statement must express a distinct obligation. Do not "
        "split mechanically by verbs, commas, sentences, parameters, examples, "
        "rationale, or non-technical description. When table_context is supplied, "
        "treat it as resolvable evidence for exactly one logical table record. "
        "Preserve its inherited header, shared-value, note, image, and parent "
        "context; never cross that record boundary, change the table topology, "
        "or infer a requirement merely because a row exists. Return strict JSON "
        "matching the schema."
    )


__all__ = [
    "BailianRequirementAtomizationProvider",
    "REQUIREMENT_ATOMIZATION_PROMPT_VERSION",
    "REQUIREMENT_ATOMIZATION_RESPONSE_SCHEMA_VERSION",
]
