from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Callable
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from reqpilotx_server.adapters.agents.codex_app_server.native_reference import (
    _codex_version,
    _canonical_json_sha256,
    _safe_process_error,
    _summarize_jsonl_events,
    _write_private_json,
)
from reqpilotx_server.application.extraction.native_atomization_reference import (
    NativeAtomizationContextDisposition,
    NativeAtomizationReferenceReceiptV1,
    NativeAtomizationReferenceV1,
    native_atomization_reference_output_schema,
    validate_native_atomization_reference,
)
from reqpilotx_server.application.extraction.native_atomization_repeatability import (
    build_native_atomization_consensus,
    evaluate_native_atomization_repeatability,
    select_native_atomization_consensus_canonical,
)
from reqpilotx_server.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from reqpilotx_server.ports.requirement_atomization_provider import (
    RequirementAtomizationContextDisposition,
    RequirementAtomizationContextRange,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationProviderResult,
)


NATIVE_CODEX_ATOMIZATION_REFERENCE_RUNNER_VERSION = "1.0.0"
NATIVE_CODEX_ATOMIZATION_RUN_RECEIPT_SCHEMA_VERSION = (
    "native-codex-atomization-run-receipt/v1"
)
CODEX_CONSENSUS_ATOMIZATION_PROVIDER_ID = "codex-native-consensus-atomization"
CODEX_CONSENSUS_ATOMIZATION_PROVIDER_VERSION = "1.0.0"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class NativeCodexAtomizationRunReceiptV1(_StrictFrozenModel):
    schema_version: str = NATIVE_CODEX_ATOMIZATION_RUN_RECEIPT_SCHEMA_VERSION
    runner_version: str = NATIVE_CODEX_ATOMIZATION_REFERENCE_RUNNER_VERSION
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_node_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=160)
    codex_version: str = Field(min_length=1, max_length=160)
    elapsed_milliseconds: StrictInt = Field(ge=0)
    event_counts: tuple[str, ...]
    tool_names: tuple[str, ...]
    obligation_count: StrictInt = Field(ge=1)
    context_range_count: StrictInt = Field(ge=0)
    grounding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @field_validator("event_counts", "tool_names")
    @classmethod
    def validate_sorted_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("native atomization run receipt values must be sorted")
        return value


class NativeCodexAtomizationRunResult(_StrictFrozenModel):
    reference: NativeAtomizationReferenceV1
    validation_receipt: NativeAtomizationReferenceReceiptV1
    run_receipt: NativeCodexAtomizationRunReceiptV1


