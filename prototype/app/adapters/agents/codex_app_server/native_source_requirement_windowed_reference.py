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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from app.adapters.agents.agent_kit_materializer import materialize_agent_kit
from app.adapters.agents.codex_app_server.native_reference import (
    _codex_version,
    _safe_process_error,
    _summarize_jsonl_events,
    _write_private_json,
)
from app.adapters.agents.codex_app_server.native_source_requirement_reference import (
    NATIVE_SOURCE_REQUIREMENT_REFERENCE_RUNNER_VERSION,
    NativeSourceRequirementRunReceiptV1,
    NativeSourceRequirementRunResult,
    _build_command,
    _compact_document_context,
    _compact_scope_evidence,
    _effective_reasoning_effort,
    _host_record_key,
    _reasoning_risk_signals,
)
from app.adapters.agents.portable_kit import PortableAgentKit, load_portable_agent_kit
from app.application.agent_workflow.runner_output_schema import (
    compile_strict_runner_output_schema,
)
from app.application.agent_workflow.source_requirement_baseline import (
    ContextRowRole,
    DocumentMapArtifactV1,
    NonRequirementRole,
    RequirementKind,
    Requirementness,
    SourceRecordForm,
    SourceRequirementBaselineError,
    SourceRequirementBoundaryCandidateV1,
    SourceRequirementContextRowGroupV1,
    SourceRequirementRowGroupV1,
    SourceRequirementScopeRequestV1,
    SourceRequirementSliceCandidateV1,
    canonical_json_sha256,
    validate_source_requirement_row_partition,
    validate_source_requirement_slice_candidate,
)

BOUNDARY_PLAN_SCHEMA_VERSION = "source-requirement-boundary-plan/v1"
BOUNDARY_AGENT_SCHEMA_VERSION = "source-requirement-boundary-agent-candidate/v1"
CLASSIFICATION_WINDOW_SCHEMA_VERSION = "source-requirement-classification-window/v1"
CLASSIFICATION_AGENT_SCHEMA_VERSION = (
    "source-requirement-classification-window-candidate/v1"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class _AgentRowGroupV1(_StrictFrozenModel):
    fragment_id: str = Field(min_length=1, max_length=200)
    row_start: StrictInt = Field(ge=0)
    row_count: StrictInt = Field(ge=1)


class _AgentContextRowGroupV1(_StrictFrozenModel):
    role: ContextRowRole
    row_group: _AgentRowGroupV1
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_machine_codes(value)


class _BoundaryAgentRecordV1(_StrictFrozenModel):
    label: str = Field(min_length=1, max_length=1000)
    source_form: SourceRecordForm
    row_groups: tuple[_AgentRowGroupV1, ...] = Field(min_length=1, max_length=512)
    shared_source_content_ids: tuple[str, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_table_form(self) -> _BoundaryAgentRecordV1:
        if self.source_form not in {
            SourceRecordForm.TABLE_RECORD,
            SourceRecordForm.TABLE_BRANCH,
        }:
            raise ValueError("table boundary plan requires a table source form")
        return self


class SourceRequirementBoundaryAgentCandidateV1(_StrictFrozenModel):
    schema_version: Literal["source-requirement-boundary-agent-candidate/v1"] = (
        BOUNDARY_AGENT_SCHEMA_VERSION
    )
    records: tuple[_BoundaryAgentRecordV1, ...] = Field(min_length=1, max_length=8192)
    context_row_groups: tuple[_AgentContextRowGroupV1, ...] = Field(
        default=(), max_length=1024
    )
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_machine_codes(value, allow_empty=True)


class SourceRequirementBoundaryPlanRecordV1(_StrictFrozenModel):
    record_key: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,199}$")
    label: str = Field(min_length=1, max_length=1000)
    source_form: SourceRecordForm
    row_groups: tuple[SourceRequirementRowGroupV1, ...] = Field(
        min_length=1, max_length=512
    )
    shared_source_content_ids: tuple[str, ...] = Field(default=(), max_length=256)


class SourceRequirementBoundaryPlanV1(_StrictFrozenModel):
    schema_version: Literal["source-requirement-boundary-plan/v1"] = (
        BOUNDARY_PLAN_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_table_scope_id: str = Field(min_length=1, max_length=200)
    records: tuple[SourceRequirementBoundaryPlanRecordV1, ...] = Field(
        min_length=1, max_length=8192
    )
    context_row_groups: tuple[SourceRequirementContextRowGroupV1, ...] = Field(
        default=(), max_length=1024
    )
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=64)
    boundary_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> SourceRequirementBoundaryPlanV1:
        keys = tuple(record.record_key for record in self.records)
        if len(keys) != len(set(keys)):
            raise ValueError("boundary-plan record keys must be unique")
        expected = canonical_json_sha256(
            self.model_dump(
                mode="json",
                exclude={"boundary_identity_sha256", "plan_sha256"},
            )
        )
        if self.plan_sha256 != expected:
            raise ValueError("boundary-plan SHA drifted")
        if self.boundary_identity_sha256 != _boundary_identity_sha256(self):
            raise ValueError("boundary semantic identity drifted")
        return self


class SourceRequirementClassificationDecisionV1(_StrictFrozenModel):
    record_key: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,199}$")
    requirementness: Requirementness
    requirement_kinds: tuple[RequirementKind, ...] = Field(default=(), max_length=5)
    non_requirement_role: NonRequirementRole | None = None
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    decision_summary: str = Field(min_length=1, max_length=1000)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("requirement_kinds")
    @classmethod
    def validate_kinds(
        cls, value: tuple[RequirementKind, ...]
    ) -> tuple[RequirementKind, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.value)):
            raise ValueError("classification kinds must be unique and sorted")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_machine_codes(value)

    @model_validator(mode="after")
    def validate_abf(self) -> SourceRequirementClassificationDecisionV1:
        if self.requirementness is Requirementness.REQUIREMENT:
            if not self.requirement_kinds or self.non_requirement_role is not None:
                raise ValueError("A=REQUIREMENT requires B and forbids F")
        elif self.requirement_kinds or self.non_requirement_role is None:
            raise ValueError("A=NON_REQUIREMENT requires F and forbids B")
        return self


class SourceRequirementClassificationAgentCandidateV1(_StrictFrozenModel):
    schema_version: Literal["source-requirement-classification-window-candidate/v1"] = (
        CLASSIFICATION_AGENT_SCHEMA_VERSION
    )
    decisions: tuple[SourceRequirementClassificationDecisionV1, ...] = Field(
        min_length=1, max_length=8192
    )
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_machine_codes(value, allow_empty=True)

    @model_validator(mode="after")
    def validate_candidate(self) -> SourceRequirementClassificationAgentCandidateV1:
        keys = tuple(item.record_key for item in self.decisions)
        if len(keys) != len(set(keys)):
            raise ValueError("classification-window record keys must be unique")
        return self


class SourceRequirementClassificationWindowV1(_StrictFrozenModel):
    schema_version: Literal["source-requirement-classification-window/v1"] = (
        CLASSIFICATION_WINDOW_SCHEMA_VERSION
    )
    window_id: str = Field(pattern=r"^window-[0-9]{2,}$")
    boundary_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    boundary_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_keys: tuple[str, ...] = Field(min_length=1, max_length=8192)
    estimated_weight: StrictInt = Field(ge=1)
    risk_signals: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _WindowRunResult:
    window: SourceRequirementClassificationWindowV1
    decisions: tuple[SourceRequirementClassificationDecisionV1, ...]
    stdout_values: tuple[str, ...]
    attempt_efforts: tuple[str, ...]
    raw_candidate_bytes: int
    elapsed_milliseconds: int
    input_payload: dict[str, object]
    diagnostic_codes: tuple[str, ...]


