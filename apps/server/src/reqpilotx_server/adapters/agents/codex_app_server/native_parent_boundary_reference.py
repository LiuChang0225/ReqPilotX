from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from reqpilotx_server.adapters.agents.codex_app_server.native_reference import (
    _codex_version,
    _safe_process_error,
    _summarize_jsonl_events,
    _write_private_json,
)
from reqpilotx_server.application.extraction.native_parent_boundary_reference import (
    NativeParentBoundaryReceiptV1,
    NativeParentBoundaryReferenceV1,
    NativeParentBoundaryRequestV1,
    canonicalize_native_parent_boundary_reference,
    native_parent_boundary_output_schema,
    validate_native_parent_boundary_reference,
)


NATIVE_CODEX_PARENT_BOUNDARY_RUNNER_VERSION = "1.0.0"
NATIVE_CODEX_PARENT_BOUNDARY_RUN_RECEIPT_SCHEMA_VERSION = (
    "native-codex-parent-boundary-run-receipt/v1"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class NativeCodexParentBoundaryRunReceiptV1(_StrictFrozenModel):
    schema_version: str = NATIVE_CODEX_PARENT_BOUNDARY_RUN_RECEIPT_SCHEMA_VERSION
    runner_version: str = NATIVE_CODEX_PARENT_BOUNDARY_RUNNER_VERSION
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=160)
    codex_version: str = Field(min_length=1, max_length=160)
    elapsed_milliseconds: StrictInt = Field(ge=0)
    event_counts: tuple[str, ...]
    tool_names: tuple[str, ...]
    provision_count: StrictInt = Field(ge=1)
    context_range_count: StrictInt = Field(ge=0)
    grounding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @field_validator("event_counts", "tool_names")
    @classmethod
    def validate_sorted_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("native parent-boundary receipt values must be sorted")
        return value


class NativeCodexParentBoundaryRunResult(_StrictFrozenModel):
    reference: NativeParentBoundaryReferenceV1
    validation_receipt: NativeParentBoundaryReceiptV1
    run_receipt: NativeCodexParentBoundaryRunReceiptV1


def run_native_codex_parent_boundary_reference(
    request: NativeParentBoundaryRequestV1,
    output_directory: Path,
    *,
    codex_binary: str = "codex",
    model_id: str = "gpt-5.6-sol",
    timeout_seconds: int = 900,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> NativeCodexParentBoundaryRunResult:
    if timeout_seconds < 60 or timeout_seconds > 3600:
        raise ValueError(
            "native parent-boundary timeout must be between 60 and 3600 seconds"
        )
    output = output_directory.expanduser().resolve()
    codex_version = _codex_version(codex_binary, command_runner)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="reqpilotx-native-codex-parent-boundary-"
    ) as raw_temp:
        workspace = Path(raw_temp).resolve()
        os.chmod(workspace, 0o700)
        _write_private_json(
            workspace / "scope.json",
            request.model_dump(mode="json"),
            exclusive=True,
        )
        schema_path = workspace / "native-parent-boundary.schema.json"
        _write_private_json(
            schema_path,
            native_parent_boundary_output_schema(),
            exclusive=True,
        )
        final_path = workspace / "native-parent-boundary.json"
        command = _build_command(
            codex_binary=codex_binary,
            model_id=model_id,
            workspace=workspace,
            schema_path=schema_path,
            final_path=final_path,
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
                "native Codex parent-boundary failed: "
                + _safe_process_error(completed.stderr, completed.stdout)
            )
        reference = NativeParentBoundaryReferenceV1.model_validate(
            _normalize_codes(json.loads(final_path.read_text(encoding="utf-8")))
        )
        reference = canonicalize_native_parent_boundary_reference(request, reference)
        validation_receipt = validate_native_parent_boundary_reference(
            request,
            reference,
        )
        event_counts, tool_names = _summarize_jsonl_events(completed.stdout)
    run_receipt = NativeCodexParentBoundaryRunReceiptV1(
        source_sha256=request.source_sha256,
        scope_id=request.scope_id,
        model_id=model_id,
        codex_version=codex_version,
        elapsed_milliseconds=round((time.monotonic() - started) * 1000),
        event_counts=event_counts,
        tool_names=tool_names,
        provision_count=validation_receipt.provision_count,
        context_range_count=validation_receipt.context_range_count,
        grounding_sha256=validation_receipt.grounding_sha256,
        decision_sha256=validation_receipt.decision_sha256,
    )
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    for filename, payload in (
        ("native_parent_boundary_reference.json", reference.model_dump(mode="json")),
        (
            "native_parent_boundary_validation_receipt.json",
            validation_receipt.model_dump(mode="json"),
        ),
        (
            "native_parent_boundary_run_receipt.json",
            run_receipt.model_dump(mode="json"),
        ),
    ):
        _write_private_json(output / filename, payload, exclusive=True)
    return NativeCodexParentBoundaryRunResult(
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
    final_path: Path,
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
        str(final_path),
        "-",
    ]


def _prompt(request: NativeParentBoundaryRequestV1) -> str:
    return (
        "You are producing a developer-only native Codex provision-boundary reference for "
        "ReqPilotX. Read scope.json. Treat source_segments as immutable source truth. Identify "
        "non-overlapping complete provision bodies suitable for later requirement recognition "
        "and decomposition. Preserve the source hierarchy through explicit_label and ordered "
        "ancestor_labels; ancestor_labels must contain only true ancestors and must never repeat "
        "the provision's own explicit_label. Do not classify every sentence, table cell, heading, or list marker "
        "as a provision. A numbered/titled parent that contains subordinate 1)/a)/bullet/table "
        "items must remain one complete parent body; do not emit those nested items as separate "
        "provisions because downstream atomization owns that split. Put all non-provision source "
        "into context ranges. Every non-whitespace "
        "source character must be covered exactly once by a provision body range or context "
        "range. Provision IDs must be native-provision- followed by 32 lowercase hex chars; "
        "source_order must be unique ascending. Codes must be lowercase snake_case. Use "
        "UNRESOLVED context and PARTIAL only when the parent boundary cannot be established. "
        f"Keep source_sha256 {request.source_sha256} and scope_id {request.scope_id}. Return "
        "only schema-valid JSON."
    )


def _normalize_codes(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    diagnostics = normalized.get("diagnostic_codes")
    if isinstance(diagnostics, list):
        normalized["diagnostic_codes"] = sorted(
            {item for item in diagnostics if isinstance(item, str)}
        )
    for field_name in ("provisions", "context_ranges"):
        values = normalized.get(field_name)
        if not isinstance(values, list):
            continue
        normalized_values: list[object] = []
        for value in values:
            if not isinstance(value, dict):
                normalized_values.append(value)
                continue
            item = dict(value)
            reasons = item.get("reason_codes")
            if isinstance(reasons, list):
                item["reason_codes"] = sorted(
                    {reason for reason in reasons if isinstance(reason, str)}
                )
            normalized_values.append(item)
        normalized[field_name] = normalized_values
    return normalized


__all__ = [
    "NATIVE_CODEX_PARENT_BOUNDARY_RUNNER_VERSION",
    "NativeCodexParentBoundaryRunReceiptV1",
    "NativeCodexParentBoundaryRunResult",
    "run_native_codex_parent_boundary_reference",
]