class CodexConsensusRequirementAtomizationProvider:
    """Developer-only shadow adapter backed by repeated native Codex runs."""

    def __init__(
        self,
        *,
        repetition_count: int = 3,
        codex_binary: str = "codex",
        model_id: str = "gpt-5.6-sol",
        timeout_seconds: int = 900,
    ) -> None:
        if repetition_count < 2 or repetition_count > 5:
            raise ValueError("Codex consensus repetition_count must be between 2 and 5")
        self._repetition_count = repetition_count
        self._codex_binary = codex_binary
        self._model_id = model_id
        self._timeout_seconds = timeout_seconds

    def atomize(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult:
        if not isinstance(request, RequirementAtomizationProviderRequest):
            raise TypeError("request must be RequirementAtomizationProviderRequest")
        started_at = datetime.now(timezone.utc)
        references: list[NativeAtomizationReferenceV1] = []
        with tempfile.TemporaryDirectory(
            prefix="reqpilotx-codex-consensus-atomization-"
        ) as raw_temp:
            root = Path(raw_temp).resolve()
            os.chmod(root, 0o700)
            for run_index in range(1, self._repetition_count + 1):
                result = run_native_codex_atomization_reference(
                    request,
                    root / f"run-{run_index}",
                    codex_binary=self._codex_binary,
                    model_id=self._model_id,
                    timeout_seconds=self._timeout_seconds,
                )
                references.append(result.reference)
        repeatability = evaluate_native_atomization_repeatability(
            tuple(
                (f"run_{index}", request, reference)
                for index, reference in enumerate(references, 1)
            )
        )
        canonical = select_native_atomization_consensus_canonical(
            request,
            tuple(references),
            repeatability,
        )
        consensus = build_native_atomization_consensus(
            request,
            canonical,
            repeatability,
        )
        validate_native_atomization_reference(request, consensus)
        request_sha256 = _canonical_json_sha256(request.model_dump(mode="json"))
        response_sha256 = _canonical_json_sha256(consensus.model_dump(mode="json"))
        finished_at = datetime.now(timezone.utc)
        status = (
            SemanticProviderRunStatus.SUCCESS
            if consensus.status.value == "OBSERVED"
            else SemanticProviderRunStatus.PARTIAL
        )
        error_code = (
            None
            if status is SemanticProviderRunStatus.SUCCESS
            else "atomization_consensus_unresolved"
        )
        run = SemanticProviderRunRecord(
            provider_run_id=stable_semantic_provider_run_id(
                operation="atomize_requirement_obligation",
                provider_id=CODEX_CONSENSUS_ATOMIZATION_PROVIDER_ID,
                model_id=self._model_id,
                request_sha256=request_sha256,
                response_sha256=response_sha256,
                status=status,
                started_at=started_at,
            ),
            operation="atomize_requirement_obligation",
            provider_id=CODEX_CONSENSUS_ATOMIZATION_PROVIDER_ID,
            provider_version=CODEX_CONSENSUS_ATOMIZATION_PROVIDER_VERSION,
            model_id=self._model_id,
            prompt_version="native-codex-atomization-reference/v1",
            response_schema_version="native-atomization-reference/v1",
            request_sha256=request_sha256,
            response_sha256=response_sha256,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=round((finished_at - started_at).total_seconds() * 1000),
            request_count=self._repetition_count,
            retry_count=0,
            error_code=error_code,
        )
        disposition_map = {
            NativeAtomizationContextDisposition.CONTEXT: RequirementAtomizationContextDisposition.CONTEXT,
            NativeAtomizationContextDisposition.HEADING: RequirementAtomizationContextDisposition.HEADING,
            NativeAtomizationContextDisposition.METHOD: RequirementAtomizationContextDisposition.METHOD,
            NativeAtomizationContextDisposition.PRESENTATION: RequirementAtomizationContextDisposition.PRESENTATION,
            NativeAtomizationContextDisposition.REFERENCE: RequirementAtomizationContextDisposition.REFERENCE,
            NativeAtomizationContextDisposition.UNRESOLVED: RequirementAtomizationContextDisposition.UNRESOLVED,
        }
        return RequirementAtomizationProviderResult(
            obligations=consensus.obligations,
            context_ranges=tuple(
                RequirementAtomizationContextRange(
                    source_range=item.source_range,
                    disposition=disposition_map[item.disposition],
                    reason_codes=item.reason_codes,
                )
                for item in consensus.context_ranges
            ),
            run=run,
        )


def run_native_codex_atomization_reference(
    request: RequirementAtomizationProviderRequest,
    output_directory: Path,
    *,
    codex_binary: str = "codex",
    model_id: str = "gpt-5.6-sol",
    timeout_seconds: int = 900,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> NativeCodexAtomizationRunResult:
    if timeout_seconds < 60 or timeout_seconds > 3600:
        raise ValueError(
            "native atomization timeout must be between 60 and 3600 seconds"
        )
    output = output_directory.expanduser().resolve()
    codex_version = _codex_version(codex_binary, command_runner)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="reqpilotx-native-codex-atomization-"
    ) as raw_temp:
        workspace = Path(raw_temp).resolve()
        os.chmod(workspace, 0o700)
        _write_private_json(
            workspace / "scope.json",
            request.model_dump(mode="json"),
            exclusive=True,
        )
        schema_path = workspace / "native-atomization.schema.json"
        _write_private_json(
            schema_path,
            native_atomization_reference_output_schema(),
            exclusive=True,
        )
        final_message_path = workspace / "native-atomization.json"
        command = _build_command(
            codex_binary=codex_binary,
            model_id=model_id,
            workspace=workspace,
            schema_path=schema_path,
            final_message_path=final_message_path,
        )
        completed = command_runner(
            command,
            input=_prompt(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "native Codex atomization failed: "
                + _safe_process_error(completed.stderr, completed.stdout)
            )
        reference = NativeAtomizationReferenceV1.model_validate(
            _normalize_reference_codes(
                json.loads(final_message_path.read_text(encoding="utf-8"))
            )
        )
        validation_receipt = validate_native_atomization_reference(
            request,
            reference,
        )
        event_counts, tool_names = _summarize_jsonl_events(completed.stdout)

    elapsed_milliseconds = round((time.monotonic() - started) * 1000)
    run_receipt = NativeCodexAtomizationRunReceiptV1(
        lineage_sha256=request.lineage_sha256,
        review_node_id=request.review_node_id,
        model_id=model_id,
        codex_version=codex_version,
        elapsed_milliseconds=elapsed_milliseconds,
        event_counts=event_counts,
        tool_names=tool_names,
        obligation_count=validation_receipt.obligation_count,
        context_range_count=validation_receipt.context_range_count,
        grounding_sha256=validation_receipt.grounding_sha256,
        decision_sha256=validation_receipt.decision_sha256,
    )
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    _write_private_json(
        output / "native_atomization_reference.json",
        reference.model_dump(mode="json"),
        exclusive=True,
    )
    _write_private_json(
        output / "native_atomization_validation_receipt.json",
        validation_receipt.model_dump(mode="json"),
        exclusive=True,
    )
    _write_private_json(
        output / "native_atomization_run_receipt.json",
        run_receipt.model_dump(mode="json"),
        exclusive=True,
    )
    return NativeCodexAtomizationRunResult(
        reference=reference,
        validation_receipt=validation_receipt,
        run_receipt=run_receipt,
    )


def _build_command(
    *,
    codex_binary: str,
    model_id: str,
    workspace: Path,
    schema_path: Path,
    final_message_path: Path,
) -> list[str]:
    return [
        codex_binary,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "--color",
        "never",
        "--model",
        model_id,
        "--sandbox",
        "workspace-write",
        "--cd",
        str(workspace),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(final_message_path),
        "-",
    ]


def _normalize_reference_codes(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    diagnostics = normalized.get("diagnostic_codes")
    if isinstance(diagnostics, list):
        normalized["diagnostic_codes"] = sorted(
            {item for item in diagnostics if isinstance(item, str)}
        )
    for field_name in ("obligations", "context_ranges"):
        values = normalized.get(field_name)
        if not isinstance(values, list):
            continue
        normalized_values: list[object] = []
        for value in values:
            if not isinstance(value, dict):
                normalized_values.append(value)
                continue
            normalized_value = dict(value)
            reason_codes = normalized_value.get("reason_codes")
            if isinstance(reason_codes, list):
                normalized_value["reason_codes"] = sorted(
                    {item for item in reason_codes if isinstance(item, str)}
                )
            normalized_values.append(normalized_value)
        normalized[field_name] = normalized_values
    return normalized


def _prompt(request: RequirementAtomizationProviderRequest) -> str:
    return (
        "You are producing a developer-only native Codex requirement decomposition "
        "reference for ReqPilotX. Read scope.json. The immutable source_segments are the "
        "only source of truth. Preserve the complete parent; identify only independently "
        "verifiable obligations. Do not split mechanically by sentence, comma, paragraph, "
        "or modal word. Return exact zero-based source ranges within the supplied segments. "
        "Every non-whitespace source character must be covered exactly once by an obligation "
        "or a context range. Use context disposition for headings, methods, references, "
        "presentation, and non-obligation context. Use UNRESOLVED only when evidence is "
        "insufficient; status must then be PARTIAL, otherwise OBSERVED. split_basis must be "
        "exactly single_or_descriptive or independent_obligation. All "
        "reason_codes and diagnostic_codes must be lowercase snake_case identifiers. "
        f"Do not change lineage_sha256 {request.lineage_sha256} or review_node_id "
        f"{request.review_node_id}. Return only schema-valid JSON."
    )


__all__ = [
    "CODEX_CONSENSUS_ATOMIZATION_PROVIDER_ID",
    "CODEX_CONSENSUS_ATOMIZATION_PROVIDER_VERSION",
    "CodexConsensusRequirementAtomizationProvider",
    "NATIVE_CODEX_ATOMIZATION_REFERENCE_RUNNER_VERSION",
    "NATIVE_CODEX_ATOMIZATION_RUN_RECEIPT_SCHEMA_VERSION",
    "NativeCodexAtomizationRunReceiptV1",
    "NativeCodexAtomizationRunResult",
    "run_native_codex_atomization_reference",
]