class NativeSourceRequirementWindowResultCheckpointV1(_StrictFrozenModel):
    """One Host-accepted classification window saved for exact resume."""

    schema_version: Literal["native-source-requirement-window-result-checkpoint/v1"] = (
        "native-source-requirement-window-result-checkpoint/v1"
    )
    window: SourceRequirementClassificationWindowV1
    decisions: tuple[SourceRequirementClassificationDecisionV1, ...] = Field(
        min_length=1,
        max_length=8192,
    )
    stdout_values: tuple[str, ...] = Field(default=(), max_length=2)
    attempt_efforts: tuple[str, ...] = Field(min_length=1, max_length=2)
    raw_candidate_bytes: StrictInt = Field(ge=1)
    elapsed_milliseconds: StrictInt = Field(ge=0)
    input_payload: dict[str, object]
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=128)
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_checkpoint(
        self,
    ) -> NativeSourceRequirementWindowResultCheckpointV1:
        if tuple(item.record_key for item in self.decisions) != self.window.record_keys:
            raise ValueError("classification result checkpoint record order drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"checkpoint_sha256"})
        )
        if self.checkpoint_sha256 != expected:
            raise ValueError("classification result checkpoint SHA drifted")
        return self


class NativeSourceRequirementWindowedCheckpointV1(_StrictFrozenModel):
    """Cumulative private checkpoint for one resumable Boundary Plan run."""

    schema_version: Literal["native-source-requirement-windowed-checkpoint/v1"] = (
        "native-source-requirement-windowed-checkpoint/v1"
    )
    content_bearing: Literal[True] = True
    private_object_store_only: Literal[True] = True
    runner_version: str = Field(min_length=1, max_length=80)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_table_scope_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=200)
    requested_reasoning_effort: str = Field(min_length=1, max_length=20)
    codex_version: str = Field(min_length=1, max_length=200)
    kit_version: str = Field(min_length=1, max_length=80)
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_source_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    visual_page_numbers: tuple[StrictInt, ...] = Field(default=(), max_length=4096)
    boundary_plan: SourceRequirementBoundaryPlanV1
    windows: tuple[SourceRequirementClassificationWindowV1, ...] = Field(
        min_length=1,
        max_length=4,
    )
    boundary_stdout: str = ""
    boundary_attempt_efforts: tuple[str, ...] = Field(min_length=1, max_length=2)
    boundary_raw_candidate_bytes: StrictInt = Field(ge=1)
    boundary_elapsed_milliseconds: StrictInt = Field(ge=0)
    classification_elapsed_milliseconds: StrictInt = Field(ge=0)
    accepted_windows: tuple[NativeSourceRequirementWindowResultCheckpointV1, ...] = ()
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_checkpoint(self) -> NativeSourceRequirementWindowedCheckpointV1:
        if (
            self.boundary_plan.source_sha256 != self.source_sha256
            or self.boundary_plan.document_map_artifact_id
            != self.document_map_artifact_id
            or self.boundary_plan.document_map_artifact_sha256
            != self.document_map_artifact_sha256
            or self.boundary_plan.request_sha256 != self.request_sha256
            or self.boundary_plan.logical_table_scope_id != self.logical_table_scope_id
        ):
            raise ValueError("windowed checkpoint boundary lineage drifted")
        window_ids = tuple(item.window_id for item in self.windows)
        if len(window_ids) != len(set(window_ids)):
            raise ValueError("windowed checkpoint window IDs must be unique")
        if any(
            item.boundary_identity_sha256 != self.boundary_plan.boundary_identity_sha256
            for item in self.windows
        ):
            raise ValueError("windowed checkpoint boundary identity drifted")
        expected_windows = {item.window_id: item for item in self.windows}
        accepted_ids = tuple(item.window.window_id for item in self.accepted_windows)
        if (
            len(accepted_ids) != len(set(accepted_ids))
            or accepted_ids
            != tuple(item for item in window_ids if item in set(accepted_ids))
            or any(
                expected_windows.get(item.window.window_id) != item.window
                for item in self.accepted_windows
            )
        ):
            raise ValueError("windowed checkpoint accepted window scope drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"checkpoint_sha256"})
        )
        if self.checkpoint_sha256 != expected:
            raise ValueError("windowed checkpoint SHA drifted")
        return self


WindowedCheckpointSink = Callable[[NativeSourceRequirementWindowedCheckpointV1], None]


def _window_result_checkpoint(
    result: _WindowRunResult,
) -> NativeSourceRequirementWindowResultCheckpointV1:
    values = {
        "window": result.window,
        "decisions": result.decisions,
        "stdout_values": result.stdout_values,
        "attempt_efforts": result.attempt_efforts,
        "raw_candidate_bytes": result.raw_candidate_bytes,
        "elapsed_milliseconds": result.elapsed_milliseconds,
        "input_payload": result.input_payload,
        "diagnostic_codes": result.diagnostic_codes,
    }
    draft = NativeSourceRequirementWindowResultCheckpointV1.model_construct(
        **values,
        checkpoint_sha256="0" * 64,
    )
    return NativeSourceRequirementWindowResultCheckpointV1(
        **values,
        checkpoint_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"checkpoint_sha256"})
        ),
    )


def _window_result_from_checkpoint(
    checkpoint: NativeSourceRequirementWindowResultCheckpointV1,
) -> _WindowRunResult:
    return _WindowRunResult(
        window=checkpoint.window,
        decisions=checkpoint.decisions,
        stdout_values=checkpoint.stdout_values,
        attempt_efforts=checkpoint.attempt_efforts,
        raw_candidate_bytes=checkpoint.raw_candidate_bytes,
        elapsed_milliseconds=checkpoint.elapsed_milliseconds,
        input_payload=checkpoint.input_payload,
        diagnostic_codes=checkpoint.diagnostic_codes,
    )


def _build_windowed_checkpoint(
    *,
    request: SourceRequirementScopeRequestV1,
    model_id: str,
    reasoning_effort: str,
    codex_version: str,
    kit_version: str,
    kit_bundle_sha256: str,
    visual_source_sha256: str,
    visual_page_numbers: tuple[int, ...],
    boundary_plan: SourceRequirementBoundaryPlanV1,
    windows: tuple[SourceRequirementClassificationWindowV1, ...],
    boundary_stdout: str,
    boundary_attempt_efforts: tuple[str, ...],
    boundary_raw_candidate_bytes: int,
    boundary_elapsed_milliseconds: int,
    classification_elapsed_milliseconds: int,
    window_results: dict[str, _WindowRunResult],
    runner_version: str = NATIVE_SOURCE_REQUIREMENT_REFERENCE_RUNNER_VERSION,
) -> NativeSourceRequirementWindowedCheckpointV1:
    accepted_windows = tuple(
        _window_result_checkpoint(window_results[item.window_id])
        for item in windows
        if item.window_id in window_results
    )
    values = {
        "runner_version": runner_version,
        "source_sha256": request.source_sha256,
        "document_map_artifact_id": request.document_map_artifact_id,
        "document_map_artifact_sha256": request.document_map_artifact_sha256,
        "request_sha256": request.request_sha256,
        "logical_table_scope_id": request.logical_table_scope_id,
        "model_id": model_id,
        "requested_reasoning_effort": reasoning_effort,
        "codex_version": codex_version,
        "kit_version": kit_version,
        "kit_bundle_sha256": kit_bundle_sha256,
        "visual_source_sha256": visual_source_sha256,
        "visual_page_numbers": visual_page_numbers,
        "boundary_plan": boundary_plan,
        "windows": windows,
        "boundary_stdout": boundary_stdout,
        "boundary_attempt_efforts": boundary_attempt_efforts,
        "boundary_raw_candidate_bytes": boundary_raw_candidate_bytes,
        "boundary_elapsed_milliseconds": boundary_elapsed_milliseconds,
        "classification_elapsed_milliseconds": (classification_elapsed_milliseconds),
        "accepted_windows": accepted_windows,
    }
    draft = NativeSourceRequirementWindowedCheckpointV1.model_construct(
        **values,
        checkpoint_sha256="0" * 64,
    )
    return NativeSourceRequirementWindowedCheckpointV1(
        **values,
        checkpoint_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"checkpoint_sha256"})
        ),
    )


