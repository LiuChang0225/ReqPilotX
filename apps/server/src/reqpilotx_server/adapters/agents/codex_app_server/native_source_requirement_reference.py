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
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
)

from reqpilotx_server.adapters.agents.agent_kit_materializer import materialize_agent_kit
from reqpilotx_server.adapters.agents.codex_app_server.native_reference import (
    _codex_version,
    _safe_process_error,
    _summarize_jsonl_events,
    _write_private_json,
)
from reqpilotx_server.adapters.agents.portable_kit import load_portable_agent_kit
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    ContextRowRole,
    DocumentMapArtifactV1,
    NonRequirementRole,
    RequirementKind,
    Requirementness,
    SourceRecordForm,
    SourceRequirementBoundaryCandidateV1,
    SourceRequirementBaselineError,
    SourceRequirementContextRowGroupV1,
    SourceRequirementRowGroupV1,
    SourceRequirementScopeRequestV1,
    SourceRequirementSliceCandidateV1,
    canonical_json_sha256,
    source_requirement_slice_candidate_output_schema,
    validate_source_requirement_slice_candidate,
)
from reqpilotx_server.application.agent_workflow.runner_output_schema import (
    compile_strict_runner_output_schema,
)

NATIVE_SOURCE_REQUIREMENT_REFERENCE_RUNNER_VERSION = "1.10.0"
NATIVE_SOURCE_REQUIREMENT_RUN_RECEIPT_SCHEMA_VERSION = (
    "native-source-requirement-run-receipt/v1"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class NativeSourceRequirementRunReceiptV1(_StrictFrozenModel):
    schema_version: str = NATIVE_SOURCE_REQUIREMENT_RUN_RECEIPT_SCHEMA_VERSION
    runner_version: str = NATIVE_SOURCE_REQUIREMENT_REFERENCE_RUNNER_VERSION
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_table_scope_id: str = Field(min_length=1, max_length=200)
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
    record_count: StrictInt = Field(ge=1)
    context_row_group_count: StrictInt = Field(ge=0)
    normalized_machine_key_count: StrictInt = Field(default=0, ge=0)
    host_assigned_machine_key_count: StrictInt = Field(default=0, ge=0)
    execution_profile: Literal[
        "legacy-full-context", "compact-adaptive"
    ] = "legacy-full-context"
    requested_reasoning_effort: str = Field(default="high", min_length=1, max_length=40)
    attempt_reasoning_efforts: tuple[str, ...] = ()
    attempt_count: StrictInt = Field(default=1, ge=1)
    evidence_context_bytes: StrictInt = Field(default=0, ge=0)
    output_schema_bytes: StrictInt = Field(default=0, ge=0)
    raw_candidate_bytes: StrictInt = Field(default=0, ge=0)
    evidence_delivery: Literal["FILES", "INLINE_AND_FILES"] = "FILES"
    initial_prompt_bytes: StrictInt = Field(default=0, ge=0)
    on_demand_evidence_resources: tuple[str, ...] = ()
    reasoning_selection: Literal[
        "REQUESTED", "COMPACT_LOW_RISK_MEDIUM", "COMPACT_RISK_PRESERVED"
    ] = "REQUESTED"
    reasoning_risk_signals: tuple[str, ...] = ()
    execution_strategy: Literal["SINGLE_TURN", "BOUNDARY_WINDOWS"] = (
        "SINGLE_TURN"
    )
    boundary_plan_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    boundary_identity_sha256: str = Field(
        default="", pattern=r"^$|^[0-9a-f]{64}$"
    )
    boundary_elapsed_milliseconds: StrictInt = Field(default=0, ge=0)
    classification_window_count: StrictInt = Field(default=0, ge=0)
    classification_attempt_count: StrictInt = Field(default=0, ge=0)
    classification_elapsed_milliseconds: StrictInt = Field(default=0, ge=0)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @field_validator(
        "event_counts",
        "tool_names",
        "on_demand_evidence_resources",
        "reasoning_risk_signals",
    )
    @classmethod
    def validate_sorted_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("native source-requirement receipt values must be sorted")
        return value

    @field_validator("visual_page_numbers")
    @classmethod
    def validate_visual_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(page < 1 for page in value):
            raise ValueError("native source-requirement visual pages must be sorted")
        return value


class _SourceRequirementAgentRowGroupV1(_StrictFrozenModel):
    fragment_id: str = Field(min_length=1, max_length=200)
    row_start: StrictInt = Field(ge=0)
    row_count: StrictInt = Field(ge=1)


class _SourceRequirementAgentContextRowGroupV1(_StrictFrozenModel):
    role: ContextRowRole
    row_group: _SourceRequirementAgentRowGroupV1
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)


