from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from reqpilotx_server.adapters.agents.agent_kit_materializer import materialize_agent_kit
from reqpilotx_server.adapters.agents.codex_app_server.native_reference import (
    _codex_version,
    _safe_process_error,
    _summarize_jsonl_events,
    _write_private_json,
)
from reqpilotx_server.adapters.agents.portable_kit import load_portable_agent_kit
from reqpilotx_server.application.agent_workflow.runner_output_schema import (
    compile_strict_runner_output_schema,
)
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1,
    SourceItemTextScopeRequestV1,
    SourceItemTextSliceCandidateV1,
    SourceRequirementBaselineError,
    canonical_json_sha256,
    validate_source_item_text_slice_candidate,
)

NATIVE_SOURCE_ITEM_TEXT_RUNNER_VERSION = "1.1.0"
SOURCE_ITEM_TEXT_BATCH_CANDIDATE_SCHEMA_VERSION = "source-item-text-batch-candidate/v1"
NATIVE_SOURCE_ITEM_TEXT_RECEIPT_SCHEMA_VERSION = (
    "native-source-item-text-run-receipt/v1"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SourceItemTextBatchCandidateV1(_StrictFrozenModel):
    schema_version: str = SOURCE_ITEM_TEXT_BATCH_CANDIDATE_SCHEMA_VERSION
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidates: tuple[SourceItemTextSliceCandidateV1, ...] = Field(
        min_length=1, max_length=128
    )

    @model_validator(mode="after")
    def validate_batch(self) -> SourceItemTextBatchCandidateV1:
        scope_ids = tuple(item.scope_id for item in self.candidates)
        if len(scope_ids) != len(set(scope_ids)):
            raise ValueError("source-item text batch scopes must be unique")
        return self


class NativeSourceItemTextRunReceiptV1(_StrictFrozenModel):
    schema_version: str = NATIVE_SOURCE_ITEM_TEXT_RECEIPT_SCHEMA_VERSION
    runner_version: str = NATIVE_SOURCE_ITEM_TEXT_RUNNER_VERSION
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: str = Field(min_length=1, max_length=40)
    codex_version: str = Field(min_length=1, max_length=160)
    kit_version: str = Field(default="UNBOUND_LEGACY", min_length=1, max_length=80)
    kit_bundle_sha256: str = Field(default="0" * 64, pattern=r"^[0-9a-f]{64}$")
    visual_source_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    visual_page_numbers: tuple[StrictInt, ...] = Field(default=())
    elapsed_milliseconds: StrictInt = Field(ge=0)
    event_counts: tuple[str, ...]
    tool_names: tuple[str, ...]
    scope_count: StrictInt = Field(ge=1)
    scope_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    source_item_count: StrictInt = Field(ge=0)
    structure_only_span_count: StrictInt = Field(ge=0)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @model_validator(mode="after")
    def validate_receipt(self) -> NativeSourceItemTextRunReceiptV1:
        if self.scope_ids != tuple(sorted(set(self.scope_ids))):
            raise ValueError("native source-item text scope IDs must be sorted")
        if self.visual_page_numbers != tuple(
            sorted(set(self.visual_page_numbers))
        ) or any(page < 1 for page in self.visual_page_numbers):
            raise ValueError("native source-item text visual pages must be sorted")
        return self


class NativeSourceItemTextRunResult(_StrictFrozenModel):
    batch: SourceItemTextBatchCandidateV1
    run_receipt: NativeSourceItemTextRunReceiptV1


def run_native_source_item_text_reference(
    *,
    document_map: DocumentMapArtifactV1,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    output_directory: Path,
    codex_binary: str = "codex",
    model_id: str = "gpt-5.6-sol",
    reasoning_effort: str = "high",
    timeout_seconds: int = 1200,
    agent_kit_directory: Path | None = None,
    source_pdf_path: Path | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> NativeSourceItemTextRunResult:
    if not requests or len(requests) > 128:
        raise ValueError("native source-item text requests must be bounded")
    if timeout_seconds < 60 or timeout_seconds > 3600:
        raise ValueError("native source-item text timeout must be bounded")
    if reasoning_effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ValueError("native source-item text reasoning effort is invalid")
    scope_ids = tuple(item.scope_id for item in requests)
    if len(scope_ids) != len(set(scope_ids)):
        raise ValueError("native source-item text scopes must be unique")
    if any(
        item.document_map_artifact_id != document_map.artifact_id
        or item.document_map_artifact_sha256 != document_map.artifact_sha256
        or item.source_sha256 != document_map.source_sha256
        for item in requests
    ):
        raise ValueError("native source-item text document map lineage drifted")

    output = output_directory.expanduser().resolve()
    codex_version = _codex_version(codex_binary, command_runner)
    portable_kit = (
        load_portable_agent_kit(agent_kit_directory)
        if agent_kit_directory is not None
        else None
    )
    kit_version = (
        portable_kit.identity.kit_version
        if portable_kit is not None
        else "UNBOUND_LEGACY"
    )
    kit_bundle_sha256 = (
        portable_kit.identity.bundle_sha256 if portable_kit is not None else "0" * 64
    )
    source_pdf = (
        source_pdf_path.expanduser().resolve(strict=True)
        if source_pdf_path is not None
        else None
    )
    visual_source_sha256 = ""
    visual_page_numbers: tuple[int, ...] = ()
    if source_pdf is not None:
        if not source_pdf.is_file():
            raise ValueError("native source-item text visual source must be a file")
        with source_pdf.open("rb") as handle:
            visual_source_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
        if visual_source_sha256 != document_map.source_sha256:
            raise ValueError("native source-item text visual source hash drifted")
        visual_page_numbers = tuple(
            sorted(
                {
                    content.page
                    for request in requests
                    for content in request.source_content
                }
            )
        )
    request_set_sha256 = canonical_json_sha256(
        [item.model_dump(mode="json") for item in requests]
    )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="reqpilotx-native-source-item-text-"
    ) as raw_temp:
        temporary_root = Path(raw_temp).resolve()
        workspace = temporary_root / "workspace"
        if portable_kit is not None:
            materialize_agent_kit(
                portable_kit,
                workspace,
                runner_profile_id="desktop-reference",
            )
        else:
            workspace.mkdir(mode=0o700)
        os.chmod(workspace, 0o700)
        _write_private_json(
            workspace / "document-map.json",
            document_map.model_dump(mode="json"),
            exclusive=True,
        )
        if source_pdf is not None:
            visual_target = workspace / "source.pdf"
            shutil.copyfile(source_pdf, visual_target)
            os.chmod(visual_target, 0o400)
        _write_private_json(
            workspace / "text-scopes.json",
            [item.model_dump(mode="json") for item in requests],
            exclusive=True,
        )
        schema_path = workspace / "source-item-text-batch.schema.json"
        _write_private_json(
            schema_path,
            compile_strict_runner_output_schema(SourceItemTextBatchCandidateV1),
            exclusive=True,
        )
        stdout_values: list[str] = []
        batch: SourceItemTextBatchCandidateV1 | None = None
        last_error: Exception | None = None
        for attempt in (1, 2):
            final_path = workspace / f"source-item-text-batch-{attempt}.json"
            completed = command_runner(
                _build_command(
                    codex_binary=codex_binary,
                    model_id=model_id,
                    reasoning_effort=reasoning_effort,
                    workspace=workspace,
                    schema_path=schema_path,
                    final_path=final_path,
                ),
                input=_prompt(
                    document_map,
                    requests,
                    correction=attempt == 2,
                    kit_version=kit_version,
                    visual_page_numbers=visual_page_numbers,
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
            )
            stdout_values.append(completed.stdout)
            if completed.returncode != 0:
                raise RuntimeError(
                    "native Codex source-item text reference failed: "
                    + _runtime_failure_summary(completed.stderr, completed.stdout)
                )
            raw_batch = json.loads(final_path.read_text(encoding="utf-8"))
            try:
                batch = SourceItemTextBatchCandidateV1.model_validate(
                    _normalize_batch(raw_batch)
                )
                _validate_batch(
                    document_map=document_map, requests=requests, batch=batch
                )
                last_error = None
                break
            except (SourceRequirementBaselineError, ValidationError, ValueError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                _write_private_json(
                    workspace / "previous-batch.json",
                    raw_batch,
                    exclusive=True,
                )
                _write_private_json(
                    workspace / "validation-feedback.json",
                    _validation_feedback(exc, requests=requests),
                    exclusive=True,
                )
        if batch is None or last_error is not None:
            raise RuntimeError(
                "native Codex source-item text batch remained invalid: "
                + _safe_validation_error(last_error)
            )
        event_counts, tool_names = _summarize_jsonl_events("\n".join(stdout_values))

    candidate_sha256 = canonical_json_sha256(batch.model_dump(mode="json"))
    receipt = NativeSourceItemTextRunReceiptV1(
        source_sha256=document_map.source_sha256,
        document_map_artifact_id=document_map.artifact_id,
        document_map_artifact_sha256=document_map.artifact_sha256,
        request_set_sha256=request_set_sha256,
        model_id=model_id,
        reasoning_effort=reasoning_effort,
        codex_version=codex_version,
        kit_version=kit_version,
        kit_bundle_sha256=kit_bundle_sha256,
        visual_source_sha256=visual_source_sha256,
        visual_page_numbers=visual_page_numbers,
        elapsed_milliseconds=round((time.monotonic() - started) * 1000),
        event_counts=event_counts,
        tool_names=tool_names,
        scope_count=len(batch.candidates),
        scope_ids=tuple(sorted(item.scope_id for item in batch.candidates)),
        source_item_count=sum(len(item.items) for item in batch.candidates),
        structure_only_span_count=sum(
            len(item.structure_only_spans) for item in batch.candidates
        ),
        candidate_sha256=candidate_sha256,
    )
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    for filename, payload in (
        (
            "text-scope-requests.json",
            [item.model_dump(mode="json") for item in requests],
        ),
        ("candidate-batch.json", batch.model_dump(mode="json")),
        ("run-receipt.json", receipt.model_dump(mode="json")),
    ):
        _write_private_json(output / filename, payload, exclusive=True)
    return NativeSourceItemTextRunResult(batch=batch, run_receipt=receipt)


def _validate_batch(
    *,
    document_map: DocumentMapArtifactV1,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    batch: SourceItemTextBatchCandidateV1,
) -> None:
    if (
        batch.source_sha256 != document_map.source_sha256
        or batch.document_map_artifact_id != document_map.artifact_id
        or batch.document_map_artifact_sha256 != document_map.artifact_sha256
    ):
        raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_BATCH_LINEAGE_DRIFT")
    request_by_scope = {item.scope_id: item for item in requests}
    candidate_by_scope = {item.scope_id: item for item in batch.candidates}
    if set(request_by_scope) != set(candidate_by_scope):
        raise SourceRequirementBaselineError("SOURCE_ITEM_TEXT_BATCH_SCOPE_SET_INVALID")
    for scope_id, request in request_by_scope.items():
        validate_source_item_text_slice_candidate(request, candidate_by_scope[scope_id])


def _build_command(
    *,
    codex_binary: str,
    model_id: str,
    reasoning_effort: str,
    workspace: Path,
    schema_path: Path,
    final_path: Path,
) -> list[str]:
    return [
        codex_binary,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--json",
        "--color",
        "never",
        "--model",
        model_id,
        "--config",
        f'model_reasoning_effort="{reasoning_effort}"',
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


def _prompt(
    document_map: DocumentMapArtifactV1,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    *,
    correction: bool,
    kit_version: str,
    visual_page_numbers: tuple[int, ...],
) -> str:
    scope_summary = ", ".join(item.scope_id for item in requests)
    prompt = (
        "Use $reqpilotx-parent-provision-recognition. Read AGENTS.md and the Skill's required "
        "references, then read document-map.json and "
        "text-scopes.json. The accepted source-document hierarchy is immutable context. "
        "For every requested scope, disposition every non-whitespace source character "
        "exactly once as either one SourceItem text span or a structure_only title span. "
        "Use Unicode code-point half-open offsets into the exact content.text. Prefer the "
        "whole content block (start 0, end exact text length) when it is one source-faithful "
        "clause, paragraph, metadata block, or physical continuation; do not split by sentence "
        "or conjunction merely to make singular requirements. Group multiple content blocks "
        "only when they are one physical continuation or one complete source item. A pure title "
        "that exactly matches the node label/title belongs to structure_only_spans and must not "
        "be duplicated as a non-requirement item. Metadata, revision history, context, notes, "
        "definitions, rationale, examples and references remain SourceItems with A=NON_REQUIREMENT "
        "and the matching F role. Normative clauses receive A=REQUIREMENT and one or more sorted B "
        "values from CONSTRAINT, FUNCTIONAL, QUALITY. Never emit UNRESOLVED, OTHER_REQUIREMENT, "
        "technicality, rewritten text, singular children, or fixed-count splits. source_form is "
        "NUMBERED_CLAUSE for a genuinely numbered clause, NATIVE_LIST for an actual source list, "
        "and PARAGRAPH for unnumbered prose. label is concise "
        "source-visible wording; when a numbered node has a visible title, do not return only the "
        "ordinal. decision_summary is "
        "concise Chinese; reason_codes are unique sorted lowercase snake_case. Return one candidate "
        "for every scope and no other scope. Preserve source/document-map/request identities exactly. "
        f"Requested scopes: {scope_summary}. Source SHA: {document_map.source_sha256}. "
        f"The bound Agent Kit version is {kit_version}."
    )
    if visual_page_numbers:
        prompt += (
            " The exact authorized original is source.pdf. Use its rendered pages "
            f"{list(visual_page_numbers)} only when a checkmark, visual marker, layout, "
            "or incomplete extracted text changes the item boundary or classification; "
            "do not render pages as a fixed completion checklist."
        )
    if correction:
        prompt += (
            " This is the only bounded correction turn. Reread previous-batch.json, "
            "validation-feedback.json and the immutable scopes. Correct only schema, offsets, "
            "overlap, coverage, title-disposition, or scope-set defects. Return a complete batch, "
            "not a patch and not the previous invalid answer."
        )
    return prompt


def _validation_feedback(
    exc: Exception,
    *,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
) -> dict[str, object]:
    code = str(exc).strip()
    if isinstance(exc, ValidationError):
        failure_codes = sorted(
            {
                str(item.get("type") or "validation_error")
                for item in exc.errors(include_url=False)
            }
        )
        field_locations = sorted(
            {
                ".".join(str(value) for value in item.get("loc", ()))[:240]
                for item in exc.errors(include_url=False)
                if item.get("loc")
            }
        )
    else:
        failure_codes = [
            code
            if re.fullmatch(r"[A-Z][A-Z0-9_]{1,159}", code)
            else "SOURCE_ITEM_TEXT_CANDIDATE_INVALID"
        ]
        field_locations = []
    return {
        "schema_version": "source-item-text-validation-feedback/v1",
        "failure_codes": failure_codes,
        "field_locations": field_locations,
        "scope_content_lengths": {
            request.scope_id: {
                item.content_id: len(item.text) for item in request.source_content
            }
            for request in requests
        },
        "instruction": (
            "Use exact Unicode half-open offsets, cover every non-whitespace character once, "
            "and return one complete candidate for each immutable scope."
        ),
    }


def _normalize_batch(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    candidates = normalized.get("candidates")
    if isinstance(candidates, list):
        normalized["candidates"] = [_normalize_candidate(item) for item in candidates]
    return normalized


def _normalize_candidate(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    diagnostics = normalized.get("diagnostic_codes")
    if isinstance(diagnostics, list):
        normalized["diagnostic_codes"] = sorted(
            {item for item in diagnostics if isinstance(item, str)}
        )
    for key in ("items", "structure_only_spans"):
        values = normalized.get(key)
        if not isinstance(values, list):
            continue
        normalized[key] = [_normalize_reason_container(item) for item in values]
    return normalized


def _normalize_reason_container(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    reasons = normalized.get("reason_codes")
    if isinstance(reasons, list):
        normalized["reason_codes"] = sorted(
            {item for item in reasons if isinstance(item, str)}
        )
    kinds = normalized.get("requirement_kinds")
    if isinstance(kinds, list):
        normalized["requirement_kinds"] = sorted(
            {item for item in kinds if isinstance(item, str)}
        )
    return normalized


def _safe_validation_error(exc: Exception | None) -> str:
    if exc is None:
        return "SOURCE_ITEM_TEXT_CANDIDATE_INVALID"
    if isinstance(exc, SourceRequirementBaselineError):
        value = str(exc).strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,159}", value):
            return value
    if isinstance(exc, ValidationError):
        return "SOURCE_ITEM_TEXT_SCHEMA_OR_RANGE_INVALID"
    return "SOURCE_ITEM_TEXT_CANDIDATE_INVALID"


def _runtime_failure_summary(stderr: str, stdout: str) -> str:
    values: list[str] = []
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") not in {"turn.failed", "error"}:
            continue
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("code", "message", "type"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    values.append(value.strip()[:500])
        elif isinstance(error, str) and error.strip():
            values.append(error.strip()[:500])
    if values:
        return " | ".join(dict.fromkeys(values))[:1200]
    safe = _safe_process_error(stderr, stdout)
    return safe if safe else "RUNTIME_FAILED"


__all__ = [
    "NativeSourceItemTextRunReceiptV1",
    "NativeSourceItemTextRunResult",
    "SourceItemTextBatchCandidateV1",
    "run_native_source_item_text_reference",
]