def run_native_source_requirement_windowed_reference(
    *,
    document_map: DocumentMapArtifactV1,
    request: SourceRequirementScopeRequestV1,
    output_directory: Path,
    codex_binary: str = "codex",
    model_id: str = "gpt-5.6-sol",
    reasoning_effort: str = "high",
    timeout_seconds: int = 1200,
    max_concurrency: int = 2,
    agent_kit_directory: Path | None = None,
    source_pdf_path: Path | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    resume_checkpoint: NativeSourceRequirementWindowedCheckpointV1 | None = None,
    checkpoint_sink: WindowedCheckpointSink | None = None,
) -> NativeSourceRequirementRunResult:
    if timeout_seconds < 60 or timeout_seconds > 3600:
        raise ValueError("windowed source-requirement timeout must be bounded")
    if max_concurrency < 1 or max_concurrency > 4:
        raise ValueError("windowed source-requirement concurrency must be 1..4")
    if reasoning_effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ValueError("windowed source-requirement reasoning effort is invalid")
    if (
        request.document_map_artifact_id != document_map.artifact_id
        or request.document_map_artifact_sha256 != document_map.artifact_sha256
        or request.source_sha256 != document_map.source_sha256
    ):
        raise ValueError("windowed source-requirement document map lineage drifted")

    output = output_directory.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("windowed source-requirement output directory must be empty")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    codex_version = _codex_version(codex_binary, command_runner)
    source_pdf = _resolve_source_pdf(source_pdf_path, request=request)
    visual_source_sha256 = request.source_sha256 if source_pdf is not None else ""
    visual_page_numbers = (
        tuple(sorted({fragment.page for fragment in request.fragments}))
        if source_pdf is not None
        else ()
    )
    if resume_checkpoint is not None:
        _validate_resume_checkpoint_identity(
            resume_checkpoint,
            request=request,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
            codex_version=codex_version,
            kit_version=kit_version,
            kit_bundle_sha256=kit_bundle_sha256,
            visual_source_sha256=visual_source_sha256,
            visual_page_numbers=visual_page_numbers,
        )
    context_payload = _compact_document_context(document_map, request)
    scope_payload = _compact_scope_evidence(request)
    global_risk_signals = _reasoning_risk_signals(
        document_map=document_map,
        request=request,
    )
    boundary_effort, reasoning_selection = _effective_reasoning_effort(
        request=request,
        requested=reasoning_effort,
        execution_profile="compact-adaptive",
        risk_signals=global_risk_signals,
    )
    started = time.monotonic()
    resumed_elapsed_milliseconds = 0

    with tempfile.TemporaryDirectory(
        prefix="reqpilotx-source-requirement-windowed-"
    ) as raw_temp:
        temporary_root = Path(raw_temp).resolve()
        if resume_checkpoint is None:
            boundary_started = time.monotonic()
            (
                boundary_candidate,
                boundary_stdout,
                boundary_attempt_efforts,
                boundary_bytes,
            ) = _run_boundary_agent(
                workspace=temporary_root / "boundary",
                document_map=document_map,
                request=request,
                context_payload=context_payload,
                scope_payload=scope_payload,
                portable_kit=portable_kit,
                source_pdf=source_pdf,
                codex_binary=codex_binary,
                model_id=model_id,
                first_effort=boundary_effort,
                correction_effort=reasoning_effort,
                timeout_seconds=timeout_seconds,
                kit_version=kit_version,
                visual_page_numbers=visual_page_numbers,
                command_runner=command_runner,
            )
            boundary_elapsed = round((time.monotonic() - boundary_started) * 1000)
            boundary_plan = _compile_boundary_plan(
                document_map=document_map,
                request=request,
                candidate=boundary_candidate,
            )
            windows = _partition_boundary_plan(
                boundary_plan,
                request=request,
                document_map=document_map,
                max_concurrency=max_concurrency,
            )
            window_results: dict[str, _WindowRunResult] = {}
            prior_classification_elapsed = 0
        else:
            boundary_plan = resume_checkpoint.boundary_plan
            windows = resume_checkpoint.windows
            boundary_stdout = resume_checkpoint.boundary_stdout
            boundary_attempt_efforts = resume_checkpoint.boundary_attempt_efforts
            boundary_bytes = resume_checkpoint.boundary_raw_candidate_bytes
            boundary_elapsed = resume_checkpoint.boundary_elapsed_milliseconds
            prior_classification_elapsed = (
                resume_checkpoint.classification_elapsed_milliseconds
            )
            resumed_elapsed_milliseconds = (
                boundary_elapsed + prior_classification_elapsed
            )
            window_results = {
                item.window.window_id: _window_result_from_checkpoint(item)
                for item in resume_checkpoint.accepted_windows
            }
        _write_boundary_checkpoint(
            output=output,
            request=request,
            boundary_plan=boundary_plan,
            windows=windows,
            boundary_elapsed_milliseconds=boundary_elapsed,
            boundary_attempt_efforts=boundary_attempt_efforts,
        )
        for item in windows:
            if item.window_id in window_results:
                _write_window_checkpoint(
                    output=output,
                    result=window_results[item.window_id],
                )
        if resume_checkpoint is None and checkpoint_sink is not None:
            checkpoint_sink(
                _build_windowed_checkpoint(
                    request=request,
                    model_id=model_id,
                    reasoning_effort=reasoning_effort,
                    codex_version=codex_version,
                    kit_version=kit_version,
                    kit_bundle_sha256=kit_bundle_sha256,
                    visual_source_sha256=visual_source_sha256,
                    visual_page_numbers=visual_page_numbers,
                    boundary_plan=boundary_plan,
                    windows=windows,
                    boundary_stdout=boundary_stdout,
                    boundary_attempt_efforts=boundary_attempt_efforts,
                    boundary_raw_candidate_bytes=boundary_bytes,
                    boundary_elapsed_milliseconds=boundary_elapsed,
                    classification_elapsed_milliseconds=0,
                    window_results=window_results,
                )
            )
        pending_windows = tuple(
            item for item in windows if item.window_id not in window_results
        )
        classification_started = time.monotonic()
        window_failures: list[tuple[str, Exception]] = []
        if pending_windows:
            with ThreadPoolExecutor(
                max_workers=min(max_concurrency, len(pending_windows))
            ) as pool:
                futures = {
                    pool.submit(
                        _run_classification_window,
                        workspace=temporary_root / window.window_id,
                        window=window,
                        boundary_plan=boundary_plan,
                        document_map=document_map,
                        request=request,
                        context_payload=context_payload,
                        scope_payload=scope_payload,
                        portable_kit=portable_kit,
                        source_pdf=source_pdf,
                        codex_binary=codex_binary,
                        model_id=model_id,
                        requested_effort=reasoning_effort,
                        timeout_seconds=timeout_seconds,
                        kit_version=kit_version,
                        command_runner=command_runner,
                    ): window.window_id
                    for window in pending_windows
                }
                for future in as_completed(futures):
                    window_id = futures[future]
                    try:
                        result = future.result()
                        window_results[result.window.window_id] = result
                        _write_window_checkpoint(
                            output=output,
                            result=result,
                        )
                        if checkpoint_sink is not None:
                            checkpoint_sink(
                                _build_windowed_checkpoint(
                                    request=request,
                                    model_id=model_id,
                                    reasoning_effort=reasoning_effort,
                                    codex_version=codex_version,
                                    kit_version=kit_version,
                                    kit_bundle_sha256=kit_bundle_sha256,
                                    visual_source_sha256=visual_source_sha256,
                                    visual_page_numbers=visual_page_numbers,
                                    boundary_plan=boundary_plan,
                                    windows=windows,
                                    boundary_stdout=boundary_stdout,
                                    boundary_attempt_efforts=(boundary_attempt_efforts),
                                    boundary_raw_candidate_bytes=boundary_bytes,
                                    boundary_elapsed_milliseconds=boundary_elapsed,
                                    classification_elapsed_milliseconds=(
                                        prior_classification_elapsed
                                        + round(
                                            (time.monotonic() - classification_started)
                                            * 1000
                                        )
                                    ),
                                    window_results=window_results,
                                )
                            )
                    except Exception as exc:
                        window_failures.append((window_id, exc))
                        _write_window_failure_checkpoint(
                            output=output,
                            window_id=window_id,
                            workspace=temporary_root / window_id,
                            exc=exc,
                        )
        classification_elapsed = prior_classification_elapsed + round(
            (time.monotonic() - classification_started) * 1000
        )
        if window_failures:
            failed_ids = ",".join(sorted(item[0] for item in window_failures))
            raise RuntimeError(
                "native Codex classification windows remained invalid: " + failed_ids
            ) from window_failures[0][1]

    candidate = _aggregate_candidate(
        request=request,
        boundary_plan=boundary_plan,
        window_results=window_results,
    )
    validate_source_requirement_slice_candidate(request, candidate)
    candidate_sha256 = canonical_json_sha256(candidate.model_dump(mode="json"))
    ordered_results = tuple(window_results[item.window_id] for item in windows)
    stdout_values = (boundary_stdout,) + tuple(
        value for result in ordered_results for value in result.stdout_values
    )
    event_counts, tool_names = _summarize_jsonl_events("\n".join(stdout_values))
    attempt_efforts = boundary_attempt_efforts + tuple(
        effort for result in ordered_results for effort in result.attempt_efforts
    )
    raw_candidate_bytes = boundary_bytes + sum(
        result.raw_candidate_bytes for result in ordered_results
    )
    on_demand_resources = ["document-map.full.json", "scope.full.json"]
    if source_pdf is not None:
        on_demand_resources.append("source.pdf")
    run_receipt = NativeSourceRequirementRunReceiptV1(
        source_sha256=request.source_sha256,
        document_map_artifact_id=request.document_map_artifact_id,
        document_map_artifact_sha256=request.document_map_artifact_sha256,
        request_sha256=request.request_sha256,
        logical_table_scope_id=request.logical_table_scope_id,
        model_id=model_id,
        reasoning_effort=reasoning_effort,
        codex_version=codex_version,
        kit_version=kit_version,
        kit_bundle_sha256=kit_bundle_sha256,
        visual_source_sha256=visual_source_sha256,
        visual_page_numbers=visual_page_numbers,
        elapsed_milliseconds=(
            resumed_elapsed_milliseconds + round((time.monotonic() - started) * 1000)
        ),
        event_counts=event_counts,
        tool_names=tool_names,
        record_count=len(candidate.records),
        context_row_group_count=len(candidate.context_row_groups),
        host_assigned_machine_key_count=len(candidate.records),
        execution_profile="compact-adaptive",
        requested_reasoning_effort=reasoning_effort,
        attempt_reasoning_efforts=attempt_efforts,
        attempt_count=len(attempt_efforts),
        evidence_context_bytes=len(_canonical_json_bytes(context_payload))
        + len(_canonical_json_bytes(scope_payload)),
        output_schema_bytes=(
            len(_canonical_json_bytes(_boundary_output_schema()))
            + len(_canonical_json_bytes(_classification_output_schema()))
        ),
        raw_candidate_bytes=raw_candidate_bytes,
        evidence_delivery="INLINE_AND_FILES",
        initial_prompt_bytes=len(
            _boundary_prompt(
                inline_payload=_boundary_inline_payload(
                    context_payload=context_payload,
                    scope_payload=scope_payload,
                ),
                kit_version=kit_version,
                visual_page_numbers=visual_page_numbers,
                source_pdf_available=source_pdf is not None,
            ).encode("utf-8")
        ),
        on_demand_evidence_resources=tuple(sorted(on_demand_resources)),
        reasoning_selection=reasoning_selection,
        reasoning_risk_signals=global_risk_signals,
        execution_strategy="BOUNDARY_WINDOWS",
        boundary_plan_sha256=boundary_plan.plan_sha256,
        boundary_identity_sha256=boundary_plan.boundary_identity_sha256,
        boundary_elapsed_milliseconds=boundary_elapsed,
        classification_window_count=len(windows),
        classification_attempt_count=sum(
            len(result.attempt_efforts) for result in ordered_results
        ),
        classification_elapsed_milliseconds=classification_elapsed,
        candidate_sha256=candidate_sha256,
    )
    _write_windowed_final_output(
        output=output,
        candidate=candidate,
        receipt=run_receipt,
    )
    return NativeSourceRequirementRunResult(
        candidate=candidate,
        run_receipt=run_receipt,
    )