class _SourceRequirementAgentRecordV1(_StrictFrozenModel):
    """Semantic Agent output; Host-owned identity is deliberately absent."""

    label: str = Field(min_length=1, max_length=1000)
    source_form: SourceRecordForm
    row_groups: tuple[_SourceRequirementAgentRowGroupV1, ...] = Field(
        min_length=1, max_length=512
    )
    shared_source_content_ids: tuple[str, ...] = Field(default=(), max_length=256)
    requirementness: Requirementness
    requirement_kinds: tuple[RequirementKind, ...] = Field(default=(), max_length=3)
    non_requirement_role: NonRequirementRole | None = None
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    decision_summary: str = Field(min_length=1, max_length=1000)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)


class _SourceRequirementAgentCandidateV1(_StrictFrozenModel):
    schema_version: Literal["source-requirement-agent-candidate/v1"] = (
        "source-requirement-agent-candidate/v1"
    )
    records: tuple[_SourceRequirementAgentRecordV1, ...] = Field(
        min_length=1, max_length=8192
    )
    context_row_groups: tuple[_SourceRequirementAgentContextRowGroupV1, ...] = Field(
        default=(), max_length=1024
    )
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=64)


class NativeSourceRequirementRunResult(_StrictFrozenModel):
    candidate: SourceRequirementSliceCandidateV1
    run_receipt: NativeSourceRequirementRunReceiptV1


def _compact_document_context(
    document_map: DocumentMapArtifactV1,
    request: SourceRequirementScopeRequestV1,
) -> dict[str, object]:
    """Project the accepted Map into the smallest sufficient semantic context.

    The Host validates the complete Map before this projection. The Agent receives
    enough document and neighboring-scope context to interpret the selected table,
    while exact source cells remain exclusively in ``scope.json``.
    """

    table_ids = tuple(item.scope_id for item in document_map.logical_tables)
    table_index = table_ids.index(request.logical_table_scope_id)
    adjacent = document_map.logical_tables[
        max(0, table_index - 1) : min(len(table_ids), table_index + 2)
    ]
    from reqpilotx_server.application.agent_workflow.classification_context import build_classification_context
    return {
        "schema_version": "source-requirement-document-context/v1",
        "source_sha256": document_map.source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "document_map_revision": document_map.revision,
        "document_summary": document_map.document_summary,
        "semantic_context": build_classification_context(document_map, node_ids=(request.node_id,), table_previews={request.logical_table_scope_id: {
            "logical_table_scope_id": request.logical_table_scope_id, "title": request.logical_table_title,
            "source_sha256": request.source_sha256, "fragments": [item.model_dump(mode="json") for item in request.fragments]}}),
        "current_node": {
            "node_id": request.node_id,
            "node_label": request.node_label,
            "node_title": request.node_title,
            "ancestor_path": request.ancestor_path,
        },
        "current_scope": {
            "scope_id": request.logical_table_scope_id,
            "title": request.logical_table_title,
            "role": request.logical_table_role,
            "source_numbering_anomaly": document_map.logical_tables[
                table_index
            ].source_numbering_anomaly,
            "capture_gap": document_map.logical_tables[table_index].capture_gap,
        },
        "adjacent_logical_scopes": [
            {
                "scope_id": item.scope_id,
                "title": item.title,
                "role": item.role,
                "pages": item.pages,
                "source_numbering_anomaly": item.source_numbering_anomaly,
                "capture_gap": item.capture_gap,
            }
            for item in adjacent
        ],
    }


def _compact_scope_evidence(
    request: SourceRequirementScopeRequestV1,
) -> dict[str, object]:
    """Remove Host-only cell identity and geometry from the semantic payload."""

    return {
        "schema_version": "source-requirement-agent-scope/v1",
        "node_id": request.node_id,
        "node_label": request.node_label,
        "node_title": request.node_title,
        "ancestor_path": request.ancestor_path,
        "logical_table_scope_id": request.logical_table_scope_id,
        "logical_table_title": request.logical_table_title,
        "logical_table_role": request.logical_table_role,
        "source_content": [
            {
                "content_id": item.content_id,
                "text": item.text,
                "page": item.page,
                "precision": item.precision,
                "is_title": item.is_title,
            }
            for item in request.source_content
        ],
        "fragments": [
            {
                "fragment_id": fragment.fragment_id,
                "table_token": fragment.table_token,
                "page": fragment.page,
                "row_count": fragment.row_count,
                "column_count": fragment.column_count,
                "cells": [
                    {
                        "row": cell.row,
                        "column": cell.column,
                        "row_span": cell.row_span,
                        "column_span": cell.column_span,
                        "text": cell.text,
                    }
                    for cell in fragment.cells
                ],
            }
            for fragment in request.fragments
        ],
    }


