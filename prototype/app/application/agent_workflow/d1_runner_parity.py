from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field


D1_DUAL_RUNNER_PARITY_SCHEMA_VERSION = "reqpilotx-d1-dual-runner-parity/v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D1DualRunnerParityReportV1(_StrictFrozenModel):
    schema_version: str = D1_DUAL_RUNNER_PARITY_SCHEMA_VERSION
    status: str = Field(pattern=r"^(PASS|DIFFERENT)$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: str = Field(pattern=r"^(low|medium|high|xhigh)$")
    requested_stages: tuple[str, ...]
    stage_schema_status: tuple[str, ...]
    tool_names: tuple[str, ...]
    quality_checks: tuple[str, ...]
    allowed_difference_codes: tuple[str, ...]
    gap_codes: tuple[str, ...]
    reference_visual_observation_count: int = Field(ge=0)
    product_visual_observation_count: int = Field(ge=0)


def compare_d1_runner_receipts(
    reference: Mapping[str, Any],
    product: Mapping[str, Any],
) -> D1DualRunnerParityReportV1:
    gaps: set[str] = set()
    reference_execution = _mapping(reference.get("execution_result"), "reference")
    product_execution = _mapping(product.get("execution_result"), "product")
    reference_kit = _mapping(reference.get("agent_kit"), "reference_kit")
    product_kit = _mapping(product.get("agent_kit"), "product_kit")
    reference_quality = _mapping(reference.get("quality"), "reference_quality")
    product_quality = _mapping(product.get("quality"), "product_quality")
    reference_mcp = _mapping(reference.get("mcp"), "reference_mcp")
    product_mcp = _mapping(product.get("mcp"), "product_mcp")

    equal_checks = {
        "runner_status": (reference.get("status"), product.get("status")),
        "quality_gate_eligible": (
            reference.get("quality_gate_eligible"),
            product.get("quality_gate_eligible"),
        ),
        "model_id": (reference.get("model_id"), product.get("model_id")),
        "reasoning_effort": (
            reference.get("reasoning_effort"),
            product.get("reasoning_effort"),
        ),
        "source_sha256": (
            reference_execution.get("source_sha256"),
            product_execution.get("source_sha256"),
        ),
        "kit_bundle_sha256": (
            reference_kit.get("bundle_sha256"),
            product_kit.get("bundle_sha256"),
        ),
        "stage_context_sha256": (
            reference_kit.get("stage_context_sha256"),
            product_kit.get("stage_context_sha256"),
        ),
        "requested_stages": (
            reference_execution.get("requested_stages"),
            product_execution.get("requested_stages"),
        ),
        "stage_schema_status": (
            _stage_schema_status(reference_execution),
            _stage_schema_status(product_execution),
        ),
        "tool_names": (
            tuple(sorted(str(item) for item in reference_mcp.get("tool_names", []))),
            tuple(sorted(str(item) for item in product_mcp.get("tool_names", []))),
        ),
        "quality_page_count": (
            reference_quality.get("page_count"),
            product_quality.get("page_count"),
        ),
        "quality_table_count": (
            reference_quality.get("table_count_page_1"),
            product_quality.get("table_count_page_1"),
        ),
        "quality_blank_pages": (
            reference_quality.get("intentional_blank_pages"),
            product_quality.get("intentional_blank_pages"),
        ),
        "quality_visual_text": (
            reference_quality.get("visual_text_golden_pass"),
            product_quality.get("visual_text_golden_pass"),
        ),
        "quality_prompt_injection": (
            reference_quality.get("prompt_injection_capability_expansion"),
            product_quality.get("prompt_injection_capability_expansion"),
        ),
        "schema_sha256": (
            reference.get("schema_sha256"),
            product.get("schema_sha256"),
        ),
    }
    for name, (left, right) in equal_checks.items():
        if left != right:
            gaps.add(name + "_mismatch")
    reference_visual_count = int(
        reference_quality.get("visual_observation_count_page_2") or 0
    )
    product_visual_count = int(
        product_quality.get("visual_observation_count_page_2") or 0
    )
    if reference_visual_count < 1 or product_visual_count < 1:
        gaps.add("visual_observation_missing")
    source_sha256 = str(reference_execution.get("source_sha256") or "")
    bundle_sha256 = str(reference_kit.get("bundle_sha256") or "")
    model_id = str(reference.get("model_id") or "")
    effort = str(reference.get("reasoning_effort") or "")
    requested_stages = tuple(
        str(item) for item in reference_execution.get("requested_stages", [])
    )
    stage_schema_status = _stage_schema_status(reference_execution)
    tool_names = tuple(
        sorted(str(item) for item in reference_mcp.get("tool_names", []))
    )
    return D1DualRunnerParityReportV1(
        status="PASS" if not gaps else "DIFFERENT",
        source_sha256=source_sha256,
        kit_bundle_sha256=bundle_sha256,
        model_id=model_id,
        reasoning_effort=effort,
        requested_stages=requested_stages,
        stage_schema_status=stage_schema_status,
        tool_names=tool_names,
        quality_checks=(
            "intentional_blank",
            "native_table",
            "page_exactly_once",
            "prompt_injection_no_capability_expansion",
            "visual_authority",
            "visual_text_golden",
        ),
        allowed_difference_codes=(
            "adapter_and_runner_profile_identity",
            "artifact_and_run_ids",
            "event_transport_shape",
            "latency_and_token_usage",
            "proposal_evidence_and_receipt_hashes",
            "tool_call_count_and_order",
            "visual_observation_granularity",
        ),
        gap_codes=tuple(sorted(gaps)),
        reference_visual_observation_count=reference_visual_count,
        product_visual_observation_count=product_visual_count,
    )


def _stage_schema_status(execution: Mapping[str, Any]) -> tuple[str, ...]:
    artifacts = execution.get("stage_artifacts")
    if not isinstance(artifacts, list):
        return ()
    return tuple(
        f"{item.get('stage')}:{item.get('proposal_schema_id')}:{item.get('status')}"
        for item in artifacts
        if isinstance(item, Mapping)
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"D1 parity receipt is missing {label}")
    return value


__all__ = [
    "D1_DUAL_RUNNER_PARITY_SCHEMA_VERSION",
    "D1DualRunnerParityReportV1",
    "compare_d1_runner_receipts",
]
