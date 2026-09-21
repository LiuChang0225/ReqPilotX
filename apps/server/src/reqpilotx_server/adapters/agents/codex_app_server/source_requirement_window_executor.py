"""Canonical App Server executor for one resumable SourceItem table scope."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import threading
import time
from typing import Any

from pydantic import ValidationError

from reqpilotx_server.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalProductEvidenceRequest,
    CanonicalSourceRequirementStageInputRequest,
    prepare_canonical_app_server_document_session,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageTransportResult,
    build_canonical_stage_invocation,
    canonical_sha256,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_transport import (
    LocalCodexAppServerStructuredStageTransport,
)
from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import (
    CanaryRuntimeState,
    LifecycleProbeError,
    _canary_runtime_state,
    load_protocol_lock,
    verify_protocol_binary_identity,
)
from reqpilotx_server.adapters.agents.codex_app_server.native_source_requirement_reference import (
    NATIVE_SOURCE_REQUIREMENT_REFERENCE_RUNNER_VERSION,
    NativeSourceRequirementRunReceiptV1,
    _compact_document_context,
    _compact_scope_evidence,
    _effective_reasoning_effort,
    _reasoning_risk_signals,
)
from reqpilotx_server.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (
    NativeSourceRequirementWindowedCheckpointV1,
    SourceRequirementBoundaryAgentCandidateV1,
    SourceRequirementBoundaryPlanV1,
    SourceRequirementClassificationAgentCandidateV1,
    SourceRequirementClassificationWindowV1,
    _WindowRunResult,
    _aggregate_candidate,
    _boundary_output_schema,
    _boundary_validation_feedback,
    _build_windowed_checkpoint,
    _classification_output_schema,
    _classification_window_payload,
    _compile_boundary_plan,
    _normalize_boundary_payload,
    _normalize_classification_payload,
    _partition_boundary_plan,
    _safe_windowed_validation_error,
    _validate_resume_checkpoint_identity,
    _window_result_from_checkpoint,
)
from reqpilotx_server.adapters.agents.portable_kit import load_portable_agent_kit
from reqpilotx_server.application.agent_workflow.d3_parent_provisions import (
    PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
    PARENT_PROVISION_VALIDATOR_ID,
    PARENT_PROVISION_VALIDATOR_VERSION,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1,
    SourceRequirementBaselineError,
    SourceRequirementScopeRequestV1,
    canonical_json_sha256,
    validate_source_requirement_slice_candidate,
)
from reqpilotx_server.ports.source_requirement_window_executor import (
    SourceRequirementWindowExecutionV1,
    SourceRequirementWindowTaskV1,
)


CANONICAL_SOURCE_REQUIREMENT_WINDOW_EXECUTOR_ID = (
    "canonical-source-requirement-window"
)
CANONICAL_SOURCE_REQUIREMENT_WINDOW_EXECUTOR_VERSION = "1.2.0"
CANONICAL_SOURCE_REQUIREMENT_TRANSPORT_KIND = "CODEX_APP_SERVER_STRUCTURED_STAGE"

BOUNDARY_EXECUTION_MODE = "SOURCE_REQUIREMENT_BOUNDARY_PLAN"
CLASSIFICATION_EXECUTION_MODE = "SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW"


@dataclass(frozen=True, slots=True)
class _BoundaryPhaseResult:
    plan: SourceRequirementBoundaryPlanV1
    windows: tuple[SourceRequirementClassificationWindowV1, ...]
    audit_summaries: tuple[str, ...]
    attempt_efforts: tuple[str, ...]
    raw_candidate_bytes: int
    elapsed_milliseconds: int


TransportFactory = Callable[
    [float, Callable[[], bool]], LocalCodexAppServerStructuredStageTransport
]
RuntimeStateFactory = Callable[..., AbstractContextManager[CanaryRuntimeState]]


class LocalCanonicalSourceRequirementWindowExecutor:
    """Run business boundaries and A/B/F windows through locked App Server turns."""

    adapter_id = CANONICAL_SOURCE_REQUIREMENT_WINDOW_EXECUTOR_ID
    adapter_version = CANONICAL_SOURCE_REQUIREMENT_WINDOW_EXECUTOR_VERSION
    transport_kind = CANONICAL_SOURCE_REQUIREMENT_TRANSPORT_KIND
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
            verify_protocol_binary_identity(
                self._codex_bin,
                lock=load_protocol_lock(),
            )
        except LifecycleProbeError as exc:
            return str(exc)
        return None

    def execute(
        self,
        task: SourceRequirementWindowTaskV1,
        *,
        resume_checkpoint: Mapping[str, Any] | None,
        checkpoint_sink: Callable[[Mapping[str, Any]], None],
        cancellation_requested: Callable[[], bool],
    ) -> SourceRequirementWindowExecutionV1:
        document_map, request, source = _validate_task(task)
        kit = load_portable_agent_kit(self._kit_root)
        profile = kit.runner_profile("app-server-product")
        if (
            profile.model_id != self.model_id
            or profile.reasoning_effort != self.reasoning_effort
        ):
            raise LifecycleProbeError("CANONICAL_SOURCE_REQUIREMENT_PROFILE_DRIFTED")
        context_payload = _compact_document_context(document_map, request)
        context_payload["semantic_context"] = task.semantic_context or context_payload["semantic_context"]
        scope_payload = _compact_scope_evidence(request)
        visual_pages = tuple(sorted({item.page for item in request.fragments}))
        risk_signals = _reasoning_risk_signals(
            document_map=document_map,
            request=request,
        )
        first_boundary_effort, reasoning_selection = _effective_reasoning_effort(
            request=request,
            requested=self.reasoning_effort,
            execution_profile="compact-adaptive",
            risk_signals=risk_signals,
        )
        checkpoint = (
            None
            if resume_checkpoint is None
            else NativeSourceRequirementWindowedCheckpointV1.model_validate(
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
                _validate_resume_checkpoint_identity(
                    checkpoint,
                    request=request,
                    model_id=self.model_id,
                    reasoning_effort=self.reasoning_effort,
                    codex_version=codex_version,
                    kit_version=kit.identity.kit_version,
                    kit_bundle_sha256=kit.identity.bundle_sha256,
                    visual_source_sha256=request.source_sha256,
                    visual_page_numbers=visual_pages,
                    runner_version=NATIVE_SOURCE_REQUIREMENT_REFERENCE_RUNNER_VERSION,
                )
            return self._execute_with_runtime(
                runtime=runtime,
                task=task,
                source=source,
                document_map=document_map,
                request=request,
                context_payload=context_payload,
                scope_payload=scope_payload,
                checkpoint=checkpoint,
                checkpoint_sink=checkpoint_sink,
                cancellation_requested=cancellation_requested,
                codex_version=codex_version,
                kit_version=kit.identity.kit_version,
                kit_bundle_sha256=kit.identity.bundle_sha256,
                visual_pages=visual_pages,
                risk_signals=risk_signals,
                first_boundary_effort=first_boundary_effort,
                reasoning_selection=reasoning_selection,
                started=started,
            )

    def _execute_with_runtime(
        self,
        *,
        runtime: CanaryRuntimeState,
        task: SourceRequirementWindowTaskV1,
        source: Path,
        document_map: DocumentMapArtifactV1,
        request: SourceRequirementScopeRequestV1,
        context_payload: dict[str, object],
        scope_payload: dict[str, object],
        checkpoint: NativeSourceRequirementWindowedCheckpointV1 | None,
        checkpoint_sink: Callable[[Mapping[str, Any]], None],
        cancellation_requested: Callable[[], bool],
        codex_version: str,
        kit_version: str,
        kit_bundle_sha256: str,
        visual_pages: tuple[int, ...],
        risk_signals: tuple[str, ...],
        first_boundary_effort: str,
        reasoning_selection: str,
        started: float,
    ) -> SourceRequirementWindowExecutionV1:
        if cancellation_requested():
            raise LifecycleProbeError("CANONICAL_STAGE_CANCELLED_BEFORE_START")
        if checkpoint is None:
            boundary = self._run_boundary_phase(
                runtime=runtime,
                task=task,
                source=source,
                document_map=document_map,
                request=request,
                context_payload=context_payload,
                scope_payload=scope_payload,
                first_effort=first_boundary_effort,
                cancellation_requested=cancellation_requested,
            )
            boundary_plan = boundary.plan
            windows = boundary.windows
            boundary_stdout = json.dumps(
                list(boundary.audit_summaries),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            boundary_attempt_efforts = boundary.attempt_efforts
            boundary_bytes = boundary.raw_candidate_bytes
            boundary_elapsed = boundary.elapsed_milliseconds
            prior_classification_elapsed = 0
            window_results: dict[str, _WindowRunResult] = {}
            checkpoint_sink(
                _build_windowed_checkpoint(
                    request=request,
                    model_id=self.model_id,
                    reasoning_effort=self.reasoning_effort,
                    codex_version=codex_version,
                    kit_version=kit_version,
                    kit_bundle_sha256=kit_bundle_sha256,
                    visual_source_sha256=request.source_sha256,
                    visual_page_numbers=visual_pages,
                    boundary_plan=boundary_plan,
                    windows=windows,
                    boundary_stdout=boundary_stdout,
                    boundary_attempt_efforts=boundary_attempt_efforts,
                    boundary_raw_candidate_bytes=boundary_bytes,
                    boundary_elapsed_milliseconds=boundary_elapsed,
                    classification_elapsed_milliseconds=0,
                    window_results=window_results,
                    runner_version=NATIVE_SOURCE_REQUIREMENT_REFERENCE_RUNNER_VERSION,
                ).model_dump(mode="json")
            )
            resumed_elapsed = 0
        else:
            boundary_plan = checkpoint.boundary_plan
            windows = checkpoint.windows
            boundary_stdout = checkpoint.boundary_stdout
            boundary_attempt_efforts = checkpoint.boundary_attempt_efforts
            boundary_bytes = checkpoint.boundary_raw_candidate_bytes
            boundary_elapsed = checkpoint.boundary_elapsed_milliseconds
            prior_classification_elapsed = (
                checkpoint.classification_elapsed_milliseconds
            )
            window_results = {
                item.window.window_id: _window_result_from_checkpoint(item)
                for item in checkpoint.accepted_windows
            }
            resumed_elapsed = boundary_elapsed + prior_classification_elapsed

        classification_started = time.monotonic()
        pending = tuple(
            item for item in windows if item.window_id not in window_results
        )
        failures: list[tuple[str, Exception]] = []
        if pending:
            with ThreadPoolExecutor(
                max_workers=min(task.max_concurrency, len(pending))
            ) as pool:
                futures = {
                    pool.submit(
                        self._run_classification_phase,
                        runtime=runtime,
                        task=task,
                        source=source,
                        document_map=document_map,
                        request=request,
                        context_payload=context_payload,
                        scope_payload=scope_payload,
                        boundary_plan=boundary_plan,
                        window=window,
                        cancellation_requested=cancellation_requested,
                    ): window.window_id
                    for window in pending
                }
                for future in as_completed(futures):
                    window_id = futures[future]
                    try:
                        window_result = future.result()
                        window_results[window_id] = window_result
                        checkpoint_sink(
                            _build_windowed_checkpoint(
                                request=request,
                                model_id=self.model_id,
                                reasoning_effort=self.reasoning_effort,
                                codex_version=codex_version,
                                kit_version=kit_version,
                                kit_bundle_sha256=kit_bundle_sha256,
                                visual_source_sha256=request.source_sha256,
                                visual_page_numbers=visual_pages,
                                boundary_plan=boundary_plan,
                                windows=windows,
                                boundary_stdout=boundary_stdout,
                                boundary_attempt_efforts=(
                                    boundary_attempt_efforts
                                ),
                                boundary_raw_candidate_bytes=boundary_bytes,
                                boundary_elapsed_milliseconds=boundary_elapsed,
                                classification_elapsed_milliseconds=(
                                    prior_classification_elapsed
                                    + round(
                                        (
                                            time.monotonic()
                                            - classification_started
                                        )
                                        * 1000
                                    )
                                ),
                                window_results=window_results,
                                runner_version=(
                                    NATIVE_SOURCE_REQUIREMENT_REFERENCE_RUNNER_VERSION
                                ),
                            ).model_dump(mode="json")
                        )
                    except Exception as exc:
                        failures.append((window_id, exc))
        classification_elapsed = prior_classification_elapsed + round(
            (time.monotonic() - classification_started) * 1000
        )
        if failures:
            failed_ids = ",".join(sorted(item[0] for item in failures))
            raise RuntimeError(
                "canonical source-requirement windows remained invalid: "
                + failed_ids
            ) from failures[0][1]

        candidate = _aggregate_candidate(
            request=request,
            boundary_plan=boundary_plan,
            window_results=window_results,
        )
        validate_source_requirement_slice_candidate(request, candidate)
        candidate_sha256 = canonical_json_sha256(candidate.model_dump(mode="json"))
        ordered_results = tuple(window_results[item.window_id] for item in windows)
        audit_summaries = _checkpoint_audit_summaries(
            boundary_stdout,
            ordered_results=ordered_results,
        )
        event_counts, tool_names = _summarize_audit_summaries(audit_summaries)
        attempts = boundary_attempt_efforts + tuple(
            effort
            for result in ordered_results
            for effort in result.attempt_efforts
        )
        receipt = NativeSourceRequirementRunReceiptV1(
            runner_version=NATIVE_SOURCE_REQUIREMENT_REFERENCE_RUNNER_VERSION,
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            logical_table_scope_id=request.logical_table_scope_id,
            model_id=self.model_id,
            reasoning_effort=self.reasoning_effort,
            codex_version=codex_version,
            kit_version=kit_version,
            kit_bundle_sha256=kit_bundle_sha256,
            visual_source_sha256=request.source_sha256,
            visual_page_numbers=visual_pages,
            elapsed_milliseconds=(
                resumed_elapsed + round((time.monotonic() - started) * 1000)
            ),
            event_counts=event_counts,
            tool_names=tool_names,
            record_count=len(candidate.records),
            context_row_group_count=len(candidate.context_row_groups),
            host_assigned_machine_key_count=len(candidate.records),
            execution_profile="compact-adaptive",
            requested_reasoning_effort=self.reasoning_effort,
            attempt_reasoning_efforts=attempts,
            attempt_count=len(attempts),
            evidence_context_bytes=(
                len(_canonical_bytes(context_payload))
                + len(_canonical_bytes(scope_payload))
            ),
            output_schema_bytes=(
                len(_canonical_bytes(_boundary_output_schema()))
                + len(_canonical_bytes(_classification_output_schema()))
            ),
            raw_candidate_bytes=(
                boundary_bytes
                + sum(item.raw_candidate_bytes for item in ordered_results)
            ),
            evidence_delivery="FILES",
            initial_prompt_bytes=0,
            on_demand_evidence_resources=(
                "document-map.full.json",
                "scope.full.json",
                "source.pdf",
            ),
            reasoning_selection=reasoning_selection,
            reasoning_risk_signals=risk_signals,
            execution_strategy="BOUNDARY_WINDOWS",
            boundary_plan_sha256=boundary_plan.plan_sha256,
            boundary_identity_sha256=boundary_plan.boundary_identity_sha256,
            boundary_elapsed_milliseconds=boundary_elapsed,
            classification_window_count=len(windows),
            classification_attempt_count=sum(
                len(item.attempt_efforts) for item in ordered_results
            ),
            classification_elapsed_milliseconds=classification_elapsed,
            candidate_sha256=candidate_sha256,
        )
        return SourceRequirementWindowExecutionV1(
            candidate=candidate.model_dump(mode="json"),
            run_receipt=receipt.model_dump(mode="json"),
        )

    def _run_boundary_phase(
        self,
        *,
        runtime: CanaryRuntimeState,
        task: SourceRequirementWindowTaskV1,
        source: Path,
        document_map: DocumentMapArtifactV1,
        request: SourceRequirementScopeRequestV1,
        context_payload: dict[str, object],
        scope_payload: dict[str, object],
        first_effort: str,
        cancellation_requested: Callable[[], bool],
    ) -> _BoundaryPhaseResult:
        started = time.monotonic()
        attempts: list[str] = []
        summaries: list[str] = []
        raw_bytes = 0
        previous: Mapping[str, Any] | None = None
        feedback: Mapping[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in (1, 2):
            if cancellation_requested():
                raise LifecycleProbeError("CANONICAL_STAGE_CANCELLED")
            effort = first_effort if attempt == 1 else self.reasoning_effort
            attempts.append(effort)
            files = _phase_files(
                document_map=document_map,
                request=request,
                context_payload=context_payload,
                scope_payload=scope_payload,
                phase_input={
                    "schema_version": "source-requirement-boundary-phase-input/v1",
                    "execution_mode": BOUNDARY_EXECUTION_MODE,
                    "attempt": attempt,
                },
                previous_candidate=previous,
                validation_feedback=feedback,
            )
            transport = self._invoke_phase(
                runtime=runtime,
                task=task,
                source=source,
                request=request,
                execution_mode=BOUNDARY_EXECUTION_MODE,
                files=files,
                task_directive=(
                    "Read the locked source-requirement stage input. Decide the "
                    "complete business-record and context row partition for this "
                    "logical table. Do not classify A/B/F. On attempt 2, correct "
                    "the previous candidate using validation-feedback.json."
                ),
                cancellation_requested=cancellation_requested,
            )
            summaries.append(_audit_summary_text(transport.tool_activity_summary))
            raw = _normalize_boundary_payload(dict(transport.structured_output))
            raw_bytes += len(_canonical_bytes(raw))
            try:
                candidate = SourceRequirementBoundaryAgentCandidateV1.model_validate(
                    raw
                )
                plan = _compile_boundary_plan(
                    document_map=document_map,
                    request=request,
                    candidate=candidate,
                )
                return _BoundaryPhaseResult(
                    plan=plan,
                    windows=_partition_boundary_plan(
                        plan,
                        request=request,
                        document_map=document_map,
                        max_concurrency=task.max_concurrency,
                    ),
                    audit_summaries=tuple(summaries),
                    attempt_efforts=tuple(attempts),
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
                previous = raw
                feedback = _boundary_validation_feedback(exc, request=request)
        raise RuntimeError(
            "canonical source-requirement boundary remained invalid: "
            + _safe_windowed_validation_error(last_error)
        )

    def _run_classification_phase(
        self,
        *,
        runtime: CanaryRuntimeState,
        task: SourceRequirementWindowTaskV1,
        source: Path,
        document_map: DocumentMapArtifactV1,
        request: SourceRequirementScopeRequestV1,
        context_payload: dict[str, object],
        scope_payload: dict[str, object],
        boundary_plan: SourceRequirementBoundaryPlanV1,
        window: SourceRequirementClassificationWindowV1,
        cancellation_requested: Callable[[], bool],
    ) -> _WindowRunResult:
        started = time.monotonic()
        input_payload = _classification_window_payload(
            window=window,
            boundary_plan=boundary_plan,
            request=request,
            context_payload=context_payload,
        )
        first_effort = (
            self.reasoning_effort
            if window.risk_signals
            else "medium"
        )
        efforts: list[str] = []
        summaries: list[str] = []
        raw_bytes = 0
        previous: Mapping[str, Any] | None = None
        feedback: Mapping[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in (1, 2):
            if cancellation_requested():
                raise LifecycleProbeError("CANONICAL_STAGE_CANCELLED")
            effort = first_effort if attempt == 1 else self.reasoning_effort
            efforts.append(effort)
            files = _phase_files(
                document_map=document_map,
                request=request,
                context_payload=context_payload,
                scope_payload=scope_payload,
                phase_input={
                    "schema_version": (
                        "source-requirement-classification-phase-input/v1"
                    ),
                    "execution_mode": CLASSIFICATION_EXECUTION_MODE,
                    "attempt": attempt,
                    "boundary_plan": boundary_plan.model_dump(mode="json"),
                    "classification_window": input_payload,
                },
                previous_candidate=previous,
                validation_feedback=feedback,
            )
            transport = self._invoke_phase(
                runtime=runtime,
                task=task,
                source=source,
                request=request,
                execution_mode=CLASSIFICATION_EXECUTION_MODE,
                files=files,
                task_directive=(
                    "Read the locked source-requirement stage input. Return exactly "
                    "one A/B/F decision for every record key in the classification "
                    "window. Boundaries and labels are immutable. On attempt 2, "
                    "correct only this window using validation-feedback.json."
                ),
                cancellation_requested=cancellation_requested,
            )
            summaries.append(_audit_summary_text(transport.tool_activity_summary))
            raw = _normalize_classification_payload(
                dict(transport.structured_output)
            )
            raw_bytes += len(_canonical_bytes(raw))
            try:
                candidate = (
                    SourceRequirementClassificationAgentCandidateV1.model_validate(
                        raw
                    )
                )
                expected = set(window.record_keys)
                if {item.record_key for item in candidate.decisions} != expected:
                    raise ValueError("CLASSIFICATION_WINDOW_KEY_SET_INVALID")
                return _WindowRunResult(
                    window=window,
                    decisions=tuple(
                        sorted(
                            candidate.decisions,
                            key=lambda item: window.record_keys.index(
                                item.record_key
                            ),
                        )
                    ),
                    stdout_values=tuple(summaries),
                    attempt_efforts=tuple(efforts),
                    raw_candidate_bytes=raw_bytes,
                    elapsed_milliseconds=round(
                        (time.monotonic() - started) * 1000
                    ),
                    input_payload=input_payload,
                    diagnostic_codes=tuple(
                        sorted(set(candidate.diagnostic_codes))
                    ),
                )
            except (ValidationError, ValueError) as exc:
                last_error = exc
                previous = raw
                feedback = {
                    "schema_version": "classification-window-feedback/v1",
                    "window_id": window.window_id,
                    "expected_record_keys": window.record_keys,
                    "failure_code": _safe_windowed_validation_error(exc),
                    "instruction": (
                        "Return exactly one corrected A/B/F decision for every "
                        "expected record key. Do not change boundaries or labels."
                    ),
                }
        raise RuntimeError(
            f"canonical classification {window.window_id} remained invalid: "
            + _safe_windowed_validation_error(last_error)
        )

    @contextmanager
    def _phase_session(
        self,
        *,
        runtime: CanaryRuntimeState,
        source: Path,
        request: SourceRequirementScopeRequestV1,
        stage_request: CanonicalSourceRequirementStageInputRequest,
    ):  # type: ignore[no-untyped-def]
        evidence_binding = canonical_sha256(
            {
                "executor_id": self.adapter_id,
                "executor_version": self.adapter_version,
                "source_sha256": request.source_sha256,
                "document_map_artifact_sha256": (
                    request.document_map_artifact_sha256
                ),
                "request_sha256": request.request_sha256,
            }
        )
        cache_root = runtime.state_path / "source-requirement-evidence-cache"
        cache_path = cache_root / evidence_binding
        evidence_request = CanonicalProductEvidenceRequest(
            source_path=source,
            expected_source_sha256=request.source_sha256,
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
                    source_workspace = session.evidence_workspace_path
                    if source_workspace is None:
                        raise LifecycleProbeError(
                            "CANONICAL_SOURCE_REQUIREMENT_EVIDENCE_MISSING"
                        )
                    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if cache_root.is_symlink():
                        raise LifecycleProbeError(
                            "CANONICAL_SOURCE_REQUIREMENT_EVIDENCE_CACHE_INVALID"
                        )
                    try:
                        shutil.copytree(
                            source_workspace,
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

    def _invoke_phase(
        self,
        *,
        runtime: CanaryRuntimeState,
        task: SourceRequirementWindowTaskV1,
        source: Path,
        request: SourceRequirementScopeRequestV1,
        execution_mode: str,
        files: tuple[tuple[str, Mapping[str, Any]], ...],
        task_directive: str,
        cancellation_requested: Callable[[], bool],
    ) -> CanonicalStageTransportResult:
        phase_binding = canonical_sha256(
            {
                "executor_id": self.adapter_id,
                "executor_version": self.adapter_version,
                "source_sha256": request.source_sha256,
                "document_map_artifact_sha256": (
                    request.document_map_artifact_sha256
                ),
                "request_sha256": request.request_sha256,
                "execution_mode": execution_mode,
                "files": [
                    {"path": name, "sha256": canonical_sha256(dict(payload))}
                    for name, payload in files
                ],
            }
        )
        stage_request = CanonicalSourceRequirementStageInputRequest(
            execution_mode=execution_mode,  # type: ignore[arg-type]
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            scope_id=request.logical_table_scope_id,
            json_files=files,
        )
        with self._phase_session(
            runtime=runtime,
            source=source,
            request=request,
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
                _boundary_output_schema()
                if execution_mode == BOUNDARY_EXECUTION_MODE
                else _classification_output_schema()
            )
            if canonical_sha256(output_schema) != canonical_sha256(expected_schema):
                raise LifecycleProbeError(
                    "CANONICAL_SOURCE_REQUIREMENT_OUTPUT_SCHEMA_DRIFTED"
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
                float(task.timeout_seconds),
                cancellation_requested,
            )
            return transport.invoke(invocation)


def _validate_task(
    task: SourceRequirementWindowTaskV1,
) -> tuple[DocumentMapArtifactV1, SourceRequirementScopeRequestV1, Path]:
    if (
        not task.project_id.strip()
        or not task.document_id.strip()
        or task.timeout_seconds < 60
        or task.timeout_seconds > 3600
        or task.max_concurrency < 1
        or task.max_concurrency > 4
    ):
        raise ValueError("canonical source-requirement task is invalid")
    document_map = DocumentMapArtifactV1.model_validate(task.document_map)
    request = SourceRequirementScopeRequestV1.model_validate(task.scope_request)
    if (
        request.source_sha256 != document_map.source_sha256
        or request.document_map_artifact_id != document_map.artifact_id
        or request.document_map_artifact_sha256 != document_map.artifact_sha256
    ):
        raise ValueError("canonical source-requirement task lineage drifted")
    source = task.source_path.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("canonical source-requirement source identity drifted")
    with source.open("rb") as stream:
        source_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    if source_sha256 != request.source_sha256:
        raise ValueError("canonical source-requirement source identity drifted")
    return document_map, request, source


def _phase_files(
    *,
    document_map: DocumentMapArtifactV1,
    request: SourceRequirementScopeRequestV1,
    context_payload: dict[str, object],
    scope_payload: dict[str, object],
    phase_input: Mapping[str, Any],
    previous_candidate: Mapping[str, Any] | None,
    validation_feedback: Mapping[str, Any] | None,
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    values: list[tuple[str, Mapping[str, Any]]] = [
        ("document-context.json", context_payload),
        ("document-map.full.json", document_map.model_dump(mode="json")),
        ("phase-input.json", phase_input),
        ("scope.full.json", request.model_dump(mode="json")),
        ("scope.json", scope_payload),
    ]
    if previous_candidate is not None:
        values.append(("previous-candidate.json", previous_candidate))
    if validation_feedback is not None:
        values.append(("validation-feedback.json", validation_feedback))
    return tuple(sorted(values, key=lambda item: item[0]))


def _audit_summary_text(summary: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(summary),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _checkpoint_audit_summaries(
    boundary_stdout: str,
    *,
    ordered_results: tuple[_WindowRunResult, ...],
) -> tuple[Mapping[str, Any], ...]:
    values: list[Mapping[str, Any]] = []
    try:
        boundary_values = json.loads(boundary_stdout)
    except json.JSONDecodeError:
        boundary_values = []
    if isinstance(boundary_values, list):
        values.extend(item for item in boundary_values if isinstance(item, Mapping))
    for result in ordered_results:
        for raw in result.stdout_values:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, Mapping):
                values.append(value)
    return tuple(values)


def _summarize_audit_summaries(
    summaries: tuple[Mapping[str, Any], ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    events: Counter[str] = Counter()
    tools: set[str] = set()
    for summary in summaries:
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
    "CANONICAL_SOURCE_REQUIREMENT_TRANSPORT_KIND",
    "CANONICAL_SOURCE_REQUIREMENT_WINDOW_EXECUTOR_ID",
    "CANONICAL_SOURCE_REQUIREMENT_WINDOW_EXECUTOR_VERSION",
    "CLASSIFICATION_EXECUTION_MODE",
    "LocalCanonicalSourceRequirementWindowExecutor",
]
