from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationObligation,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationProviderResult,
    RequirementAtomizationRange,
)


LOCAL_REQUIREMENT_ATOMIZATION_PROVIDER_ID = (
    "local-source-faithful-pass-through"
)
LOCAL_REQUIREMENT_ATOMIZATION_PROVIDER_VERSION = "1.0.0"
LOCAL_REQUIREMENT_ATOMIZATION_MODEL_ID = "not-invoked"
LOCAL_REQUIREMENT_ATOMIZATION_PROMPT_VERSION = (
    "local-pass-through-not-invoked/v1"
)
LOCAL_REQUIREMENT_ATOMIZATION_RESPONSE_SCHEMA_VERSION = (
    "local-pass-through-obligation/v1"
)


class LocalPassThroughRequirementAtomizationProvider:
    """Keep one exact source-bound obligation when no remote provider exists."""

    def atomize(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult:
        if not isinstance(request, RequirementAtomizationProviderRequest):
            raise TypeError("request must be RequirementAtomizationProviderRequest")

        request_sha256 = hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        timestamp = datetime.now(timezone.utc)
        status = SemanticProviderRunStatus.SKIPPED
        run = SemanticProviderRunRecord(
            provider_run_id=stable_semantic_provider_run_id(
                operation="atomize_requirement_obligation",
                provider_id=LOCAL_REQUIREMENT_ATOMIZATION_PROVIDER_ID,
                model_id=LOCAL_REQUIREMENT_ATOMIZATION_MODEL_ID,
                request_sha256=request_sha256,
                response_sha256=None,
                status=status,
                started_at=timestamp,
            ),
            operation="atomize_requirement_obligation",
            provider_id=LOCAL_REQUIREMENT_ATOMIZATION_PROVIDER_ID,
            provider_version=LOCAL_REQUIREMENT_ATOMIZATION_PROVIDER_VERSION,
            model_id=LOCAL_REQUIREMENT_ATOMIZATION_MODEL_ID,
            prompt_version=LOCAL_REQUIREMENT_ATOMIZATION_PROMPT_VERSION,
            response_schema_version=(
                LOCAL_REQUIREMENT_ATOMIZATION_RESPONSE_SCHEMA_VERSION
            ),
            request_sha256=request_sha256,
            response_sha256=None,
            status=status,
            started_at=timestamp,
            finished_at=timestamp,
            latency_ms=0,
            request_count=0,
            retry_count=0,
            error_code="remote_provider_not_configured",
        )
        return RequirementAtomizationProviderResult(
            obligations=(
                RequirementAtomizationObligation(
                    source_ranges=tuple(
                        RequirementAtomizationRange(
                            segment_index=item.segment_index,
                            start_offset=0,
                            end_offset=len(item.source_text),
                        )
                        for item in request.source_segments
                    ),
                    atomic_statement="".join(
                        item.source_text for item in request.source_segments
                    ),
                    split_basis="single_or_descriptive",
                    reason_codes=("local_pass_through",),
                ),
            ),
            run=run,
        )


__all__ = [
    "LOCAL_REQUIREMENT_ATOMIZATION_MODEL_ID",
    "LOCAL_REQUIREMENT_ATOMIZATION_PROMPT_VERSION",
    "LOCAL_REQUIREMENT_ATOMIZATION_PROVIDER_ID",
    "LOCAL_REQUIREMENT_ATOMIZATION_PROVIDER_VERSION",
    "LOCAL_REQUIREMENT_ATOMIZATION_RESPONSE_SCHEMA_VERSION",
    "LocalPassThroughRequirementAtomizationProvider",
]
