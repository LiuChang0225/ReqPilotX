from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pypdf import PdfReader

from reqpilotx_server.application.extraction.codex_extraction_parity import (
    CodexExtractionObservationV1,
    CodexExtractionStage,
    NativeCodexReferencePayloadV1,
    build_native_codex_reference_observation,
    native_codex_reference_output_schema,
)


NATIVE_CODEX_REFERENCE_RUNNER_VERSION = "1.2.0"
NATIVE_CODEX_REFERENCE_RECEIPT_SCHEMA_VERSION = "native-codex-reference-receipt/v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class NativeCodexReferenceReceiptV1(_StrictFrozenModel):
    schema_version: str = NATIVE_CODEX_REFERENCE_RECEIPT_SCHEMA_VERSION
    runner_version: str = NATIVE_CODEX_REFERENCE_RUNNER_VERSION
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: StrictInt = Field(ge=1)
    model_id: str = Field(min_length=1, max_length=160)
    codex_version: str = Field(min_length=1, max_length=160)
    elapsed_milliseconds: StrictInt = Field(ge=0)
    event_counts: tuple[str, ...]
    tool_names: tuple[str, ...]
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_id: str = Field(pattern=r"^codex-extraction-observation-[0-9a-f]{32}$")
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @field_validator("event_counts", "tool_names")
    @classmethod
    def validate_sorted_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError(
                "native reference receipt values must be unique and sorted"
            )
        return value


class NativeCodexReferenceRunResult(_StrictFrozenModel):
    payload: NativeCodexReferencePayloadV1
    observation: CodexExtractionObservationV1
    receipt: NativeCodexReferenceReceiptV1