def _reasoning_risk_signals(
    *,
    document_map: DocumentMapArtifactV1,
    request: SourceRequirementScopeRequestV1,
) -> tuple[str, ...]:
    table = next(
        item
        for item in document_map.logical_tables
        if item.scope_id == request.logical_table_scope_id
    )
    owner = next(
        item
        for item in document_map.nodes
        if item.node_id == request.node_id
    )
    signals: set[str] = set()
    page_count = len({fragment.page for fragment in request.fragments})
    row_count = sum(fragment.row_count for fragment in request.fragments)
    if len(request.fragments) > 1:
        signals.add("multi_fragment_scope")
    if page_count > 1:
        signals.add("multi_page_scope")
    if row_count > 32:
        signals.add("large_scope")
    if table.source_numbering_anomaly:
        signals.add("source_numbering_anomaly")
    if table.capture_gap:
        signals.add("capture_gap")
    if document_map.evidence_state != "SUFFICIENT":
        signals.add("document_evidence_not_sufficient")
    if any(
        cell.row_span > 1
        for fragment in request.fragments
        for cell in fragment.cells
    ):
        signals.add("row_spanning_cell")
    if any(
        anchor.asset_id or anchor.placement_id
        for anchor in owner.source_anchors
        if anchor.logical_table_scope_id in {"", request.logical_table_scope_id}
    ):
        signals.add("visual_asset_evidence")
    if any(not item.precision.startswith("EXACT") for item in request.source_content):
        signals.add("non_exact_source_content")
    return tuple(sorted(signals))


def _effective_reasoning_effort(
    *,
    request: SourceRequirementScopeRequestV1,
    requested: str,
    execution_profile: Literal["legacy-full-context", "compact-adaptive"],
    risk_signals: tuple[str, ...],
) -> tuple[str, str]:
    if execution_profile == "legacy-full-context":
        return requested, "REQUESTED"
    page_count = len({fragment.page for fragment in request.fragments})
    row_count = sum(fragment.row_count for fragment in request.fragments)
    if risk_signals:
        return requested, "COMPACT_RISK_PRESERVED"
    if len(request.fragments) == 1 and page_count == 1 and row_count <= 32:
        effort = (
            "medium"
            if requested in {"high", "xhigh", "max", "ultra"}
            else requested
        )
        return effort, (
            "COMPACT_LOW_RISK_MEDIUM" if effort == "medium" else "REQUESTED"
        )
    return requested, "REQUESTED"


def _materialize_candidate(
    payload: object,
    *,
    request: SourceRequirementScopeRequestV1,
    execution_profile: Literal["legacy-full-context", "compact-adaptive"],
) -> tuple[SourceRequirementSliceCandidateV1, int]:
    if execution_profile == "legacy-full-context" or (
        isinstance(payload, dict)
        and payload.get("schema_version") == "source-requirement-slice-candidate/v1"
    ):
        return (
            SourceRequirementSliceCandidateV1.model_validate(
                _normalize_candidate(payload)
            ),
            0,
        )
    compact = _SourceRequirementAgentCandidateV1.model_validate(payload)
    fragment_order = {
        fragment.fragment_id: index
        for index, fragment in enumerate(request.fragments)
    }
    reserved_keys: set[str] = set()
    records: list[SourceRequirementBoundaryCandidateV1] = []
    for item in compact.records:
        row_groups = tuple(
            sorted(
                (
                    SourceRequirementRowGroupV1(
                        fragment_id=group.fragment_id,
                        row_start=group.row_start,
                        row_end=group.row_start + group.row_count,
                    )
                    for group in item.row_groups
                ),
                key=lambda group: (
                    fragment_order.get(group.fragment_id, len(fragment_order)),
                    group.row_start,
                    group.row_end,
                ),
            )
        )
        record_key = _host_record_key(
            request=request,
            row_groups=row_groups,
            reserved_keys=reserved_keys,
        )
        reserved_keys.add(record_key)
        records.append(
            SourceRequirementBoundaryCandidateV1(
                record_key=record_key,
                label=item.label,
                source_form=item.source_form,
                row_groups=row_groups,
                shared_source_content_ids=tuple(
                    dict.fromkeys(item.shared_source_content_ids)
                ),
                requirementness=item.requirementness,
                requirement_kinds=tuple(
                    sorted(set(item.requirement_kinds), key=lambda value: value.value)
                ),
                non_requirement_role=item.non_requirement_role,
                confidence=item.confidence,
                decision_summary=item.decision_summary,
                reason_codes=tuple(sorted(set(item.reason_codes))),
            )
        )
    return (
        SourceRequirementSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            logical_table_scope_id=request.logical_table_scope_id,
            records=tuple(records),
            context_row_groups=tuple(
                sorted(
                    (
                        SourceRequirementContextRowGroupV1(
                            role=item.role,
                            row_group=SourceRequirementRowGroupV1(
                                fragment_id=item.row_group.fragment_id,
                                row_start=item.row_group.row_start,
                                row_end=(
                                    item.row_group.row_start
                                    + item.row_group.row_count
                                ),
                            ),
                            reason_codes=tuple(sorted(set(item.reason_codes))),
                        )
                        for item in compact.context_row_groups
                    ),
                    key=lambda item: (
                        fragment_order.get(
                            item.row_group.fragment_id, len(fragment_order)
                        ),
                        item.row_group.row_start,
                        item.row_group.row_end,
                        item.role.value,
                    ),
                )
            ),
            diagnostic_codes=tuple(sorted(set(compact.diagnostic_codes))),
        ),
        len(records),
    )