def _validate_resume_checkpoint_identity(
    checkpoint: NativeSourceRequirementWindowedCheckpointV1,
    *,
    request: SourceRequirementScopeRequestV1,
    model_id: str,
    reasoning_effort: str,
    codex_version: str,
    kit_version: str,
    kit_bundle_sha256: str,
    visual_source_sha256: str,
    visual_page_numbers: tuple[int, ...],
    runner_version: str = NATIVE_SOURCE_REQUIREMENT_REFERENCE_RUNNER_VERSION,
) -> None:
    observed = (
        checkpoint.runner_version,
        checkpoint.source_sha256,
        checkpoint.document_map_artifact_id,
        checkpoint.document_map_artifact_sha256,
        checkpoint.request_sha256,
        checkpoint.logical_table_scope_id,
        checkpoint.model_id,
        checkpoint.requested_reasoning_effort,
        checkpoint.codex_version,
        checkpoint.kit_version,
        checkpoint.kit_bundle_sha256,
        checkpoint.visual_source_sha256,
        checkpoint.visual_page_numbers,
    )
    expected = (
        runner_version,
        request.source_sha256,
        request.document_map_artifact_id,
        request.document_map_artifact_sha256,
        request.request_sha256,
        request.logical_table_scope_id,
        model_id,
        reasoning_effort,
        codex_version,
        kit_version,
        kit_bundle_sha256,
        visual_source_sha256,
        visual_page_numbers,
    )
    if observed != expected:
        raise ValueError("windowed source-requirement resume identity drifted")


def _resolve_source_pdf(
    source_pdf_path: Path | None,
    *,
    request: SourceRequirementScopeRequestV1,
) -> Path | None:
    if source_pdf_path is None:
        return None
    source_pdf = source_pdf_path.expanduser().resolve(strict=True)
    if not source_pdf.is_file():
        raise ValueError("windowed visual source must be a file")
    with source_pdf.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != request.source_sha256:
        raise ValueError("windowed visual source hash drifted")
    return source_pdf


def _prepare_workspace(
    *,
    workspace: Path,
    document_map: DocumentMapArtifactV1,
    request: SourceRequirementScopeRequestV1,
    context_payload: dict[str, object],
    scope_payload: dict[str, object],
    portable_kit: PortableAgentKit | None,
    source_pdf: Path | None,
    schema: dict[str, object],
    extra_payloads: tuple[tuple[str, object], ...],
) -> Path:
    if portable_kit is not None:
        materialize_agent_kit(
            portable_kit,
            workspace,
            runner_profile_id="desktop-reference",
        )
    else:
        workspace.mkdir(parents=True, mode=0o700)
    os.chmod(workspace, 0o700)
    for filename, payload, read_only in (
        ("document-context.json", context_payload, False),
        ("scope.json", scope_payload, False),
        ("document-map.full.json", document_map.model_dump(mode="json"), True),
        ("scope.full.json", request.model_dump(mode="json"), True),
    ):
        path = workspace / filename
        _write_private_json(path, payload, exclusive=True)
        if read_only:
            os.chmod(path, 0o400)
    for filename, payload in extra_payloads:
        path = workspace / filename
        _write_private_json(path, payload, exclusive=True)
        os.chmod(path, 0o400)
    if source_pdf is not None:
        target = workspace / "source.pdf"
        shutil.copyfile(source_pdf, target)
        os.chmod(target, 0o400)
    schema_path = workspace / "candidate.schema.json"
    _write_private_json(schema_path, schema, exclusive=True)
    return schema_path