def run_native_codex_reference(
    pdf_path: Path,
    output_directory: Path,
    *,
    codex_binary: str = "codex",
    model_id: str = "gpt-5.6-sol",
    timeout_seconds: int = 3600,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> NativeCodexReferenceRunResult:
    source = pdf_path.expanduser().resolve(strict=True)
    if source.suffix.lower() != ".pdf" or not source.is_file():
        raise ValueError("native Codex reference input must be one PDF file")
    output = output_directory.expanduser().resolve()
    if output == source.parent or source.parent in output.parents:
        raise ValueError(
            "native Codex reference output cannot be inside the source directory"
        )
    if timeout_seconds < 60 or timeout_seconds > 4 * 3600:
        raise ValueError(
            "native Codex reference timeout must be between 60 and 14400 seconds"
        )

    source_sha256 = _sha256(source)
    page_count = len(PdfReader(str(source), strict=True).pages)
    codex_version = _codex_version(codex_binary, command_runner)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="reqpilotx-native-codex-reference-"
    ) as raw_temp:
        workspace = Path(raw_temp).resolve()
        os.chmod(workspace, 0o700)
        pdf_directory = workspace / "tmp" / "pdfs"
        pdf_directory.mkdir(parents=True, mode=0o700)
        copied_pdf = pdf_directory / "document.pdf"
        shutil.copyfile(source, copied_pdf)
        os.chmod(copied_pdf, 0o400)
        schema_path = workspace / "native-reference.schema.json"
        _write_private_json(
            schema_path,
            native_codex_reference_output_schema(),
            exclusive=True,
        )
        final_message_path = workspace / "native-reference-payload.json"
        prompt = _reference_prompt(
            source_sha256=source_sha256,
            page_count=page_count,
            model_id=model_id,
        )
        command = _build_command(
            codex_binary=codex_binary,
            model_id=model_id,
            workspace=workspace,
            schema_path=schema_path,
            final_message_path=final_message_path,
        )
        completed = command_runner(
            command,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "native Codex reference failed: "
                + _safe_process_error(completed.stderr, completed.stdout)
            )
        payload = NativeCodexReferencePayloadV1.model_validate(
            _normalize_v1_reference_payload(
                json.loads(final_message_path.read_text(encoding="utf-8"))
            )
        )
        if payload.source_sha256 != source_sha256:
            raise ValueError("native Codex reference returned a stale source identity")
        if payload.page_count != page_count:
            raise ValueError("native Codex reference returned an invalid page count")
        if payload.model_id != model_id:
            raise ValueError(
                "native Codex reference returned an unexpected model identity"
            )
        if "pdf" not in payload.skill_ids:
            raise ValueError("native Codex reference did not declare the PDF Skill")
        observation = build_native_codex_reference_observation(payload)
        event_counts, tool_names = _summarize_jsonl_events(completed.stdout)

    elapsed_milliseconds = round((time.monotonic() - started) * 1000)
    payload_json = payload.model_dump(mode="json")
    payload_sha256 = _canonical_json_sha256(payload_json)
    receipt = NativeCodexReferenceReceiptV1(
        source_sha256=source_sha256,
        page_count=page_count,
        model_id=model_id,
        codex_version=codex_version,
        elapsed_milliseconds=elapsed_milliseconds,
        event_counts=event_counts,
        tool_names=tool_names,
        payload_sha256=payload_sha256,
        observation_id=observation.observation_id,
    )
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    _write_private_json(
        output / "native_reference_payload.json",
        payload_json,
        exclusive=True,
    )
    _write_private_json(
        output / "native_reference_observation.json",
        observation.model_dump(mode="json"),
        exclusive=True,
    )
    _write_private_json(
        output / "native_reference_receipt.json",
        receipt.model_dump(mode="json"),
        exclusive=True,
    )
    return NativeCodexReferenceRunResult(
        payload=payload,
        observation=observation,
        receipt=receipt,
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


def _normalize_v1_reference_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return payload
    normalized = dict(payload)
    normalized_stages: list[object] = []
    semantic_stages = {
        CodexExtractionStage.REQUIREMENT_RECOGNITION.value,
        CodexExtractionStage.REQUIREMENT_DECOMPOSITION.value,
    }
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("stage") not in semantic_stages:
            normalized_stages.append(stage)
            continue
        normalized_stage = dict(stage)
        normalized_stage["status"] = "NOT_RUN"
        metrics = normalized_stage.get("metrics")
        if isinstance(metrics, list):
            normalized_stage["metrics"] = [
                {**item, "value": 0} if isinstance(item, dict) else item
                for item in metrics
            ]
        normalized_stage["capabilities_used"] = []
        normalized_stage["diagnostic_codes"] = ["source_bound_stage_deferred"]
        normalized_stages.append(normalized_stage)
    normalized["stages"] = normalized_stages
    return normalized


def _reference_prompt(*, source_sha256: str, page_count: int, model_id: str) -> str:
    metric_contract = {
        CodexExtractionStage.PAGE_UNDERSTANDING.value: (
            "page_count,pages_observed,pages_unresolved"
        ),
        CodexExtractionStage.STRUCTURE.value: (
            "native_reference_figures,native_reference_logical_tables,"
            "native_reference_numbered_sections"
        ),
        CodexExtractionStage.REQUIREMENT_RECOGNITION.value: (
            "native_reference_requirement_parents,native_reference_semantic_unresolved"
        ),
        CodexExtractionStage.REQUIREMENT_DECOMPOSITION.value: (
            "native_reference_requirement_children,"
            "native_reference_requirement_parents,"
            "native_reference_unresolved_spans"
        ),
    }
    return (
        "$pdf\n"
        "You are producing a developer-only native Codex reference observation for "
        "ReqPilotX. Work only inside this temporary workspace. Read tmp/pdfs/document.pdf "
        "with the PDF Skill. Inspect the complete PDF, render pages as needed, and visually "
        "verify layout; pdftotext alone is not sufficient. Do not modify document.pdf. "
        "Do not infer document authority from its filename.\n\n"
        f"Expected source_sha256: {source_sha256}\n"
        f"Expected page_count: {page_count}\n"
        f"model_id: {model_id}\n"
        'skill_ids must be exactly ["pdf"]. is_human_gold and '
        "is_serving_activation_gate must be false.\n\n"
        "Return the four stages exactly once and in this order: PAGE_UNDERSTANDING, "
        "STRUCTURE, REQUIREMENT_RECOGNITION, REQUIREMENT_DECOMPOSITION. Status means "
        "OBSERVED when you actually completed that stage, PARTIAL when evidence remains, "
        "BLOCKED when attempted but impossible, and NOT_RUN when not attempted. "
        "This v1 run covers only Page Understanding and Structure. Do not perform or estimate "
        "Recognition or Decomposition. Those two stages must be NOT_RUN, all their metrics "
        "must be zero, capabilities_used must be empty, and diagnostic_codes must be exactly "
        '["source_bound_stage_deferred"]. '
        "Metrics must be unique and alphabetically sorted, using exactly these names: "
        f"{json.dumps(metric_contract, sort_keys=True)}. "
        "Use only actual non-negative counts. capabilities_used and diagnostic_codes must "
        "be unique and alphabetically sorted, content-free snake_case identifiers. The only "
        "allowed capabilities are figure_inspection, page_render, "
        "parent_child_decomposition, pdf_native_text, semantic_reasoning, "
        "source_grounding, structure_reconstruction, table_reconstruction, and "
        "visual_inspection. OBSERVED/PARTIAL page understanding must include page_render; "
        "Structure must include structure_reconstruction; Recognition must include "
        "semantic_reasoning; Decomposition must include parent_child_decomposition. Do not include "
        "customer text in diagnostics. Leave all decision/grounding SHA and fingerprint "
        "kind fields null; the host will add normalized fingerprints in a later version. "
        "The final response must be only the JSON object required by the supplied schema."
    )


def _summarize_jsonl_events(payload: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    event_counts: dict[str, int] = {}
    tool_names: set[str] = set()
    for raw_line in payload.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        event_type = str(event.get("type") or "unknown")
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        item = event.get("item")
        if isinstance(item, dict):
            tool_name = item.get("name") or item.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                tool_names.add(tool_name[:160])
            elif isinstance(item.get("type"), str):
                fallback_tool_names = {
                    "command_execution": "command_execution",
                    "commandExecution": "command_execution",
                    "file_change": "file_change",
                    "fileChange": "file_change",
                    "image_view": "image_view",
                    "imageView": "image_view",
                    "mcp_tool_call": "mcp_tool_call",
                    "mcpToolCall": "mcp_tool_call",
                    "web_search": "web_search",
                    "webSearch": "web_search",
                }
                fallback = fallback_tool_names.get(str(item["type"]))
                if fallback:
                    tool_names.add(fallback)
    return (
        tuple(sorted(f"{name}:{count}" for name, count in event_counts.items())),
        tuple(sorted(tool_names)),
    )


def _safe_process_error(stderr: str, stdout: str = "") -> str:
    lowered = f"{stderr}\n{stdout}".lower()
    if "invalid_json_schema" in lowered:
        return "OUTPUT_SCHEMA_REJECTED"
    if "not logged in" in lowered or "unauthorized" in lowered:
        return "AUTH_REQUIRED"
    if "usage limit" in lowered or "rate limit" in lowered:
        return "USAGE_LIMITED"
    if "timed out" in lowered or "timeout" in lowered:
        return "RUNTIME_TIMEOUT"
    return "RUNTIME_FAILED"


def _codex_version(
    codex_binary: str,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    completed = command_runner(
        [codex_binary, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError("native Codex reference could not read the Codex version")
    return completed.stdout.strip()[:160]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_private_json(path: Path, payload: object, *, exclusive: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "NATIVE_CODEX_REFERENCE_RECEIPT_SCHEMA_VERSION",
    "NATIVE_CODEX_REFERENCE_RUNNER_VERSION",
    "NativeCodexReferenceReceiptV1",
    "NativeCodexReferenceRunResult",
    "run_native_codex_reference",
]