def _host_record_key(
    *,
    request: SourceRequirementScopeRequestV1,
    row_groups: tuple[SourceRequirementRowGroupV1, ...],
    reserved_keys: set[str],
) -> str:
    digest = canonical_json_sha256(
        {
            "logical_table_scope_id": request.logical_table_scope_id,
            "row_groups": [item.model_dump(mode="json") for item in row_groups],
        }
    )
    base = f"record-{digest[:16]}"
    value = base
    suffix = 2
    while value in reserved_keys:
        value = f"{base}-{suffix}"
        suffix += 1
    return value


def run_native_source_requirement_slice_reference(
    *,
    document_map: DocumentMapArtifactV1,
    request: SourceRequirementScopeRequestV1,
    output_directory: Path,
    codex_binary: str = "codex",
    model_id: str = "gpt-5.6-sol",
    reasoning_effort: str = "high",
    timeout_seconds: int = 1200,
    agent_kit_directory: Path | None = None,
    source_pdf_path: Path | None = None,
    execution_profile: Literal[
        "legacy-full-context", "compact-adaptive"
    ] = "compact-adaptive",
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> NativeSourceRequirementRunResult:
    if timeout_seconds < 60 or timeout_seconds > 3600:
        raise ValueError("native source-requirement timeout must be bounded")
    if reasoning_effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ValueError("native source-requirement reasoning effort is invalid")
    if execution_profile not in {"legacy-full-context", "compact-adaptive"}:
        raise ValueError("native source-requirement execution profile is invalid")
    if (
        request.document_map_artifact_id != document_map.artifact_id
        or request.document_map_artifact_sha256 != document_map.artifact_sha256
        or request.source_sha256 != document_map.source_sha256
    ):
        raise ValueError("native source-requirement document map lineage drifted")
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
            raise ValueError("native source-requirement visual source must be a file")
        with source_pdf.open("rb") as handle:
            visual_source_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
        if visual_source_sha256 != request.source_sha256:
            raise ValueError("native source-requirement visual source hash drifted")
        visual_page_numbers = tuple(
            sorted({fragment.page for fragment in request.fragments})
        )
    reasoning_risk_signals = (
        ()
        if execution_profile == "legacy-full-context"
        else _reasoning_risk_signals(
            document_map=document_map,
            request=request,
        )
    )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="reqpilotx-native-source-requirement-"
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
        context_path = workspace / (
            "document-map.json"
            if execution_profile == "legacy-full-context"
            else "document-context.json"
        )
        context_payload = (
            document_map.model_dump(mode="json")
            if execution_profile == "legacy-full-context"
            else _compact_document_context(document_map, request)
        )
        _write_private_json(context_path, context_payload, exclusive=True)
        on_demand_evidence_resources: list[str] = []
        if execution_profile == "compact-adaptive":
            full_map_path = workspace / "document-map.full.json"
            full_scope_path = workspace / "scope.full.json"
            _write_private_json(
                full_map_path,
                document_map.model_dump(mode="json"),
                exclusive=True,
            )
            _write_private_json(
                full_scope_path,
                request.model_dump(mode="json"),
                exclusive=True,
            )
            os.chmod(full_map_path, 0o400)
            os.chmod(full_scope_path, 0o400)
            on_demand_evidence_resources.extend(
                ("document-map.full.json", "scope.full.json")
            )
        if source_pdf is not None:
            visual_target = workspace / "source.pdf"
            shutil.copyfile(source_pdf, visual_target)
            os.chmod(visual_target, 0o400)
            on_demand_evidence_resources.append("source.pdf")
        scope_payload = (
            request.model_dump(mode="json")
            if execution_profile == "legacy-full-context"
            else _compact_scope_evidence(request)
        )
        _write_private_json(
            workspace / "scope.json", scope_payload, exclusive=True
        )
        schema_path = workspace / "source-requirement-candidate.schema.json"
        output_schema = (
            source_requirement_slice_candidate_output_schema()
            if execution_profile == "legacy-full-context"
            else compile_strict_runner_output_schema(
                _SourceRequirementAgentCandidateV1
            )
        )
        _write_private_json(schema_path, output_schema, exclusive=True)
        evidence_context_bytes = context_path.stat().st_size + (
            workspace / "scope.json"
        ).stat().st_size
        output_schema_bytes = schema_path.stat().st_size
        inline_evidence = (
            None
            if execution_profile == "legacy-full-context"
            else json.dumps(
                {
                    "document_context": context_payload,
                    "scope": scope_payload,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        stdout_values: list[str] = []
        candidate: SourceRequirementSliceCandidateV1 | None = None
        last_error: Exception | None = None
        normalized_machine_key_count = 0
        host_assigned_machine_key_count = 0
        raw_candidate_bytes = 0
        attempt_reasoning_efforts: list[str] = []
        effective_first_effort, reasoning_selection = _effective_reasoning_effort(
            request=request,
            requested=reasoning_effort,
            execution_profile=execution_profile,
            risk_signals=reasoning_risk_signals,
        )
        for attempt in (1, 2):
            final_path = workspace / f"source-requirement-candidate-{attempt}.json"
            attempt_effort = (
                effective_first_effort if attempt == 1 else reasoning_effort
            )
            attempt_reasoning_efforts.append(attempt_effort)
            command = _build_command(
                codex_binary=codex_binary,
                model_id=model_id,
                reasoning_effort=attempt_effort,
                workspace=workspace,
                schema_path=schema_path,
                final_path=final_path,
            )
            attempt_prompt = (
                _prompt(
                    request,
                    kit_version=kit_version,
                    visual_page_numbers=visual_page_numbers,
                    execution_profile=execution_profile,
                    inline_evidence=inline_evidence,
                )
                if attempt == 1
                else _correction_prompt(
                    request,
                    kit_version=kit_version,
                    visual_page_numbers=visual_page_numbers,
                    execution_profile=execution_profile,
                )
            )
            completed = command_runner(
                command,
                input=attempt_prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
            )
            stdout_values.append(completed.stdout)
            if completed.returncode != 0:
                raise RuntimeError(
                    "native Codex source-requirement reference failed: "
                    + _safe_process_error(completed.stderr, completed.stdout)
                )
            raw_candidate_bytes += final_path.stat().st_size
            raw_candidate = json.loads(final_path.read_text(encoding="utf-8"))
            try:
                attempt_machine_key_count = (
                    _invalid_record_key_count(raw_candidate)
                    if isinstance(raw_candidate, dict)
                    and raw_candidate.get("schema_version")
                    == "source-requirement-slice-candidate/v1"
                    else 0
                )
                candidate, attempt_host_assigned_count = _materialize_candidate(
                    raw_candidate,
                    request=request,
                    execution_profile=execution_profile,
                )
                validate_source_requirement_slice_candidate(request, candidate)
                normalized_machine_key_count = attempt_machine_key_count
                host_assigned_machine_key_count = attempt_host_assigned_count
                last_error = None
                break
            except (SourceRequirementBaselineError, ValidationError, ValueError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                _write_private_json(
                    workspace / "previous-candidate.json",
                    raw_candidate,
                    exclusive=True,
                )
                _write_private_json(
                    workspace / "validation-feedback.json",
                    _validation_feedback(exc, request=request),
                    exclusive=True,
                )
        if candidate is None or last_error is not None:
            raise RuntimeError(
                "native Codex source-requirement candidate remained invalid: "
                + _safe_validation_error(last_error)
            )
        event_counts, tool_names = _summarize_jsonl_events("\n".join(stdout_values))
    candidate_sha256 = canonical_json_sha256(candidate.model_dump(mode="json"))
    run_receipt = NativeSourceRequirementRunReceiptV1(
        source_sha256=request.source_sha256,
        document_map_artifact_id=document_map.artifact_id,
        document_map_artifact_sha256=document_map.artifact_sha256,
        request_sha256=request.request_sha256,
        logical_table_scope_id=request.logical_table_scope_id,
        model_id=model_id,
        reasoning_effort=attempt_reasoning_efforts[-1],
        codex_version=codex_version,
        kit_version=kit_version,
        kit_bundle_sha256=kit_bundle_sha256,
        visual_source_sha256=visual_source_sha256,
        visual_page_numbers=visual_page_numbers,
        elapsed_milliseconds=round((time.monotonic() - started) * 1000),
        event_counts=event_counts,
        tool_names=tool_names,
        record_count=len(candidate.records),
        context_row_group_count=len(candidate.context_row_groups),
        normalized_machine_key_count=normalized_machine_key_count,
        host_assigned_machine_key_count=host_assigned_machine_key_count,
        execution_profile=execution_profile,
        requested_reasoning_effort=reasoning_effort,
        attempt_reasoning_efforts=tuple(attempt_reasoning_efforts),
        attempt_count=len(attempt_reasoning_efforts),
        evidence_context_bytes=evidence_context_bytes,
        output_schema_bytes=output_schema_bytes,
        raw_candidate_bytes=raw_candidate_bytes,
        evidence_delivery=(
            "FILES"
            if execution_profile == "legacy-full-context"
            else "INLINE_AND_FILES"
        ),
        initial_prompt_bytes=len(
            _prompt(
                request,
                kit_version=kit_version,
                visual_page_numbers=visual_page_numbers,
                execution_profile=execution_profile,
                inline_evidence=inline_evidence,
            ).encode("utf-8")
        ),
        on_demand_evidence_resources=tuple(
            sorted(set(on_demand_evidence_resources))
        ),
        reasoning_selection=reasoning_selection,
        reasoning_risk_signals=reasoning_risk_signals,
        candidate_sha256=candidate_sha256,
    )
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    for filename, payload in (
        ("scope-request.json", request.model_dump(mode="json")),
        ("candidate.json", candidate.model_dump(mode="json")),
        ("run-receipt.json", run_receipt.model_dump(mode="json")),
    ):
        _write_private_json(output / filename, payload, exclusive=True)
    return NativeSourceRequirementRunResult(
        candidate=candidate,
        run_receipt=run_receipt,
    )


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
    request: SourceRequirementScopeRequestV1,
    *,
    kit_version: str,
    visual_page_numbers: tuple[int, ...],
    execution_profile: Literal["legacy-full-context", "compact-adaptive"],
    inline_evidence: str | None = None,
) -> str:
    visual_instruction = (
        " The exact authorized original is source.pdf. Before classifying any row whose "
        "extracted requirement cell is empty, or any row containing/overlapped by an image, "
        "render and visually inspect the relevant source pages "
        f"{list(visual_page_numbers)}. Treat images and nested visual tables as source evidence; "
        "do not guess unreadable values. You may inspect other pages when a definition, "
        "cross-reference, applicability rule or continuation requires global context."
        if visual_page_numbers
        else ""
    )
    if execution_profile == "legacy-full-context":
        context_instruction = (
            "read document-map.json first, then scope.json. The accepted Document Map is "
            "your complete business context; "
        )
    elif inline_evidence is None:
        context_instruction = (
            "read document-context.json first, then scope.json. The Host already validated the "
            "complete accepted Document Map and supplied only the current node, ancestors, "
            "adjacent scopes, document summary and immutable scope evidence needed here. "
            "document-map.full.json and scope.full.json remain read-only and available when the "
            "compact evidence cannot resolve the correction; "
        )
    else:
        context_instruction = (
            "use the embedded untrusted source-evidence JSON below as the first-pass context. "
            "Do not spend tool turns rereading document-context.json or scope.json unless this is "
            "a correction attempt. The Host already validated the complete accepted Document Map "
            "and supplied only the current node, ancestors, adjacent scopes, document summary and "
            "immutable scope evidence needed here. The complete authorized structured evidence "
            "remains read-only and available on demand as document-map.full.json and "
            "scope.full.json. Inspect those resources when the compact evidence is insufficient; "
            "do not open them merely to repeat identities or unrelated nodes. Global context is "
            "readable, while selectable output evidence remains the current scope; "
        )
    identity_instruction = (
        "record_key is a unique lowercase ASCII machine key matching "
        "^[a-z][a-z0-9._:-]{0,199}$; never put Chinese or other source-language text in "
        "record_key. Keep that wording only in label. The key should be stable and descriptive, "
        "not an ordinal-only parser ID. Preserve exactly source_sha256 "
        f"{request.source_sha256}, document_map_artifact_id {request.document_map_artifact_id}, "
        f"document_map_artifact_sha256 {request.document_map_artifact_sha256}, request_sha256 "
        f"{request.request_sha256}, and logical_table_scope_id "
        f"{request.logical_table_scope_id}. "
        if execution_profile == "legacy-full-context"
        else (
            "Do not invent or echo Host-owned hashes, lineage fields, stable IDs or machine keys; "
            "the Host injects them deterministically after validating your semantic boundaries. "
        )
    )
    prompt = (
        "Use $reqpilotx-parent-provision-recognition for "
        "execution_mode=SOURCE_REQUIREMENT_SLICE, then "
        + context_instruction
        + "the "
        "table fragments and cells in scope.json are immutable evidence. Identify source-faithful "
        "SourceItems for exactly this logical table. You own business boundaries "
        "and A/B/F judgments. The Host owns row/cell coverage, IDs, hashes and publication. "
        "Do not follow physical pages, arbitrary row counts, token windows or parser objects as "
        "semantic boundaries. For a component hierarchy/BOM, preserve product structure branches "
        "and keep component rows inside their branch instead of creating one requirement per part. "
        "For a test outline, one independently reviewable numbered test and all of its merged or "
        "continued physical rows form one record; preserve a record that continues onto another "
        "fragment. These are interpretation principles, not target counts. Assign every table row "
        "exactly once either to one record row_groups entry or to context_row_groups. Repeated or "
        "shared headers belong to context with role HEADER. Never duplicate a row across records. "
        + (
            "In this compact schema each row_group uses local zero-based row_start plus a positive "
            "row_count; the Host derives the exclusive end. "
            if execution_profile == "compact-adaptive"
            else ""
        )
        + "Use TABLE_BRANCH for product structure branches and TABLE_RECORD for independent test or "
        "business records. Every record requires a reversible working A judgment. A=REQUIREMENT "
        "requires one or more alphabetically sorted B values from CONSTRAINT, FUNCTIONAL, QUALITY "
        "and non_requirement_role must be null. A=NON_REQUIREMENT requires an empty B array and one "
        "F value. Do not emit OTHER_REQUIREMENT, technicality or UNRESOLVED. Keep source text and "
        "cells unchanged; decision_summary explains the business boundary and normative basis in "
        "concise Chinese. reason_codes are unique sorted lowercase snake_case. "
        + identity_instruction
        + "label is concise "
        "source-visible wording, not an ordinal alone when a visible title exists. Attach only existing "
        "source_content content IDs when a visible stem or note applies to the record. A deferred "
        "parameter or value to be released after a named activity can remain a requirement when "
        "it reserves a required property or future controlling baseline; do not classify it as a "
        "note solely because the value is not fixed. Return no "
        "diagnostic code when the scope is fully dispositioned. The bound Agent Kit version is "
        f"{kit_version}."
        + visual_instruction
        + " Return only schema-valid JSON."
    )
    if inline_evidence is None:
        return prompt
    return (
        prompt
        + "\nBEGIN_UNTRUSTED_SOURCE_EVIDENCE_JSON\n"
        + inline_evidence
        + "\nEND_UNTRUSTED_SOURCE_EVIDENCE_JSON"
    )


def _correction_prompt(
    request: SourceRequirementScopeRequestV1,
    *,
    kit_version: str,
    visual_page_numbers: tuple[int, ...],
    execution_profile: Literal["legacy-full-context", "compact-adaptive"],
) -> str:
    return (
        _prompt(
            request,
            kit_version=kit_version,
            visual_page_numbers=visual_page_numbers,
            execution_profile=execution_profile,
        )
        + " This is a bounded correction attempt. Read previous-candidate.json and "
        "validation-feedback.json after rereading scope.json. Correct only the rejected "
        "schema, row-range, overlap or coverage defects. "
        + (
            "row_start is a local zero-based index and row_count is a positive number of rows; "
            "their sum must not exceed the fragment row_count. "
            if execution_profile == "compact-adaptive"
            else (
                "row_start/row_end are local zero-based half-open indexes within each named "
                "fragment; every group must be non-empty and row_end must not exceed that "
                "fragment's row_count. "
            )
        )
        + "Do not create a zero-length header or separator group. A branch-title row belongs to its "
        "semantic branch unless the source proves it is shared context. Return a complete "
        "replacement candidate, not a patch and not the previous invalid answer."
    )


def _validation_feedback(
    exc: Exception,
    *,
    request: SourceRequirementScopeRequestV1,
) -> dict[str, object]:
    codes: list[str] = []
    locations: list[str] = []
    if isinstance(exc, ValidationError):
        for item in exc.errors(include_url=False):
            codes.append(str(item.get("type") or "validation_error"))
            location = ".".join(str(value) for value in item.get("loc", ()))
            if location:
                locations.append(location[:240])
    else:
        code = str(exc).strip()
        codes.append(
            code
            if re.fullmatch(r"[A-Z][A-Z0-9_]{1,159}", code)
            else "SOURCE_REQUIREMENT_CANDIDATE_INVALID"
        )
    record_key_pattern_failed = any(
        code == "string_pattern_mismatch" for code in codes
    ) and any(location.endswith(".record_key") for location in locations)
    instruction = (
        "Replace every rejected record_key with a unique lowercase ASCII machine key "
        "matching ^[a-z][a-z0-9._:-]{0,199}$; keep Chinese and source-visible wording "
        "only in label. Preserve all accepted semantic boundaries, classifications and "
        "row groups. Return one complete replacement for the immutable scope."
        if record_key_pattern_failed
        else (
            "Use local zero-based half-open row ranges, remove empty groups, and return "
            "one complete exact row partition for the same immutable scope."
        )
    )
    return {
        "schema_version": "source-requirement-validation-feedback/v1",
        "logical_table_scope_id": request.logical_table_scope_id,
        "request_sha256": request.request_sha256,
        "failure_codes": sorted(set(codes)),
        "field_locations": sorted(set(locations)),
        "fragment_row_counts": {
            item.fragment_id: item.row_count for item in request.fragments
        },
        "instruction": instruction,
    }


def _safe_validation_error(exc: Exception | None) -> str:
    if exc is None:
        return "SOURCE_REQUIREMENT_CANDIDATE_INVALID"
    if isinstance(exc, SourceRequirementBaselineError):
        value = str(exc).strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,159}", value):
            return value
    if isinstance(exc, ValidationError):
        return "SOURCE_REQUIREMENT_SCHEMA_OR_RANGE_INVALID"
    return "SOURCE_REQUIREMENT_CANDIDATE_INVALID"


def _normalize_candidate(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    diagnostics = normalized.get("diagnostic_codes")
    if isinstance(diagnostics, list):
        normalized["diagnostic_codes"] = sorted(
            {item for item in diagnostics if isinstance(item, str)}
        )
    context_values = normalized.get("context_row_groups")
    if isinstance(context_values, list):
        normalized["context_row_groups"] = [
            _normalize_reason_container(item) for item in context_values
        ]
    records = normalized.get("records")
    if isinstance(records, list):
        reserved_keys = {
            value.get("record_key")
            for value in records
            if isinstance(value, dict)
            and isinstance(value.get("record_key"), str)
            and re.fullmatch(r"[a-z][a-z0-9._:-]{0,199}", value["record_key"])
            is not None
        }
        normalized_records: list[object] = []
        for index, value in enumerate(records, start=1):
            if not isinstance(value, dict):
                normalized_records.append(value)
                continue
            item = dict(_normalize_reason_container(value))
            record_key = item.get("record_key")
            if (
                not isinstance(record_key, str)
                or re.fullmatch(r"[a-z][a-z0-9._:-]{0,199}", record_key) is None
            ):
                base = f"record-{index:04d}"
                candidate_key = base
                suffix = 2
                while candidate_key in reserved_keys:
                    candidate_key = f"{base}-{suffix}"
                    suffix += 1
                item["record_key"] = candidate_key
                reserved_keys.add(candidate_key)
            kinds = item.get("requirement_kinds")
            if isinstance(kinds, list):
                item["requirement_kinds"] = sorted(
                    {kind for kind in kinds if isinstance(kind, str)}
                )
            shared = item.get("shared_source_content_ids")
            if isinstance(shared, list):
                item["shared_source_content_ids"] = list(
                    dict.fromkeys(value for value in shared if isinstance(value, str))
                )
            normalized_records.append(item)
        normalized["records"] = normalized_records
    return normalized


def _invalid_record_key_count(payload: object) -> int:
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        return 0
    return sum(
        not isinstance(item, dict)
        or not isinstance(item.get("record_key"), str)
        or re.fullmatch(
            r"[a-z][a-z0-9._:-]{0,199}",
            item.get("record_key", ""),
        )
        is None
        for item in payload["records"]
    )


def _normalize_reason_container(value: object) -> object:
    if not isinstance(value, dict):
        return value
    item = dict(value)
    reasons = item.get("reason_codes")
    if isinstance(reasons, list):
        item["reason_codes"] = sorted(
            {reason for reason in reasons if isinstance(reason, str)}
        )
    return item


__all__ = [
    "NATIVE_SOURCE_REQUIREMENT_REFERENCE_RUNNER_VERSION",
    "NativeSourceRequirementRunReceiptV1",
    "NativeSourceRequirementRunResult",
    "run_native_source_requirement_slice_reference",
]