def _run_boundary_agent(
    *,
    workspace: Path,
    document_map: DocumentMapArtifactV1,
    request: SourceRequirementScopeRequestV1,
    context_payload: dict[str, object],
    scope_payload: dict[str, object],
    portable_kit: PortableAgentKit | None,
    source_pdf: Path | None,
    codex_binary: str,
    model_id: str,
    first_effort: str,
    correction_effort: str,
    timeout_seconds: int,
    kit_version: str,
    visual_page_numbers: tuple[int, ...],
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[
    SourceRequirementBoundaryAgentCandidateV1,
    str,
    tuple[str, ...],
    int,
]:
    schema_path = _prepare_workspace(
        workspace=workspace,
        document_map=document_map,
        request=request,
        context_payload=context_payload,
        scope_payload=scope_payload,
        portable_kit=portable_kit,
        source_pdf=source_pdf,
        schema=_boundary_output_schema(),
        extra_payloads=(),
    )
    inline_payload = _boundary_inline_payload(
        context_payload=context_payload,
        scope_payload=scope_payload,
    )
    stdout_values: list[str] = []
    efforts: list[str] = []
    raw_bytes = 0
    last_error: Exception | None = None
    for attempt in (1, 2):
        effort = first_effort if attempt == 1 else correction_effort
        efforts.append(effort)
        final_path = workspace / f"boundary-candidate-{attempt}.json"
        command = _build_command(
            codex_binary=codex_binary,
            model_id=model_id,
            reasoning_effort=effort,
            workspace=workspace,
            schema_path=schema_path,
            final_path=final_path,
        )
        prompt = (
            _boundary_prompt(
                inline_payload=inline_payload,
                kit_version=kit_version,
                visual_page_numbers=visual_page_numbers,
                source_pdf_available=source_pdf is not None,
            )
            if attempt == 1
            else _boundary_correction_prompt(
                kit_version=kit_version,
                visual_page_numbers=visual_page_numbers,
                source_pdf_available=source_pdf is not None,
            )
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
        stdout_values.append(completed.stdout)
        if completed.returncode != 0:
            raise RuntimeError(
                "native Codex boundary plan failed: "
                + _safe_process_error(completed.stderr, completed.stdout)
            )
        raw_bytes += final_path.stat().st_size
        raw = _normalize_boundary_payload(
            json.loads(final_path.read_text(encoding="utf-8"))
        )
        try:
            candidate = SourceRequirementBoundaryAgentCandidateV1.model_validate(raw)
            _compile_boundary_plan(
                document_map=document_map,
                request=request,
                candidate=candidate,
            )
            return candidate, "\n".join(stdout_values), tuple(efforts), raw_bytes
        except (ValidationError, ValueError, SourceRequirementBaselineError) as exc:
            last_error = exc
            if attempt == 2:
                break
            _write_private_json(
                workspace / "previous-candidate.json", raw, exclusive=True
            )
            _write_private_json(
                workspace / "validation-feedback.json",
                _boundary_validation_feedback(exc, request=request),
                exclusive=True,
            )
    raise RuntimeError(
        "native Codex boundary plan remained invalid: "
        + _safe_windowed_validation_error(last_error)
    )


def _compile_boundary_plan(
    *,
    document_map: DocumentMapArtifactV1,
    request: SourceRequirementScopeRequestV1,
    candidate: SourceRequirementBoundaryAgentCandidateV1,
) -> SourceRequirementBoundaryPlanV1:
    fragment_order = {
        fragment.fragment_id: index for index, fragment in enumerate(request.fragments)
    }

    def canonical_groups(
        groups: tuple[_AgentRowGroupV1, ...],
    ) -> tuple[SourceRequirementRowGroupV1, ...]:
        return tuple(
            sorted(
                (
                    SourceRequirementRowGroupV1(
                        fragment_id=group.fragment_id,
                        row_start=group.row_start,
                        row_end=group.row_start + group.row_count,
                    )
                    for group in groups
                ),
                key=lambda group: (
                    fragment_order.get(group.fragment_id, len(fragment_order)),
                    group.row_start,
                    group.row_end,
                ),
            )
        )

    available_content_ids = {item.content_id for item in request.source_content}
    reserved_keys: set[str] = set()
    records: list[SourceRequirementBoundaryPlanRecordV1] = []
    for item in candidate.records:
        groups = canonical_groups(item.row_groups)
        if not set(item.shared_source_content_ids).issubset(available_content_ids):
            raise SourceRequirementBaselineError(
                "SOURCE_REQUIREMENT_SHARED_CONTENT_UNKNOWN"
            )
        key = _host_record_key(
            request=request,
            row_groups=groups,
            reserved_keys=reserved_keys,
        )
        reserved_keys.add(key)
        records.append(
            SourceRequirementBoundaryPlanRecordV1(
                record_key=key,
                label=item.label,
                source_form=item.source_form,
                row_groups=groups,
                shared_source_content_ids=tuple(
                    dict.fromkeys(item.shared_source_content_ids)
                ),
            )
        )
    records.sort(
        key=lambda item: (
            fragment_order[item.row_groups[0].fragment_id],
            item.row_groups[0].row_start,
            item.row_groups[0].row_end,
            item.record_key,
        )
    )
    context_groups = _merge_adjacent_context_groups(
        tuple(
            sorted(
                (
                    SourceRequirementContextRowGroupV1(
                        role=item.role,
                        row_group=SourceRequirementRowGroupV1(
                            fragment_id=item.row_group.fragment_id,
                            row_start=item.row_group.row_start,
                            row_end=(
                                item.row_group.row_start + item.row_group.row_count
                            ),
                        ),
                        reason_codes=tuple(sorted(set(item.reason_codes))),
                    )
                    for item in candidate.context_row_groups
                ),
                key=lambda item: (
                    fragment_order[item.row_group.fragment_id],
                    item.row_group.row_start,
                    item.row_group.row_end,
                    item.role.value,
                ),
            )
        )
    )
    validate_source_requirement_row_partition(
        request,
        record_row_groups=tuple(item.row_groups for item in records),
        context_row_groups=context_groups,
    )
    values = {
        "source_sha256": request.source_sha256,
        "document_map_artifact_id": request.document_map_artifact_id,
        "document_map_artifact_sha256": request.document_map_artifact_sha256,
        "request_sha256": request.request_sha256,
        "logical_table_scope_id": request.logical_table_scope_id,
        "records": tuple(records),
        "context_row_groups": context_groups,
        "diagnostic_codes": tuple(sorted(set(candidate.diagnostic_codes))),
    }
    draft = SourceRequirementBoundaryPlanV1.model_construct(
        **values,
        boundary_identity_sha256="0" * 64,
        plan_sha256="0" * 64,
    )
    digest = canonical_json_sha256(
        draft.model_dump(
            mode="json",
            exclude={"boundary_identity_sha256", "plan_sha256"},
        )
    )
    boundary_identity_sha256 = _boundary_identity_sha256(draft)
    return SourceRequirementBoundaryPlanV1(
        **values,
        boundary_identity_sha256=boundary_identity_sha256,
        plan_sha256=digest,
    )


def _partition_boundary_plan(
    boundary_plan: SourceRequirementBoundaryPlanV1,
    *,
    request: SourceRequirementScopeRequestV1,
    document_map: DocumentMapArtifactV1,
    max_concurrency: int,
) -> tuple[SourceRequirementClassificationWindowV1, ...]:
    record_weights = tuple(
        _record_weight(record, request=request) for record in boundary_plan.records
    )
    desired_windows = min(max_concurrency, len(boundary_plan.records))
    groups: list[list[int]] = []
    start = 0
    remaining_weight = sum(record_weights)
    for window_index in range(desired_windows):
        remaining_windows = desired_windows - window_index
        if remaining_windows == 1:
            groups.append(list(range(start, len(record_weights))))
            break
        target = remaining_weight / remaining_windows
        current: list[int] = []
        current_weight = 0
        while start < len(record_weights):
            remaining_records = len(record_weights) - start
            if current and remaining_records == remaining_windows - 1:
                break
            current.append(start)
            current_weight += record_weights[start]
            start += 1
            if current_weight >= target:
                break
        groups.append(current)
        remaining_weight -= current_weight
    windows = []
    for index, record_indices in enumerate(groups, start=1):
        records = tuple(boundary_plan.records[item] for item in record_indices)
        risk_signals = _classification_window_risk_signals(
            records=records,
            request=request,
            document_map=document_map,
        )
        windows.append(
            SourceRequirementClassificationWindowV1(
                window_id=f"window-{index:02d}",
                boundary_plan_sha256=boundary_plan.plan_sha256,
                boundary_identity_sha256=boundary_plan.boundary_identity_sha256,
                record_keys=tuple(item.record_key for item in records),
                estimated_weight=sum(record_weights[item] for item in record_indices),
                risk_signals=risk_signals,
            )
        )
    return tuple(windows)


def _record_weight(
    record: SourceRequirementBoundaryPlanRecordV1,
    *,
    request: SourceRequirementScopeRequestV1,
) -> int:
    fragment_by_id = {item.fragment_id: item for item in request.fragments}
    cells = set()
    character_count = 0
    for group in record.row_groups:
        fragment = fragment_by_id[group.fragment_id]
        for cell in fragment.cells:
            if cell.row < group.row_end and cell.row + cell.row_span > group.row_start:
                cells.add(cell.cell_id)
                character_count += len(cell.text)
    return max(1, len(cells) + character_count // 160)


def _classification_window_risk_signals(
    *,
    records: tuple[SourceRequirementBoundaryPlanRecordV1, ...],
    request: SourceRequirementScopeRequestV1,
    document_map: DocumentMapArtifactV1,
) -> tuple[str, ...]:
    selected_rows = {
        (group.fragment_id, row)
        for record in records
        for group in record.row_groups
        for row in range(group.row_start, group.row_end)
    }
    signals: set[str] = set()
    if document_map.evidence_state != "SUFFICIENT":
        signals.add("document_evidence_not_sufficient")
    table = next(
        item
        for item in document_map.logical_tables
        if item.scope_id == request.logical_table_scope_id
    )
    if table.capture_gap:
        signals.add("capture_gap")
    if any(not item.precision.startswith("EXACT") for item in request.source_content):
        signals.add("non_exact_source_content")
    if any(len(record.row_groups) > 1 for record in records):
        signals.add("cross_fragment_record")
    if any(
        cell.row_span > 1
        and any(
            (fragment.fragment_id, row) in selected_rows
            for row in range(cell.row, cell.row + cell.row_span)
        )
        for fragment in request.fragments
        for cell in fragment.cells
    ):
        signals.add("row_spanning_cell")
    owner = next(item for item in document_map.nodes if item.node_id == request.node_id)
    for anchor in owner.source_anchors:
        if anchor.logical_table_scope_id not in {"", request.logical_table_scope_id}:
            continue
        if not (anchor.asset_id or anchor.placement_id):
            continue
        if anchor.row_range is None:
            signals.add("visual_asset_evidence")
            break
        fragment_ids = {
            fragment.fragment_id
            for fragment in request.fragments
            if fragment.table_token == anchor.table_token
        }
        if any(
            (fragment_id, row) in selected_rows
            for fragment_id in fragment_ids
            for row in range(anchor.row_range[0], anchor.row_range[1])
        ):
            signals.add("visual_asset_evidence")
            break
    return tuple(sorted(signals))


def _run_classification_window(
    *,
    workspace: Path,
    window: SourceRequirementClassificationWindowV1,
    boundary_plan: SourceRequirementBoundaryPlanV1,
    document_map: DocumentMapArtifactV1,
    request: SourceRequirementScopeRequestV1,
    context_payload: dict[str, object],
    scope_payload: dict[str, object],
    portable_kit: PortableAgentKit | None,
    source_pdf: Path | None,
    codex_binary: str,
    model_id: str,
    requested_effort: str,
    timeout_seconds: int,
    kit_version: str,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> _WindowRunResult:
    started = time.monotonic()
    input_payload = _classification_window_payload(
        window=window,
        boundary_plan=boundary_plan,
        request=request,
        context_payload=context_payload,
    )
    schema_path = _prepare_workspace(
        workspace=workspace,
        document_map=document_map,
        request=request,
        context_payload=context_payload,
        scope_payload=scope_payload,
        portable_kit=portable_kit,
        source_pdf=source_pdf,
        schema=_classification_output_schema(),
        extra_payloads=(
            ("boundary-plan.full.json", boundary_plan.model_dump(mode="json")),
            ("classification-window.json", input_payload),
        ),
    )
    first_effort = (
        requested_effort
        if window.risk_signals
        else (
            "medium"
            if requested_effort in {"high", "xhigh", "max", "ultra"}
            else requested_effort
        )
    )
    stdout_values: list[str] = []
    efforts: list[str] = []
    raw_bytes = 0
    last_error: Exception | None = None
    for attempt in (1, 2):
        effort = first_effort if attempt == 1 else requested_effort
        efforts.append(effort)
        final_path = workspace / f"classification-candidate-{attempt}.json"
        command = _build_command(
            codex_binary=codex_binary,
            model_id=model_id,
            reasoning_effort=effort,
            workspace=workspace,
            schema_path=schema_path,
            final_path=final_path,
        )
        prompt = (
            _classification_prompt(
                window=window,
                inline_payload=input_payload,
                kit_version=kit_version,
                source_pdf_available=source_pdf is not None,
            )
            if attempt == 1
            else _classification_correction_prompt(
                window=window,
                kit_version=kit_version,
                source_pdf_available=source_pdf is not None,
            )
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
        stdout_values.append(completed.stdout)
        if completed.returncode != 0:
            raise RuntimeError(
                f"native Codex classification {window.window_id} failed: "
                + _safe_process_error(completed.stderr, completed.stdout)
            )
        raw_bytes += final_path.stat().st_size
        raw = _normalize_classification_payload(
            json.loads(final_path.read_text(encoding="utf-8"))
        )
        try:
            candidate = SourceRequirementClassificationAgentCandidateV1.model_validate(
                raw
            )
            expected = set(window.record_keys)
            actual = {item.record_key for item in candidate.decisions}
            if actual != expected:
                raise ValueError("CLASSIFICATION_WINDOW_KEY_SET_INVALID")
            return _WindowRunResult(
                window=window,
                decisions=tuple(
                    sorted(
                        candidate.decisions,
                        key=lambda item: window.record_keys.index(item.record_key),
                    )
                ),
                stdout_values=tuple(stdout_values),
                attempt_efforts=tuple(efforts),
                raw_candidate_bytes=raw_bytes,
                elapsed_milliseconds=round((time.monotonic() - started) * 1000),
                input_payload=input_payload,
                diagnostic_codes=tuple(sorted(set(candidate.diagnostic_codes))),
            )
        except (ValidationError, ValueError) as exc:
            last_error = exc
            if attempt == 2:
                break
            _write_private_json(
                workspace / "previous-candidate.json", raw, exclusive=True
            )
            _write_private_json(
                workspace / "validation-feedback.json",
                {
                    "schema_version": "classification-window-feedback/v1",
                    "window_id": window.window_id,
                    "expected_record_keys": window.record_keys,
                    "failure_code": _safe_windowed_validation_error(exc),
                    "instruction": (
                        "Return exactly one corrected A/B/F decision for every expected "
                        "record key. Do not change boundaries or labels."
                    ),
                },
                exclusive=True,
            )
    raise RuntimeError(
        f"native Codex classification {window.window_id} remained invalid: "
        + _safe_windowed_validation_error(last_error)
    )


def _classification_window_payload(
    *,
    window: SourceRequirementClassificationWindowV1,
    boundary_plan: SourceRequirementBoundaryPlanV1,
    request: SourceRequirementScopeRequestV1,
    context_payload: dict[str, object],
) -> dict[str, object]:
    record_by_key = {item.record_key: item for item in boundary_plan.records}
    fragment_by_id = {item.fragment_id: item for item in request.fragments}

    def cells_for(
        record: SourceRequirementBoundaryPlanRecordV1,
    ) -> list[dict[str, object]]:
        values: dict[tuple[str, str], dict[str, object]] = {}
        for group in record.row_groups:
            fragment = fragment_by_id[group.fragment_id]
            for cell in fragment.cells:
                if (
                    cell.row < group.row_end
                    and cell.row + cell.row_span > group.row_start
                ):
                    values[(fragment.fragment_id, cell.cell_id)] = {
                        "fragment_id": fragment.fragment_id,
                        "page": fragment.page,
                        "row": cell.row,
                        "column": cell.column,
                        "row_span": cell.row_span,
                        "column_span": cell.column_span,
                        "text": cell.text,
                    }
        return list(values.values())

    records = []
    for key in window.record_keys:
        record = record_by_key[key]
        records.append(
            {
                "record_key": record.record_key,
                "label": record.label,
                "source_form": record.source_form.value,
                "row_groups": [
                    item.model_dump(mode="json") for item in record.row_groups
                ],
                "shared_source_content_ids": record.shared_source_content_ids,
                "cells": cells_for(record),
            }
        )
    return {
        "schema_version": CLASSIFICATION_WINDOW_SCHEMA_VERSION,
        "window": window.model_dump(mode="json"),
        "document_context": context_payload,
        "source_content": [
            {
                "content_id": item.content_id,
                "page": item.page,
                "text": item.text,
                "precision": item.precision,
                "is_title": item.is_title,
            }
            for item in request.source_content
        ],
        "records": records,
        "shared_context_row_groups": [
            item.model_dump(mode="json") for item in boundary_plan.context_row_groups
        ],
    }


def _aggregate_candidate(
    *,
    request: SourceRequirementScopeRequestV1,
    boundary_plan: SourceRequirementBoundaryPlanV1,
    window_results: dict[str, _WindowRunResult],
) -> SourceRequirementSliceCandidateV1:
    decisions = {
        item.record_key: item
        for result in window_results.values()
        for item in result.decisions
    }
    expected = {item.record_key for item in boundary_plan.records}
    if set(decisions) != expected:
        raise ValueError("CLASSIFICATION_AGGREGATE_KEY_SET_INVALID")
    records = tuple(
        SourceRequirementBoundaryCandidateV1(
            record_key=boundary.record_key,
            label=boundary.label,
            source_form=boundary.source_form,
            row_groups=boundary.row_groups,
            shared_source_content_ids=boundary.shared_source_content_ids,
            requirementness=decisions[boundary.record_key].requirementness,
            requirement_kinds=decisions[boundary.record_key].requirement_kinds,
            non_requirement_role=decisions[boundary.record_key].non_requirement_role,
            confidence=decisions[boundary.record_key].confidence,
            decision_summary=decisions[boundary.record_key].decision_summary,
            reason_codes=decisions[boundary.record_key].reason_codes,
        )
        for boundary in boundary_plan.records
    )
    return SourceRequirementSliceCandidateV1(
        source_sha256=request.source_sha256,
        document_map_artifact_id=request.document_map_artifact_id,
        document_map_artifact_sha256=request.document_map_artifact_sha256,
        request_sha256=request.request_sha256,
        logical_table_scope_id=request.logical_table_scope_id,
        records=records,
        context_row_groups=boundary_plan.context_row_groups,
        diagnostic_codes=tuple(
            sorted(
                set(boundary_plan.diagnostic_codes).union(
                    *(
                        set(result.diagnostic_codes)
                        for result in window_results.values()
                    )
                )
            )
        ),
    )


def _write_boundary_checkpoint(
    *,
    output: Path,
    request: SourceRequirementScopeRequestV1,
    boundary_plan: SourceRequirementBoundaryPlanV1,
    windows: tuple[SourceRequirementClassificationWindowV1, ...],
    boundary_elapsed_milliseconds: int,
    boundary_attempt_efforts: tuple[str, ...],
) -> None:
    os.chmod(output, 0o700)
    for filename, payload in (
        ("scope-request.json", request.model_dump(mode="json")),
        ("boundary-plan.json", boundary_plan.model_dump(mode="json")),
        (
            "classification-windows.json",
            [item.model_dump(mode="json") for item in windows],
        ),
        (
            "boundary-receipt.json",
            {
                "schema_version": "boundary-plan-run-receipt/v1",
                "plan_sha256": boundary_plan.plan_sha256,
                "boundary_identity_sha256": (boundary_plan.boundary_identity_sha256),
                "attempt_efforts": boundary_attempt_efforts,
                "elapsed_milliseconds": boundary_elapsed_milliseconds,
            },
        ),
    ):
        _write_private_json(output / filename, payload, exclusive=True)


def _write_window_checkpoint(
    *,
    output: Path,
    result: _WindowRunResult,
) -> None:
    window_output = output / "windows" / result.window.window_id
    window_output.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private_json(
        window_output / "input.json", result.input_payload, exclusive=True
    )
    _write_private_json(
        window_output / "candidate.json",
        {
            "schema_version": CLASSIFICATION_AGENT_SCHEMA_VERSION,
            "decisions": [item.model_dump(mode="json") for item in result.decisions],
            "diagnostic_codes": result.diagnostic_codes,
        },
        exclusive=True,
    )
    _write_private_json(
        window_output / "receipt.json",
        {
            "schema_version": "classification-window-run-receipt/v1",
            "window": result.window.model_dump(mode="json"),
            "attempt_efforts": result.attempt_efforts,
            "elapsed_milliseconds": result.elapsed_milliseconds,
            "raw_candidate_bytes": result.raw_candidate_bytes,
        },
        exclusive=True,
    )


def _write_window_failure_checkpoint(
    *,
    output: Path,
    window_id: str,
    workspace: Path,
    exc: Exception,
) -> None:
    window_output = output / "windows" / window_id
    window_output.mkdir(parents=True, exist_ok=True, mode=0o700)
    for source_name, target_name in (
        ("classification-window.json", "input.json"),
        ("classification-candidate-1.json", "failed-candidate-1.json"),
        ("classification-candidate-2.json", "failed-candidate-2.json"),
        ("validation-feedback.json", "validation-feedback.json"),
    ):
        source = workspace / source_name
        if not source.is_file():
            continue
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        _write_private_json(window_output / target_name, payload, exclusive=True)
    _write_private_json(
        window_output / "failure.json",
        {
            "schema_version": "classification-window-failure/v1",
            "window_id": window_id,
            "failure_code": _safe_windowed_validation_error(exc),
            "error_type": type(exc).__name__,
        },
        exclusive=True,
    )


def _write_windowed_final_output(
    *,
    output: Path,
    candidate: SourceRequirementSliceCandidateV1,
    receipt: NativeSourceRequirementRunReceiptV1,
) -> None:
    for filename, payload in (
        ("candidate.json", candidate.model_dump(mode="json")),
        ("run-receipt.json", receipt.model_dump(mode="json")),
    ):
        _write_private_json(output / filename, payload, exclusive=True)


def _boundary_output_schema() -> dict[str, object]:
    return compile_strict_runner_output_schema(
        SourceRequirementBoundaryAgentCandidateV1
    )


def _classification_output_schema() -> dict[str, object]:
    return compile_strict_runner_output_schema(
        SourceRequirementClassificationAgentCandidateV1
    )


def _boundary_inline_payload(
    *,
    context_payload: dict[str, object],
    scope_payload: dict[str, object],
) -> dict[str, object]:
    return {
        "document_context": context_payload,
        "scope": scope_payload,
    }


def _boundary_prompt(
    *,
    inline_payload: dict[str, object],
    kit_version: str,
    visual_page_numbers: tuple[int, ...],
    source_pdf_available: bool,
) -> str:
    pages = list(visual_page_numbers)
    visual_instruction = (
        " source.pdf is the complete authorized original and remains read-only on demand. "
        f"Inspect visual evidence when it affects boundaries; relevant pages are {pages}."
        if source_pdf_available
        else " No original PDF was supplied to this reference run; do not claim visual review."
    )
    return (
        "Use $reqpilotx-parent-provision-recognition for "
        "execution_mode=SOURCE_REQUIREMENT_BOUNDARY_PLAN. Decide only complete business "
        "record boundaries for this logical table; do not classify A/B/F yet. Assign every "
        "row exactly once to one record or typed context group. DVP tests keep complete "
        "numbered tests and continuations; BOM tables keep product branches. Each row_group "
        "uses fragment_id, local zero-based row_start and positive row_count. Preserve source "
        "numbering and a concise source-visible label. Full document-map.full.json, "
        "scope.full.json are read-only and available on demand; global context is readable but "
        "selectable rows remain this scope."
        + visual_instruction
        + f" Kit version is {kit_version}. "
        "Return only schema-valid JSON.\nBEGIN_UNTRUSTED_SOURCE_EVIDENCE_JSON\n"
        + json.dumps(
            inline_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\nEND_UNTRUSTED_SOURCE_EVIDENCE_JSON"
    )


def _boundary_correction_prompt(
    *,
    kit_version: str,
    visual_page_numbers: tuple[int, ...],
    source_pdf_available: bool,
) -> str:
    visual_instruction = (
        f" Relevant visual pages are {list(visual_page_numbers)}."
        if source_pdf_available
        else " No original PDF is available; do not claim visual review."
    )
    return (
        "Use $reqpilotx-parent-provision-recognition for "
        "execution_mode=SOURCE_REQUIREMENT_BOUNDARY_PLAN. Read scope.json, "
        "previous-candidate.json and validation-feedback.json. Return one complete corrected "
        "boundary plan using row_start + positive row_count, exact row partition and no A/B/F. "
        + visual_instruction
        + f" Kit version is {kit_version}. "
        "Return only schema-valid JSON."
    )


def _classification_prompt(
    *,
    window: SourceRequirementClassificationWindowV1,
    inline_payload: dict[str, object],
    kit_version: str,
    source_pdf_available: bool,
) -> str:
    evidence_resources = (
        "boundary-plan.full.json, document-map.full.json, scope.full.json and source.pdf"
        if source_pdf_available
        else "boundary-plan.full.json, document-map.full.json and scope.full.json"
    )
    return (
        "Use $reqpilotx-parent-provision-recognition for "
        "execution_mode=SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW. The Host-validated "
        "Boundary Plan is immutable: do not merge, split, relabel or reorder records. Return "
        "exactly one A/B/F decision for every listed record_key and no other key. A requirement "
        "needs a sorted subset of FUNCTIONAL/QUALITY/CONSTRAINT/PROJECT/PROCESS kinds "
        "and null F. FUNCTIONAL/QUALITY/CONSTRAINT belong to product/system requirements; "
        "PROJECT covers project deliverables and milestones, PROCESS covers activities "
        "and responsibilities. Topics and verification relations are independent; standards "
        "or test methods do not automatically mean CONSTRAINT. A non-requirement needs "
        "empty B and one F role. Keep concise Chinese summaries and sorted lowercase reason "
        f"codes. Full {evidence_resources} remain read-only and available on demand. Inspect "
        "global or visual evidence "
        f"when needed. Window is {window.window_id}; Kit version is {kit_version}. Return only "
        "schema-valid JSON.\nBEGIN_UNTRUSTED_CLASSIFICATION_WINDOW_JSON\n"
        + json.dumps(
            inline_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\nEND_UNTRUSTED_CLASSIFICATION_WINDOW_JSON"
    )


def _classification_correction_prompt(
    *,
    window: SourceRequirementClassificationWindowV1,
    kit_version: str,
    source_pdf_available: bool,
) -> str:
    evidence_note = (
        " source.pdf remains available for visual correction."
        if source_pdf_available
        else " Do not claim visual review because no source PDF is available."
    )
    return (
        "Use $reqpilotx-parent-provision-recognition for "
        "execution_mode=SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW. Read "
        "classification-window.json, previous-candidate.json and validation-feedback.json. "
        "Correct only this window and return exactly one complete A/B/F decision per expected "
        f"record key. Do not change boundaries. Window is {window.window_id}; Kit version is "
        f"{kit_version}." + evidence_note + " Return only schema-valid JSON."
    )


def _boundary_validation_feedback(
    exc: Exception,
    *,
    request: SourceRequirementScopeRequestV1,
) -> dict[str, object]:
    return {
        "schema_version": "boundary-plan-validation-feedback/v1",
        "logical_table_scope_id": request.logical_table_scope_id,
        "fragment_row_counts": {
            item.fragment_id: item.row_count for item in request.fragments
        },
        "failure_code": _safe_windowed_validation_error(exc),
        "instruction": (
            "Return one complete business boundary plan with exact row ownership. Use "
            "row_start + positive row_count; do not add A/B/F."
        ),
    }


def _safe_windowed_validation_error(exc: Exception | None) -> str:
    if exc is None:
        return "WINDOWED_CANDIDATE_INVALID"
    if isinstance(exc, SourceRequirementBaselineError):
        value = str(exc).strip()
        if value:
            return value[:160]
    if isinstance(exc, ValidationError):
        return "WINDOWED_SCHEMA_INVALID"
    value = str(exc).strip()
    if value and value.replace("_", "").isalnum() and value.upper() == value:
        return value[:160]
    return "WINDOWED_CANDIDATE_INVALID"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _boundary_identity_sha256(plan: SourceRequirementBoundaryPlanV1) -> str:
    return canonical_json_sha256(
        {
            "source_sha256": plan.source_sha256,
            "document_map_artifact_id": plan.document_map_artifact_id,
            "document_map_artifact_sha256": plan.document_map_artifact_sha256,
            "request_sha256": plan.request_sha256,
            "logical_table_scope_id": plan.logical_table_scope_id,
            "records": [
                {
                    "record_key": record.record_key,
                    "source_form": record.source_form.value,
                    "row_groups": [
                        item.model_dump(mode="json") for item in record.row_groups
                    ],
                    "shared_source_content_ids": record.shared_source_content_ids,
                }
                for record in plan.records
            ],
            "context_row_groups": [
                {
                    "role": item.role.value,
                    "row_group": item.row_group.model_dump(mode="json"),
                }
                for item in plan.context_row_groups
            ],
        }
    )


def _merge_adjacent_context_groups(
    values: tuple[SourceRequirementContextRowGroupV1, ...],
) -> tuple[SourceRequirementContextRowGroupV1, ...]:
    merged: list[SourceRequirementContextRowGroupV1] = []
    for value in values:
        if (
            merged
            and merged[-1].role is value.role
            and merged[-1].row_group.fragment_id == value.row_group.fragment_id
            and merged[-1].row_group.row_end == value.row_group.row_start
        ):
            previous = merged.pop()
            merged.append(
                SourceRequirementContextRowGroupV1(
                    role=value.role,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id=value.row_group.fragment_id,
                        row_start=previous.row_group.row_start,
                        row_end=value.row_group.row_end,
                    ),
                    reason_codes=tuple(
                        sorted(set(previous.reason_codes).union(value.reason_codes))
                    ),
                )
            )
        else:
            merged.append(value)
    return tuple(merged)


def _validate_machine_codes(
    value: tuple[str, ...],
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not allow_empty and not value:
        raise ValueError("machine codes cannot be empty")
    if value != tuple(sorted(set(value))) or any(
        re.fullmatch(r"[a-z][a-z0-9_]{0,79}", item) is None for item in value
    ):
        raise ValueError("machine codes must be sorted lowercase identifiers")
    return value


def _normalize_boundary_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    diagnostics = normalized.get("diagnostic_codes")
    if isinstance(diagnostics, list):
        normalized["diagnostic_codes"] = sorted(
            {item for item in diagnostics if isinstance(item, str)}
        )
    records = normalized.get("records")
    if isinstance(records, list):
        normalized_records = []
        for value in records:
            if not isinstance(value, dict):
                normalized_records.append(value)
                continue
            item = dict(value)
            shared = item.get("shared_source_content_ids")
            if isinstance(shared, list):
                item["shared_source_content_ids"] = list(
                    dict.fromkeys(entry for entry in shared if isinstance(entry, str))
                )
            normalized_records.append(item)
        normalized["records"] = normalized_records
    context = normalized.get("context_row_groups")
    if isinstance(context, list):
        normalized_context = []
        for value in context:
            if not isinstance(value, dict):
                normalized_context.append(value)
                continue
            item = dict(value)
            reasons = item.get("reason_codes")
            if isinstance(reasons, list):
                item["reason_codes"] = sorted(
                    {entry for entry in reasons if isinstance(entry, str)}
                )
            normalized_context.append(item)
        normalized["context_row_groups"] = normalized_context
    return normalized


def _normalize_classification_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    diagnostics = normalized.get("diagnostic_codes")
    if isinstance(diagnostics, list):
        normalized["diagnostic_codes"] = sorted(
            {item for item in diagnostics if isinstance(item, str)}
        )
    decisions = normalized.get("decisions")
    if isinstance(decisions, list):
        normalized_decisions = []
        for value in decisions:
            if not isinstance(value, dict):
                normalized_decisions.append(value)
                continue
            item = dict(value)
            kinds = item.get("requirement_kinds")
            if isinstance(kinds, list):
                item["requirement_kinds"] = sorted(
                    {entry for entry in kinds if isinstance(entry, str)}
                )
            reasons = item.get("reason_codes")
            if isinstance(reasons, list):
                item["reason_codes"] = sorted(
                    {entry for entry in reasons if isinstance(entry, str)}
                )
            normalized_decisions.append(item)
        normalized["decisions"] = normalized_decisions
    return normalized


__all__ = [
    "CLASSIFICATION_AGENT_SCHEMA_VERSION",
    "CLASSIFICATION_WINDOW_SCHEMA_VERSION",
    "BOUNDARY_AGENT_SCHEMA_VERSION",
    "BOUNDARY_PLAN_SCHEMA_VERSION",
    "SourceRequirementBoundaryAgentCandidateV1",
    "SourceRequirementBoundaryPlanV1",
    "SourceRequirementClassificationAgentCandidateV1",
    "SourceRequirementClassificationWindowV1",
    "NativeSourceRequirementWindowResultCheckpointV1",
    "NativeSourceRequirementWindowedCheckpointV1",
    "WindowedCheckpointSink",
    "run_native_source_requirement_windowed_reference",
]
