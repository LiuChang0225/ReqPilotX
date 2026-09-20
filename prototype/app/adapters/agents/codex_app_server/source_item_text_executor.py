"""Windowed Canonical App Server executor for direct-text SourceItems."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager, contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import threading
import time
from typing import Any

from pydantic import ValidationError

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalProductEvidenceRequest,
    CanonicalSourceRequirementStageInputRequest,
    prepare_canonical_app_server_document_session,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageTransportResult,
    build_canonical_stage_invocation,
    canonical_sha256,
)
from app.adapters.agents.codex_app_server.canonical_stage_transport import (
    LocalCodexAppServerStructuredStageTransport,
)
from app.adapters.agents.codex_app_server.lifecycle import (
    CanaryRuntimeState,
    LifecycleProbeError,
    _canary_runtime_state,
    load_protocol_lock,
    verify_protocol_binary_identity,
)
from app.adapters.agents.codex_app_server.native_source_item_text_reference import (
    NativeSourceItemTextRunReceiptV1,
)
from app.adapters.agents.codex_app_server.native_source_item_text_windowed import (
    NATIVE_SOURCE_ITEM_TEXT_WINDOWED_RUNNER_VERSION,
    NativeSourceItemTextWindowedCheckpointV1,
    SourceItemTextBoundaryAgentCandidateV1,
    SourceItemTextBoundaryPlanV1,
    SourceItemTextClassificationAgentCandidateV1,
    SourceItemTextClassificationWindowV1,
    TextBoundaryRunResult,
    TextClassificationWindowRunResult,
    aggregate_source_item_text_batch,
    boundary_output_schema,
    boundary_result_from_checkpoint,
    boundary_validation_feedback,
    build_source_item_text_classification_window_input,
    build_source_item_text_classification_windows,
    build_source_item_text_windowed_checkpoint,
    classification_output_schema,
    compile_source_item_text_boundary_plan,
    normalize_boundary_payload,
    normalize_classification_payload,
    safe_validation_error,
    source_item_text_request_set_sha256,
    validate_source_item_text_resume_checkpoint_identity,
    window_result_from_checkpoint,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.application.agent_workflow.d3_parent_provisions import (
    PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
    PARENT_PROVISION_VALIDATOR_ID,
    PARENT_PROVISION_VALIDATOR_VERSION,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1,
    SourceItemTextScopeRequestV1,
    SourceRequirementBaselineError,
    canonical_json_sha256,
)
from app.ports.source_item_text_executor import (
    CheckpointSink,
    SourceItemTextExecutionV1,
    SourceItemTextTaskV1,
)


CANONICAL_SOURCE_ITEM_TEXT_EXECUTOR_ID = "canonical-source-item-text"
CANONICAL_SOURCE_ITEM_TEXT_EXECUTOR_VERSION = "2.4.0"
CANONICAL_SOURCE_ITEM_TEXT_TRANSPORT_KIND = "CODEX_APP_SERVER_STRUCTURED_STAGE"

BOUNDARY_EXECUTION_MODE = "SOURCE_ITEM_TEXT_BOUNDARY_PLAN"
CLASSIFICATION_EXECUTION_MODE = "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW"


TransportFactory = Callable[
    [float, Callable[[], bool]], LocalCodexAppServerStructuredStageTransport
]
RuntimeStateFactory = Callable[..., AbstractContextManager[CanaryRuntimeState]]


class LocalCanonicalSourceItemTextExecutor:
    """Freeze each text scope, then classify whole records with concurrency <= 2."""

    adapter_id = CANONICAL_SOURCE_ITEM_TEXT_EXECUTOR_ID
    adapter_version = CANONICAL_SOURCE_ITEM_TEXT_EXECUTOR_VERSION
    transport_kind = CANONICAL_SOURCE_ITEM_TEXT_TRANSPORT_KIND
    model_id = "gpt-5.6-sol"
    reasoning_effort = "high"

    def __init__(
        self,
        *,
        codex_bin: Path,
        codex_home: Path,
        repository_root: Path,
        transport_factory: TransportFactory | None = None,
        runtime_state_factory: RuntimeStateFactory = _canary_runtime_state,
    ) -> None:
        self._codex_bin = codex_bin.expanduser().resolve(strict=True)
        self._codex_home = codex_home.expanduser().resolve(strict=True)
        self._repository_root = repository_root.expanduser().resolve(strict=True)
        self._kit_root = self._repository_root / "agent-kit"
        self._transport_factory = transport_factory or (
            lambda timeout, cancelled: LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=timeout,
                cancellation_requested=cancelled,
            )
        )
        self._runtime_state_factory = runtime_state_factory
        self._evidence_cache_lock = threading.Lock()

    def readiness_reason(self) -> str | None:
        try:
            verify_protocol_binary_identity(self._codex_bin, lock=load_protocol_lock())
        except LifecycleProbeError as exc:
            return str(exc)
        return None

    def execute(
        self,
        task: SourceItemTextTaskV1,
        *,
        cancellation_requested: Callable[[], bool],
        resume_checkpoint: Mapping[str, Any] | None = None,
        checkpoint_sink: CheckpointSink | None = None,
    ) -> SourceItemTextExecutionV1:
        document_map, requests, source = _validate_task(task)
        execution_context = _validated_execution_context(task.execution_context)
        sink: CheckpointSink = checkpoint_sink or (lambda _value: None)
        kit = load_portable_agent_kit(self._kit_root)
        profile = kit.runner_profile("app-server-product")
        if (
            profile.model_id != self.model_id
            or profile.reasoning_effort != self.reasoning_effort
        ):
            raise LifecycleProbeError("CANONICAL_SOURCE_ITEM_TEXT_PROFILE_DRIFTED")
        request_set_sha256 = source_item_text_request_set_sha256(
            requests,
            execution_context=execution_context,
        )
        visual_pages = tuple(
            sorted(
                {
                    content.page
                    for request in requests
                    for content in request.source_content
                }
            )
        )
        context_payload = {
            "schema_version": "source-item-text-document-context/v1",
            "document_summary": document_map.document_summary,
            "root_node_ids": document_map.root_node_ids,
            "requested_scope_ids": tuple(item.scope_id for item in requests),
            "semantic_context": task.semantic_context,
            "execution_context_sha256": (
                canonical_json_sha256(execution_context)
                if execution_context is not None
                else ""
            ),
        }
        request_payload = {
            "schema_version": "source-item-text-request-set/v1",
            "request_set_sha256": request_set_sha256,
            "requests": [item.model_dump(mode="json") for item in requests],
        }
        checkpoint = (
            None
            if resume_checkpoint is None
            else NativeSourceItemTextWindowedCheckpointV1.model_validate(
                resume_checkpoint
            )
        )
        started = time.monotonic()
        with self._runtime_state_factory(
            codex_bin=self._codex_bin,
            codex_home=self._codex_home,
            repository_root=self._repository_root,
        ) as runtime:
            codex_version = str(runtime.protocol.get("codex_version") or "").strip()
            if not codex_version:
                raise LifecycleProbeError("CANONICAL_CODEX_VERSION_MISSING")
            if checkpoint is not None:
                validate_source_item_text_resume_checkpoint_identity(
                    checkpoint,
                    document_map=document_map,
                    requests=requests,
                    runner_version=NATIVE_SOURCE_ITEM_TEXT_WINDOWED_RUNNER_VERSION,
                    executor_id=self.adapter_id,
                    executor_version=self.adapter_version,
                    transport_kind=self.transport_kind,
                    model_id=self.model_id,
                    reasoning_effort=self.reasoning_effort,
                    codex_version=codex_version,
                    kit_version=kit.identity.kit_version,
                    kit_bundle_sha256=kit.identity.bundle_sha256,
                    visual_source_sha256=document_map.source_sha256,
                    visual_page_numbers=visual_pages,
                    max_concurrency=task.max_concurrency,
                    request_set_sha256=request_set_sha256,
                )
            return self._execute_with_runtime(
                runtime=runtime,
                task=task,
                source=source,
                document_map=document_map,
                requests=requests,
                request_set_sha256=request_set_sha256,
                context_payload=context_payload,
                request_payload=request_payload,
                checkpoint=checkpoint,
                checkpoint_sink=sink,
                cancellation_requested=cancellation_requested,
                codex_version=codex_version,
                kit_version=kit.identity.kit_version,
                kit_bundle_sha256=kit.identity.bundle_sha256,
                visual_pages=visual_pages,
                execution_context=execution_context,
                started=started,
            )

    def _execute_with_runtime(
        self,
        *,
        runtime: CanaryRuntimeState,
        task: SourceItemTextTaskV1,
        source: Path,
        document_map: DocumentMapArtifactV1,
        requests: tuple[SourceItemTextScopeRequestV1, ...],
        request_set_sha256: str,
        context_payload: Mapping[str, Any],
        request_payload: Mapping[str, Any],
        checkpoint: NativeSourceItemTextWindowedCheckpointV1 | None,
        checkpoint_sink: CheckpointSink,
        cancellation_requested: Callable[[], bool],
        codex_version: str,
        kit_version: str,
        kit_bundle_sha256: str,
        visual_pages: tuple[int, ...],
        execution_context: Mapping[str, object] | None,
        started: float,
    ) -> SourceItemTextExecutionV1:
        if cancellation_requested():
            raise LifecycleProbeError("CANONICAL_STAGE_CANCELLED_BEFORE_START")
        boundary_results = (
            {}
            if checkpoint is None
            else {
                item.plan.scope_id: boundary_result_from_checkpoint(item)
                for item in checkpoint.accepted_boundaries
            }
        )
        window_results = (
            {}
            if checkpoint is None
            else {
                item.window.window_id: window_result_from_checkpoint(item)
                for item in checkpoint.accepted_windows
            }
        )
        windows = () if checkpoint is None else checkpoint.classification_windows
        windows_planned = (
            False
            if checkpoint is None
            else checkpoint.classification_windows_planned
        )
        boundary_elapsed = (
            0 if checkpoint is None else checkpoint.boundary_elapsed_milliseconds
        )
        classification_elapsed = (
            0
            if checkpoint is None
            else checkpoint.classification_elapsed_milliseconds
        )

        for request in requests:
            if request.scope_id in boundary_results:
                continue
            result = self._run_boundary_phase(
                runtime=runtime,
                task=task,
                source=source,
                document_map=document_map,
                request=request,
                request_set_sha256=request_set_sha256,
                context_payload=context_payload,
                execution_context=execution_context,
                cancellation_requested=cancellation_requested,
            )
            boundary_results[request.scope_id] = result
            boundary_elapsed += result.elapsed_milliseconds
            checkpoint_sink(
                self._checkpoint(
                    requests=requests,
                    request_set_sha256=request_set_sha256,
                    codex_version=codex_version,
                    kit_version=kit_version,
                    kit_bundle_sha256=kit_bundle_sha256,
                    visual_pages=visual_pages,
                    boundary_results=boundary_results,
                    windows_planned=False,
                    windows=(),
                    window_results={},
                    boundary_elapsed=boundary_elapsed,
                    classification_elapsed=0,
                )
            )

        plans = tuple(boundary_results[item.scope_id].plan for item in requests)
        if not windows_planned:
            windows = build_source_item_text_classification_windows(
                document_map=document_map,
                requests=requests,
                plans=plans,
                max_concurrency=task.max_concurrency,
            )
            windows_planned = True
            checkpoint_sink(
                self._checkpoint(
                    requests=requests,
                    request_set_sha256=request_set_sha256,
                    codex_version=codex_version,
                    kit_version=kit_version,
                    kit_bundle_sha256=kit_bundle_sha256,
                    visual_pages=visual_pages,
                    boundary_results=boundary_results,
                    windows_planned=True,
                    windows=windows,
                    window_results=window_results,
                    boundary_elapsed=boundary_elapsed,
                    classification_elapsed=classification_elapsed,
                )
            )

        classification_started = time.monotonic()
        pending = tuple(
            item for item in windows if item.window_id not in window_results
        )
        failures: list[tuple[str, Exception]] = []
        if pending:
            with ThreadPoolExecutor(
                max_workers=min(2, task.max_concurrency, len(pending))
            ) as pool:
                futures = {
                    pool.submit(
                        self._run_classification_phase,
                        runtime=runtime,
                        task=task,
                        source=source,
                        document_map=document_map,
                        requests=requests,
                        request_set_sha256=request_set_sha256,
                        context_payload=context_payload,
                        execution_context=execution_context,
                        request_payload=request_payload,
                        plans=plans,
                        window=window,
                        cancellation_requested=cancellation_requested,
                    ): window.window_id
                    for window in pending
                }
                for future in as_completed(futures):
                    window_id = futures[future]
                    try:
                        window_results[window_id] = future.result()
                        checkpoint_sink(
                            self._checkpoint(
                                requests=requests,
                                request_set_sha256=request_set_sha256,
                                codex_version=codex_version,
                                kit_version=kit_version,
                                kit_bundle_sha256=kit_bundle_sha256,
                                visual_pages=visual_pages,
                                boundary_results=boundary_results,
                                windows_planned=True,
                                windows=windows,
                                window_results=window_results,
                                boundary_elapsed=boundary_elapsed,
                                classification_elapsed=(
                                    classification_elapsed
                                    + round(
                                        (
                                            time.monotonic()
                                            - classification_started
                                        )
                                        * 1000
                                    )
                                ),
                            )
                        )
                    except Exception as exc:
                        failures.append((window_id, exc))
        classification_elapsed += round(
            (time.monotonic() - classification_started) * 1000
        )
        if failures:
            failed_ids = ",".join(sorted(item[0] for item in failures))
            raise RuntimeError(
                "canonical source-item text windows remained invalid: "
                + failed_ids
            ) from failures[0][1]

        batch = aggregate_source_item_text_batch(
            document_map=document_map,
            requests=requests,
            plans=plans,
            window_results=window_results,
        )
        audit_summaries = tuple(
            summary
            for request in requests
            for summary in boundary_results[request.scope_id].audit_summaries
        ) + tuple(
            summary
            for window in windows
            for summary in window_results[window.window_id].audit_summaries
        )
        event_counts, tool_names = _summarize_audit_summaries(audit_summaries)
        candidate_sha256 = canonical_json_sha256(batch.model_dump(mode="json"))
        receipt = NativeSourceItemTextRunReceiptV1(
            runner_version=NATIVE_SOURCE_ITEM_TEXT_WINDOWED_RUNNER_VERSION,
            source_sha256=document_map.source_sha256,
            document_map_artifact_id=document_map.artifact_id,
            document_map_artifact_sha256=document_map.artifact_sha256,
            request_set_sha256=request_set_sha256,
            model_id=self.model_id,
            reasoning_effort=self.reasoning_effort,
            codex_version=codex_version,
            kit_version=kit_version,
            kit_bundle_sha256=kit_bundle_sha256,
            visual_source_sha256=document_map.source_sha256,
            visual_page_numbers=visual_pages,
            elapsed_milliseconds=max(
                round((time.monotonic() - started) * 1000),
                boundary_elapsed + classification_elapsed,
            ),
            event_counts=event_counts,
            tool_names=tool_names,
            scope_count=len(requests),
            scope_ids=tuple(sorted(item.scope_id for item in requests)),
            source_item_count=sum(len(item.items) for item in batch.candidates),
            structure_only_span_count=sum(
                len(item.structure_only_spans) for item in batch.candidates
            ),
            candidate_sha256=candidate_sha256,
        )
        return SourceItemTextExecutionV1(
            batch=batch.model_dump(mode="json"),
            run_receipt=receipt.model_dump(mode="json"),
        )

    def _checkpoint(
        self,
        *,
        requests: tuple[SourceItemTextScopeRequestV1, ...],
        request_set_sha256: str,
        codex_version: str,
        kit_version: str,
        kit_bundle_sha256: str,
        visual_pages: tuple[int, ...],
        boundary_results: Mapping[str, TextBoundaryRunResult],
        windows_planned: bool,
        windows: tuple[SourceItemTextClassificationWindowV1, ...],
        window_results: Mapping[str, TextClassificationWindowRunResult],
        boundary_elapsed: int,
        classification_elapsed: int,
    ) -> dict[str, Any]:
        return build_source_item_text_windowed_checkpoint(
            requests=requests,
            runner_version=NATIVE_SOURCE_ITEM_TEXT_WINDOWED_RUNNER_VERSION,
            executor_id=self.adapter_id,
            executor_version=self.adapter_version,
            transport_kind=self.transport_kind,
            model_id=self.model_id,
            reasoning_effort=self.reasoning_effort,
            codex_version=codex_version,
            kit_version=kit_version,
            kit_bundle_sha256=kit_bundle_sha256,
            visual_source_sha256=requests[0].source_sha256,
            visual_page_numbers=visual_pages,
            boundary_results=boundary_results,
            classification_windows_planned=windows_planned,
            classification_windows=windows,
            window_results=window_results,
            boundary_elapsed_milliseconds=boundary_elapsed,
            classification_elapsed_milliseconds=classification_elapsed,
            request_set_sha256=request_set_sha256,
        ).model_dump(mode="json")

    def _run_boundary_phase(
        self,
        *,
        runtime: CanaryRuntimeState,
        task: SourceItemTextTaskV1,
        source: Path,
        document_map: DocumentMapArtifactV1,
        request: SourceItemTextScopeRequestV1,
        request_set_sha256: str,
        context_payload: Mapping[str, Any],
        execution_context: Mapping[str, object] | None,
        cancellation_requested: Callable[[], bool],
    ) -> TextBoundaryRunResult:
        started = time.monotonic()
        summaries: list[str] = []
        efforts: list[str] = []
        raw_bytes = 0
        previous: Mapping[str, Any] | None = None
        feedback: Mapping[str, Any] | None = None
        last_error: Exception | None = None
        compact = _compact_scope(request)
        for attempt in (1, 2):
            if cancellation_requested():
                raise LifecycleProbeError("CANONICAL_STAGE_CANCELLED")
            efforts.append(self.reasoning_effort)
            files = _phase_files(
                document_map=document_map,
                context_payload=context_payload,
                scope_full=request.model_dump(mode="json"),
                scope_compact=compact,
                phase_input={
                    "schema_version": "source-item-text-boundary-phase-input/v1",
                    "execution_mode": BOUNDARY_EXECUTION_MODE,
                    "attempt": attempt,
                    "scope_id": request.scope_id,
                    "request_sha256": request.request_sha256,
                },
                previous_candidate=previous,
                validation_feedback=feedback,
                execution_context=execution_context,
            )
            transport = self._invoke_phase(
                runtime=runtime,
                task=task,
                source=source,
                document_map=document_map,
                request_set_sha256=request_set_sha256,
                request_sha256=request.request_sha256,
                scope_id=request.scope_id,
                execution_mode=BOUNDARY_EXECUTION_MODE,
                files=files,
                task_directive=(
                    "Read the locked direct-text scope. Return only scope_id, "
                    "record labels/forms, exact Unicode text spans, shared source "
                    "IDs, and structure-only title spans. Do not return A/B/F or "
                    "machine record keys. On attempt 2, correct the complete scope "
                    "using validation-feedback.json."
                    + (
                        " Apply the source-supported user guidance and comparison "
                        "scopes in revision-guidance.json. Treat the matching "
                        "current_affected_scopes entry as the accepted starting "
                        "boundary: verify it against source and guidance, preserve "
                        "it when it remains supported, and change it only with a "
                        "source-grounded reason. Still return one complete replacement "
                        "for only this affected scope."
                        if execution_context is not None
                        else ""
                    )
                ),
                cancellation_requested=cancellation_requested,
            )
            summaries.append(_audit_summary_text(transport.tool_activity_summary))
            raw = normalize_boundary_payload(dict(transport.structured_output))
            raw_bytes += len(_canonical_bytes(raw))
            try:
                candidate = SourceItemTextBoundaryAgentCandidateV1.model_validate(raw)
                plan = compile_source_item_text_boundary_plan(
                    request=request,
                    candidate=candidate,
                )
                return TextBoundaryRunResult(
                    plan=plan,
                    candidate=candidate,
                    audit_summaries=tuple(summaries),
                    attempt_efforts=tuple(efforts),
                    raw_candidate_bytes=raw_bytes,
                    elapsed_milliseconds=round(
                        (time.monotonic() - started) * 1000
                    ),
                )
            except (
                ValidationError,
                ValueError,
                SourceRequirementBaselineError,
            ) as exc:
                last_error = exc
                previous = raw if isinstance(raw, Mapping) else {"value": raw}
                feedback = boundary_validation_feedback(exc, request=request)
        raise RuntimeError(
            f"canonical source-item text boundary {request.scope_id} remained invalid: "
            + safe_validation_error(last_error)
        )

    def _run_classification_phase(
        self,
        *,
        runtime: CanaryRuntimeState,
        task: SourceItemTextTaskV1,
        source: Path,
        document_map: DocumentMapArtifactV1,
        requests: tuple[SourceItemTextScopeRequestV1, ...],
        request_set_sha256: str,
        context_payload: Mapping[str, Any],
        execution_context: Mapping[str, object] | None,
        request_payload: Mapping[str, Any],
        plans: tuple[SourceItemTextBoundaryPlanV1, ...],
        window: SourceItemTextClassificationWindowV1,
        cancellation_requested: Callable[[], bool],
    ) -> TextClassificationWindowRunResult:
        started = time.monotonic()
        input_payload = build_source_item_text_classification_window_input(
            window=window,
            requests=requests,
            plans=plans,
        )
        input_payload["semantic_context"] = context_payload.get("semantic_context")
        summaries: list[str] = []
        efforts: list[str] = []
        raw_bytes = 0
        previous: Mapping[str, Any] | None = None
        feedback: Mapping[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in (1, 2):
            if cancellation_requested():
                raise LifecycleProbeError("CANONICAL_STAGE_CANCELLED")
            efforts.append(
                self.reasoning_effort
                if window.risk_signals or attempt == 2
                else "medium"
            )
            files = _phase_files(
                document_map=document_map,
                context_payload=context_payload,
                scope_full=request_payload,
                scope_compact=input_payload,
                phase_input={
                    "schema_version": (
                        "source-item-text-classification-phase-input/v1"
                    ),
                    "execution_mode": CLASSIFICATION_EXECUTION_MODE,
                    "attempt": attempt,
                    "classification_window": input_payload,
                },
                previous_candidate=previous,
                validation_feedback=feedback,
                execution_context=execution_context,
            )
            transport = self._invoke_phase(
                runtime=runtime,
                task=task,
                source=source,
                document_map=document_map,
                request_set_sha256=request_set_sha256,
                request_sha256=request_set_sha256,
                scope_id=window.window_id,
                execution_mode=CLASSIFICATION_EXECUTION_MODE,
                files=files,
                task_directive=(
                    "Read the locked direct-text classification window. Return "
                    "First read semantic_context, including the complete associated original table and document/section text. "
                    "Do not classify technical verification conditions as organizational PROCESS or promote an introductory pointer/undefined-criterion notice into a duplicate obligation. "
                    "exactly one canonical A/B/F decision for every immutable "
                    "record_key. Do not merge, split, relabel, reorder, or omit "
                    "records. On attempt 2, correct only this window."
                    + (
                        " Use revision-guidance.json only to interpret the affected "
                        "records; never change their Host-frozen boundaries."
                        if execution_context is not None
                        else ""
                    )
                ),
                cancellation_requested=cancellation_requested,
            )
            summaries.append(_audit_summary_text(transport.tool_activity_summary))
            raw = normalize_classification_payload(
                dict(transport.structured_output)
            )
            raw_bytes += len(_canonical_bytes(raw))
            try:
                candidate = SourceItemTextClassificationAgentCandidateV1.model_validate(
                    raw
                )
                if {item.record_key for item in candidate.decisions} != set(
                    window.record_keys
                ):
                    raise ValueError("SOURCE_ITEM_TEXT_CLASSIFICATION_KEY_SET_INVALID")
                decisions = tuple(
                    sorted(
                        candidate.decisions,
                        key=lambda item: window.record_keys.index(item.record_key),
                    )
                )
                return TextClassificationWindowRunResult(
                    window=window,
                    decisions=decisions,
                    audit_summaries=tuple(summaries),
                    attempt_efforts=tuple(efforts),
                    raw_candidate_bytes=raw_bytes,
                    elapsed_milliseconds=round(
                        (time.monotonic() - started) * 1000
                    ),
                    input_payload=input_payload,
                    diagnostic_codes=tuple(sorted(set(candidate.diagnostic_codes))),
                )
            except (ValidationError, ValueError) as exc:
                last_error = exc
                previous = raw if isinstance(raw, Mapping) else {"value": raw}
                feedback = {
                    "schema_version": (
                        "source-item-text-classification-feedback/v1"
                    ),
                    "window_id": window.window_id,
                    "expected_record_keys": window.record_keys,
                    "failure_code": safe_validation_error(exc),
                    "instruction": (
                        "Return exactly one corrected canonical A/B/F decision "
                        "for every expected record key; do not change boundaries."
                    ),
                }
        raise RuntimeError(
            f"canonical text classification {window.window_id} remained invalid: "
            + safe_validation_error(last_error)
        )

    def _invoke_phase(
        self,
        *,
        runtime: CanaryRuntimeState,
        task: SourceItemTextTaskV1,
        source: Path,
        document_map: DocumentMapArtifactV1,
        request_set_sha256: str,
        request_sha256: str,
        scope_id: str,
        execution_mode: str,
        files: tuple[tuple[str, Mapping[str, Any]], ...],
        task_directive: str,
        cancellation_requested: Callable[[], bool],
    ) -> CanonicalStageTransportResult:
        phase_binding = canonical_sha256(
            {
                "executor_id": self.adapter_id,
                "executor_version": self.adapter_version,
                "source_sha256": document_map.source_sha256,
                "document_map_artifact_sha256": document_map.artifact_sha256,
                "request_set_sha256": request_set_sha256,
                "request_sha256": request_sha256,
                "scope_id": scope_id,
                "execution_mode": execution_mode,
                "files": [
                    {"path": name, "sha256": canonical_sha256(dict(payload))}
                    for name, payload in files
                ],
            }
        )
        stage_request = CanonicalSourceRequirementStageInputRequest(
            execution_mode=execution_mode,  # type: ignore[arg-type]
            source_sha256=document_map.source_sha256,
            document_map_artifact_id=document_map.artifact_id,
            document_map_artifact_sha256=document_map.artifact_sha256,
            request_sha256=request_sha256,
            scope_id=scope_id,
            json_files=files,
        )
        with self._phase_session(
            runtime=runtime,
            source=source,
            document_map=document_map,
            request_set_sha256=request_set_sha256,
            stage_request=stage_request,
        ) as session:
            activation = session.require_inference_stage(
                AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
                execution_mode=execution_mode,
            )
            output_schema = json.loads(
                activation.runner_output_schema_path.read_text(encoding="utf-8")
            )
            expected_schema = (
                boundary_output_schema()
                if execution_mode == BOUNDARY_EXECUTION_MODE
                else classification_output_schema()
            )
            if canonical_sha256(output_schema) != canonical_sha256(expected_schema):
                raise LifecycleProbeError(
                    "CANONICAL_SOURCE_ITEM_TEXT_OUTPUT_SCHEMA_DRIFTED"
                )
            invocation = build_canonical_stage_invocation(
                activation,
                task_binding_sha256=phase_binding,
                turn_output_schema_id=activation.runner_output_schema_id,
                turn_output_schema=output_schema,
                expected_final_artifact_schema_id=(
                    PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION
                ),
                expected_validator_id=PARENT_PROVISION_VALIDATOR_ID,
                expected_validator_version=PARENT_PROVISION_VALIDATOR_VERSION,
                task_directive=task_directive,
            )
            transport = self._transport_factory(
                float(task.timeout_seconds), cancellation_requested
            )
            return transport.invoke(invocation)

    @contextmanager
    def _phase_session(
        self,
        *,
        runtime: CanaryRuntimeState,
        source: Path,
        document_map: DocumentMapArtifactV1,
        request_set_sha256: str,
        stage_request: CanonicalSourceRequirementStageInputRequest,
    ):  # type: ignore[no-untyped-def]
        evidence_binding = canonical_sha256(
            {
                "executor_id": self.adapter_id,
                "executor_version": self.adapter_version,
                "source_sha256": document_map.source_sha256,
                "document_map_artifact_sha256": document_map.artifact_sha256,
                "request_set_sha256": request_set_sha256,
            }
        )
        cache_root = runtime.state_path / "source-item-text-window-evidence-cache"
        cache_path = cache_root / evidence_binding
        evidence_request = CanonicalProductEvidenceRequest(
            source_path=source,
            expected_source_sha256=document_map.source_sha256,
            document_role="CUSTOMER_SOR",
            authorized_corpus_root=source.parent,
            run_id="agent-run-" + evidence_binding[:32],
            opaque_source_handle="source-handle-" + evidence_binding[32:64],
        )
        with self._evidence_cache_lock:
            template = cache_path if cache_path.is_dir() else None
            manager = prepare_canonical_app_server_document_session(
                base_runtime=runtime,
                canonical_kit_root=self._kit_root,
                stages=(AgentExtractionStage.PARENT_PROVISION_RECOGNITION,),
                evidence_request=evidence_request,
                source_requirement_stage_input_request=stage_request,
                repository_root=self._repository_root,
                evidence_workspace_template=template,
            )
            session = manager.__enter__()
            try:
                if template is None:
                    workspace = session.evidence_workspace_path
                    if workspace is None:
                        raise LifecycleProbeError(
                            "CANONICAL_SOURCE_ITEM_TEXT_EVIDENCE_MISSING"
                        )
                    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if cache_root.is_symlink():
                        raise LifecycleProbeError(
                            "CANONICAL_SOURCE_ITEM_TEXT_CACHE_INVALID"
                        )
                    try:
                        shutil.copytree(
                            workspace,
                            cache_path,
                            copy_function=shutil.copy2,
                        )
                    except BaseException:
                        if cache_path.exists():
                            shutil.rmtree(cache_path)
                        raise
            except BaseException:
                manager.__exit__(None, None, None)
                raise
        try:
            yield session
        finally:
            manager.__exit__(None, None, None)


def _validate_task(
    task: SourceItemTextTaskV1,
) -> tuple[
    DocumentMapArtifactV1,
    tuple[SourceItemTextScopeRequestV1, ...],
    Path,
]:
    if (
        not task.project_id.strip()
        or not task.document_id.strip()
        or not task.scope_requests
        or len(task.scope_requests) > 128
        or task.timeout_seconds < 60
        or task.timeout_seconds > 3600
        or task.max_concurrency < 1
        or task.max_concurrency > 2
    ):
        raise ValueError("canonical source-item text task is invalid")
    document_map = DocumentMapArtifactV1.model_validate(task.document_map)
    requests = tuple(
        SourceItemTextScopeRequestV1.model_validate(item)
        for item in task.scope_requests
    )
    scope_ids = tuple(item.scope_id for item in requests)
    if len(scope_ids) != len(set(scope_ids)) or any(
        item.source_sha256 != document_map.source_sha256
        or item.document_map_artifact_id != document_map.artifact_id
        or item.document_map_artifact_sha256 != document_map.artifact_sha256
        for item in requests
    ):
        raise ValueError("canonical source-item text task lineage drifted")
    source = task.source_path.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("canonical source-item text source identity drifted")
    with source.open("rb") as stream:
        source_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    if source_sha256 != document_map.source_sha256:
        raise ValueError("canonical source-item text source identity drifted")
    return document_map, requests, source


def _validated_execution_context(
    value: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    normalized = dict(value)
    if normalized.get("schema_version") != (
        "source-item-scope-revision-agent-context/v2"
    ):
        raise ValueError("canonical source-item text execution context is invalid")
    context_sha256 = str(normalized.get("context_sha256") or "")
    expected = canonical_json_sha256(
        {key: item for key, item in normalized.items() if key != "context_sha256"}
    )
    if context_sha256 != expected or len(_canonical_bytes(normalized)) > 512 * 1024:
        raise ValueError("canonical source-item text execution context drifted")
    return normalized


def _compact_scope(request: SourceItemTextScopeRequestV1) -> dict[str, object]:
    return {
        "schema_version": "source-item-text-compact-scope/v1",
        "scope_id": request.scope_id,
        "node_id": request.node_id,
        "node_label": request.node_label,
        "node_title": request.node_title,
        "ancestor_path": request.ancestor_path,
        "source_content": [
            {
                "content_id": content.content_id,
                "page": content.page,
                "text": content.text,
                "precision": content.precision,
                "is_title": content.is_title,
            }
            for content in request.source_content
        ],
    }


def _phase_files(
    *,
    document_map: DocumentMapArtifactV1,
    context_payload: Mapping[str, Any],
    scope_full: Mapping[str, Any],
    scope_compact: Mapping[str, Any],
    phase_input: Mapping[str, Any],
    previous_candidate: Mapping[str, Any] | None,
    validation_feedback: Mapping[str, Any] | None,
    execution_context: Mapping[str, object] | None = None,
    extra_payloads: tuple[tuple[str, Mapping[str, Any]], ...] = (),
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    values: list[tuple[str, Mapping[str, Any]]] = [
        ("document-context.json", context_payload),
        ("document-map.full.json", document_map.model_dump(mode="json")),
        ("phase-input.json", phase_input),
        ("scope.full.json", scope_full),
        ("scope.json", scope_compact),
        *extra_payloads,
    ]
    if previous_candidate is not None:
        values.append(("previous-candidate.json", previous_candidate))
    if validation_feedback is not None:
        values.append(("validation-feedback.json", validation_feedback))
    if execution_context is not None:
        values.append(("revision-guidance.json", execution_context))
    return tuple(sorted(values, key=lambda item: item[0]))


def _audit_summary_text(summary: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(summary),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _summarize_audit_summaries(
    raw_summaries: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    events: Counter[str] = Counter()
    tools: set[str] = set()
    for raw in raw_summaries:
        try:
            summary = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(summary, Mapping):
            continue
        for family in ("event_counts", "item_event_counts", "unique_item_counts"):
            values = summary.get(family)
            if isinstance(values, Mapping):
                for key, value in values.items():
                    if isinstance(value, int):
                        events[f"{family}:{key}"] += value
        dynamic = summary.get("dynamic_tools")
        calls = dynamic.get("tool_call_counts") if isinstance(dynamic, Mapping) else None
        if isinstance(calls, Mapping):
            tools.update(str(key) for key in calls)
    return (
        tuple(sorted(f"{key}={value}" for key, value in events.items())),
        tuple(sorted(tools)),
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "BOUNDARY_EXECUTION_MODE",
    "CANONICAL_SOURCE_ITEM_TEXT_EXECUTOR_ID",
    "CANONICAL_SOURCE_ITEM_TEXT_EXECUTOR_VERSION",
    "CANONICAL_SOURCE_ITEM_TEXT_TRANSPORT_KIND",
    "CLASSIFICATION_EXECUTION_MODE",
    "LocalCanonicalSourceItemTextExecutor",
]
