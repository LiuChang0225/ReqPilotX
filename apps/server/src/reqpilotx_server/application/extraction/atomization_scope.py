from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable

from reqpilotx_server.adapters.documents.local_requirement_atomization_provider import (
    LocalPassThroughRequirementAtomizationProvider,
)
from reqpilotx_server.application.extraction.contracts import RequirementAtomizationScopeRunRequest
from reqpilotx_server.application.extraction.requirement_enrichment import (
    _validated_atomization_obligation_spans,
    build_requirement_atomization_run,
    build_requirement_enrichment_lineage,
)
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.domain.atomization_scope import (
    ATOMIZATION_SCOPE_RUNNING_ATTEMPT_STALE_AFTER_SECONDS,
    AtomizationScopeAttemptStatus,
    AtomizationScopeAttemptV1,
    AtomizationScopeOperation,
    AtomizationScopeReceiptV1,
    AtomizationScopeSkipReason,
    AtomizationScopeSkippedTargetV1,
    AtomizationTargetScope,
    AtomizationUnselectedPolicy,
    canonical_atomization_scope_attempt_sha256,
    canonical_atomization_scope_receipt_sha256,
    stable_atomization_scope_attempt_id,
    stable_atomization_scope_receipt_id,
    stable_atomization_target_scope_sha256,
    stable_effective_entry_classification_sha256,
)
from reqpilotx_server.domain.entry_classification import (
    EntryClassificationSnapshotV1,
    EntryClassificationType,
    canonical_entry_classification_payload_sha256,
    build_initial_entry_classification_snapshot,
    entry_classification_nodes_in_tree_order,
)
from reqpilotx_server.application.extraction.entry_classification_repository import (
    EntryClassificationSnapshotCorrupted,
    get_entry_classification_snapshot_or_initial,
)
from reqpilotx_server.domain.extraction.llm_requirement_review import LlmRequirementReviewSession
from reqpilotx_server.domain.extraction.requirement_enrichment import (
    RequirementAtomizationNode,
    RequirementAtomizationNodeKind,
    RequirementAtomizationRun,
    RequirementEnrichmentCoverageReceipt,
    stable_atomization_node_id,
    stable_atomization_run_id,
)
from reqpilotx_server.domain.extraction.requirement_view import RequirementDecisionMode
from reqpilotx_server.domain.extraction.requirement_tree import (
    RequirementTreeSnapshotV1,
    canonical_requirement_tree_sha256,
)
from reqpilotx_server.domain.extraction.semantics import SemanticClass
from reqpilotx_server.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderPort,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationProviderResult,
)
from reqpilotx_server.ports.requirement_enrichment_repository import (
    AtomizationScopeStateSnapshot,
    RequirementEnrichmentConflictError,
    RequirementEnrichmentRepositoryError,
    RequirementEnrichmentRepositoryPort,
)


_SCOPED_RUN_ALGORITHM_VERSION = (
    "source-bound-provider-atomization/v3+explicit-source-scope/v1"
)
_RUNNING_ATTEMPT_STALE_AFTER = timedelta(
    seconds=ATOMIZATION_SCOPE_RUNNING_ATTEMPT_STALE_AFTER_SECONDS
)


@dataclass(frozen=True, slots=True)
class RequirementAtomizationScopeExecution:
    run: RequirementAtomizationRun
    receipt: AtomizationScopeReceiptV1
    replayed: bool


@dataclass(frozen=True, slots=True)
class AtomizationScopeExecutionIdentity:
    source_sha256: str
    lineage_sha256: str
    review_revision: int
    reviewed_result_sha256: str
    coverage_sha256: str
    entry_classification_snapshot_id: str
    entry_classification_revision: int
    entry_classification_sha256: str
    working_tree_id: str
    working_tree_sha256: str
    effective_structure_id: str


class _AtomizationScopeAttemptHeartbeat:
    def __init__(
        self,
        *,
        repository: RequirementEnrichmentRepositoryPort,
        attempt: AtomizationScopeAttemptV1,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("scope attempt heartbeat interval must be positive")
        self._repository = repository
        self._attempt = attempt
        self._interval_seconds = float(interval_seconds)
        self._stop = Event()
        self._lock = Lock()
        self._error: BaseException | None = None
        self._thread = Thread(
            target=self._run,
            name=f"atomization-scope-heartbeat-{attempt.attempt_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            with self._lock:
                if self._error is None:
                    self._error = RuntimeError(
                        "atomization scope attempt heartbeat did not stop"
                    )

    def assert_owned(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            raise RequirementEnrichmentConflictError(
                "atomization scope attempt lease was lost"
            ) from error

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._repository.heartbeat_atomization_scope_attempt(
                    attempt_id=self._attempt.attempt_id,
                    lineage_sha256=self._attempt.lineage_sha256,
                    expected_scope_revision=(self._attempt.expected_scope_revision),
                )
            except BaseException as exc:
                with self._lock:
                    self._error = exc
                self._stop.set()
                return


@dataclass(frozen=True, slots=True)
class AtomizationScopeStateReadView:
    snapshot: AtomizationScopeStateSnapshot
    attempt_payload: dict[str, Any] | None


class RequirementAtomizationScopeExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        requested_review_node_ids: tuple[str, ...],
        executed_review_node_ids: tuple[str, ...],
        skipped_targets: tuple[AtomizationScopeSkippedTargetV1, ...],
        failed_review_node_ids: tuple[str, ...],
        completed_review_node_ids: tuple[str, ...],
        provider_call_count: int,
        checkpoint_created_count: int,
        checkpoint_reuse_count: int,
        retryable: bool,
        checkpoint_resumable: bool,
    ) -> None:
        super().__init__(message)
        self.requested_review_node_ids = requested_review_node_ids
        self.executed_review_node_ids = executed_review_node_ids
        self.skipped_targets = skipped_targets
        self.failed_review_node_ids = failed_review_node_ids
        self.completed_review_node_ids = completed_review_node_ids
        self.provider_call_count = provider_call_count
        self.checkpoint_created_count = checkpoint_created_count
        self.checkpoint_reuse_count = checkpoint_reuse_count
        self.retryable = retryable
        self.checkpoint_resumable = checkpoint_resumable


class ScopedRequirementAtomizationProvider:
    """Route only explicitly selected review nodes to the remote-capable path."""

    def __init__(
        self,
        *,
        selected_review_node_ids: frozenset[str],
        selected_provider: RequirementAtomizationProviderPort,
        unselected_provider: RequirementAtomizationProviderPort,
    ) -> None:
        self._selected_review_node_ids = selected_review_node_ids
        self._selected_provider = selected_provider
        self._unselected_provider = unselected_provider
        self._lock = Lock()
        self._selected: list[str] = []
        self._pass_through: list[str] = []

    def atomize(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult:
        selected = request.review_node_id in self._selected_review_node_ids
        result = (
            self._selected_provider.atomize(request)
            if selected
            else self._unselected_provider.atomize(request)
        )
        with self._lock:
            target = self._selected if selected else self._pass_through
            if request.review_node_id not in target:
                target.append(request.review_node_id)
        return result

    @property
    def selected_review_node_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._selected)

    @property
    def pass_through_review_node_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._pass_through)


class _CheckpointedSelectedRequirementAtomizationProvider:
    def __init__(
        self,
        *,
        provider: RequirementAtomizationProviderPort,
        repository: RequirementEnrichmentRepositoryPort,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
        scope_attempt_id: str,
        remote_capable_review_node_ids: frozenset[str],
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._binding = {
            "project_id": project_id,
            "document_id": document_id,
            "session_id": session_id,
            "review_revision": review_revision,
            "lineage_sha256": lineage_sha256,
        }
        self._remote_capable_review_node_ids = remote_capable_review_node_ids
        self._scope_attempt_id = scope_attempt_id
        self._lock = Lock()
        self._provider_calls: list[str] = []
        self._checkpoint_created: list[str] = []
        self._checkpoint_reused: list[str] = []
        self._checkpoint_invalidated: list[str] = []
        self._failed: list[str] = []

    def atomize(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult:
        try:
            cached = self._repository.get_atomization_checkpoint(
                **self._binding,
                request=request,
            )
        except RequirementEnrichmentRepositoryError as exc:
            if request.review_node_id not in self._remote_capable_review_node_ids:
                raise RuntimeError(
                    "historical atomization checkpoint envelope is invalid"
                ) from exc
            self._repository.delete_atomization_checkpoint(
                **self._binding,
                request=request,
                scope_attempt_id=self._scope_attempt_id,
            )
            self._record(self._checkpoint_invalidated, request.review_node_id)
            cached = None
        if cached is not None:
            try:
                _validated_atomization_obligation_spans(
                    cached,
                    source_segments=request.source_segments,
                )
            except ValueError:
                if request.review_node_id not in self._remote_capable_review_node_ids:
                    raise RuntimeError("historical atomization checkpoint is invalid")
                self._repository.delete_atomization_checkpoint(
                    **self._binding,
                    request=request,
                    scope_attempt_id=self._scope_attempt_id,
                )
                self._record(self._checkpoint_invalidated, request.review_node_id)
            else:
                self._record(self._checkpoint_reused, request.review_node_id)
                return cached
        if request.review_node_id not in self._remote_capable_review_node_ids:
            raise RuntimeError("historical atomization checkpoint is unavailable")
        self._record(self._provider_calls, request.review_node_id)
        try:
            result = self._provider.atomize(request)
            _validated_atomization_obligation_spans(
                result,
                source_segments=request.source_segments,
            )
            stored = self._repository.create_or_get_atomization_checkpoint(
                **self._binding,
                request=request,
                result=result,
                scope_attempt_id=self._scope_attempt_id,
            )
            _validated_atomization_obligation_spans(
                stored,
                source_segments=request.source_segments,
            )
        except Exception:
            self._record(self._failed, request.review_node_id)
            raise
        self._record(self._checkpoint_created, request.review_node_id)
        return stored

    def _record(self, target: list[str], review_node_id: str) -> None:
        with self._lock:
            if review_node_id not in target:
                target.append(review_node_id)

    @property
    def provider_call_review_node_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._provider_calls)

    @property
    def checkpoint_created_review_node_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._checkpoint_created)

    @property
    def checkpoint_reused_review_node_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._checkpoint_reused)

    @property
    def checkpoint_invalidated_review_node_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._checkpoint_invalidated)

    @property
    def failed_review_node_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._failed)


class _LazyRequirementAtomizationProvider:
    def __init__(
        self,
        factory: Callable[[], RequirementAtomizationProviderPort],
    ) -> None:
        self._factory = factory
        self._provider: RequirementAtomizationProviderPort | None = None
        self._lock = Lock()

    def atomize(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult:
        with self._lock:
            if self._provider is None:
                self._provider = self._factory()
            provider = self._provider
        return provider.atomize(request)


def create_requirement_atomization_scope_run(
    project_id: str,
    document_id: str,
    session_id: str,
    payload: RequirementAtomizationScopeRunRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    """Transport-facing W4 orchestration, kept outside the protected use case."""

    from reqpilotx_server.application.extraction import enrichment_workspace
    from reqpilotx_server.application.extraction.llm_provision_batch import (
        MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    enrichment_workspace._reject_requirement_enrichment_during_structure_optimization(
        project_id,
        document_id,
    )
    _, session = enrichment_workspace._require_requirement_enrichment_session(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
        expected_review_revision=payload.expected_review_revision,
    )
    coverage = enrichment_workspace._require_current_requirement_enrichment_coverage(
        session
    )
    try:
        working_tree, classification = load_current_entry_classification_context(
            project_id,
            document_id,
        )
    except EntryClassificationSnapshotCorrupted as exc:
        raise enrichment_workspace._requirement_enrichment_http_error(
            status_code=409,
            code="requirement_atomization_classification_corrupted",
            message="The current entry classification receipt could not be verified.",
        ) from exc
    repository = enrichment_workspace._requirement_enrichment_repository()
    lineage = build_requirement_enrichment_lineage(session, coverage=coverage)

    def load_current_execution_identity() -> AtomizationScopeExecutionIdentity:
        _, latest_session = (
            enrichment_workspace._require_requirement_enrichment_session(
                project_id=project_id,
                document_id=document_id,
                session_id=session_id,
                expected_review_revision=payload.expected_review_revision,
            )
        )
        latest_coverage = (
            enrichment_workspace._require_current_requirement_enrichment_coverage(
                latest_session
            )
        )
        latest_tree, latest_classification = load_current_entry_classification_context(
            project_id, document_id
        )
        return build_atomization_scope_execution_identity(
            session=latest_session,
            coverage=latest_coverage,
            classification_snapshot=latest_classification,
            working_tree=latest_tree,
        )

    response.headers.update(_ASSET_RESPONSE_HEADERS)
    try:
        with enrichment_workspace._requirement_atomization_lineage_lock(
            lineage.lineage_sha256
        ):
            enrichment_workspace._set_requirement_atomization_active(
                lineage.lineage_sha256,
                active=True,
            )
            try:
                execution = execute_requirement_atomization_scope(
                    project_id=project_id,
                    document_id=document_id,
                    session=session,
                    coverage=coverage,
                    classification_snapshot=classification,
                    working_tree=working_tree,
                    payload=payload,
                    repository=repository,
                    remote_provider=_LazyRequirementAtomizationProvider(
                        lambda: enrichment_workspace._build_requirement_atomization_execution_provider(
                            project_id,
                            document_id,
                        )
                    ),
                    max_concurrent_provider_calls=(MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS),
                    current_execution_identity_loader=(load_current_execution_identity),
                )
            finally:
                enrichment_workspace._set_requirement_atomization_active(
                    lineage.lineage_sha256,
                    active=False,
                )
    except RequirementAtomizationScopeExecutionError as exc:
        raise enrichment_workspace._requirement_enrichment_http_error(
            status_code=503,
            code="requirement_atomization_scope_interrupted",
            message=str(exc),
            detail={
                "requested_review_node_ids": list(exc.requested_review_node_ids),
                "executed_review_node_ids": list(exc.executed_review_node_ids),
                "skipped_targets": [
                    item.model_dump(mode="json") for item in exc.skipped_targets
                ],
                "failed_review_node_ids": list(exc.failed_review_node_ids),
                "completed_review_node_ids": list(exc.completed_review_node_ids),
                "completed_checkpoint_count": len(exc.completed_review_node_ids),
                "provider_call_count": exc.provider_call_count,
                "checkpoint_created_count": exc.checkpoint_created_count,
                "checkpoint_reuse_count": exc.checkpoint_reuse_count,
                "retryable": exc.retryable,
                "checkpoint_resumable": exc.checkpoint_resumable,
                "resumable": exc.checkpoint_resumable,
            },
        ) from exc
    except RequirementEnrichmentConflictError as exc:
        raise enrichment_workspace._requirement_enrichment_http_error(
            status_code=409,
            code="requirement_atomization_scope_conflict",
            message=str(exc),
        ) from exc
    except EntryClassificationSnapshotCorrupted as exc:
        raise enrichment_workspace._requirement_enrichment_http_error(
            status_code=409,
            code="requirement_atomization_classification_corrupted",
            message="The current entry classification receipt could not be verified.",
        ) from exc
    except ValueError as exc:
        message = str(exc)
        conflict = any(
            marker in message
            for marker in (
                "has changed",
                "binding is stale",
                "snapshot hash is invalid",
                "no revision to undo",
            )
        )
        raise enrichment_workspace._requirement_enrichment_http_error(
            status_code=409 if conflict else 422,
            code=(
                "requirement_atomization_scope_stale"
                if conflict
                else "requirement_atomization_scope_invalid"
            ),
            message=message,
        ) from exc
    except RequirementEnrichmentRepositoryError as exc:
        raise enrichment_workspace._requirement_enrichment_http_error(
            status_code=422,
            code="requirement_atomization_scope_persistence_invalid",
            message=str(exc),
        ) from exc
    return {
        "schema_version": "requirement-atomization-run-response/v2",
        "project_id": project_id,
        "document_id": document_id,
        "serving_impact": "NONE",
        "replayed": execution.replayed,
        "atomization_run": execution.run.model_dump(mode="json"),
        "scope_receipt": execution.receipt.model_dump(mode="json"),
    }


def load_current_entry_classification_snapshot(
    project_id: str,
    document_id: str,
) -> EntryClassificationSnapshotV1:
    return load_current_entry_classification_context(project_id, document_id)[1]


def load_current_entry_classification_context(
    project_id: str,
    document_id: str,
) -> tuple[RequirementTreeSnapshotV1, EntryClassificationSnapshotV1]:
    from reqpilotx_server.application.extraction.entry_classification import (
        _read_current_requirement_tree,
    )

    tree = _read_current_requirement_tree(project_id, document_id)
    initial = build_initial_entry_classification_snapshot(tree)
    return tree, get_entry_classification_snapshot_or_initial(initial)


def _load_current_requirement_tree_for_state_read(
    project_id: str,
    document_id: str,
    document: dict[str, Any],
) -> RequirementTreeSnapshotV1 | None:
    """Return only a proven-current cached WorkingTree; never rebuild Structure."""

    from reqpilotx_server.adapters.documents.pdf_source_ir_adapter import (
        PDF_SOURCE_IR_ADAPTER_VERSION,
    )
    from reqpilotx_server.application.errors import ApplicationError
    from reqpilotx_server.application.extraction.document_jobs import (
        _document_extraction_result_artifact_receipt_identity,
    )
    from reqpilotx_server.application.extraction.resolve_structure import (
        DEFAULT_STRUCTURE_RESOLVER_VERSION,
    )
    from reqpilotx_server.application.extraction.requirement_tree import (
        REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION,
    )
    from reqpilotx_server.application.extraction.serialize_structure_workbench import (
        STRUCTURE_WORKBENCH_SCHEMA_VERSION,
    )
    from reqpilotx_server.application.extraction.structure_presentation import (
        STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION,
    )
    from reqpilotx_server.application.extraction.structure_response_cache import (
        build_static_structure_response_identity,
        get_structure_response_cache_candidate,
    )
    from reqpilotx_server.application.extraction.structure_workspace import (
        _structure_agent_job_payload,
        _structure_response_document_identity,
        _structure_table_projection_revision_state,
    )

    try:
        source_sha256 = str(document.get("source_sha256") or "")
        artifact_receipt = _document_extraction_result_artifact_receipt_identity(
            project_id,
            document_id,
            source_sha256=source_sha256,
        )
        if not artifact_receipt:
            return None
        static_identity = build_static_structure_response_identity(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_receipt=artifact_receipt,
            document_identity=_structure_response_document_identity(document),
            source_ir_adapter_version=PDF_SOURCE_IR_ADAPTER_VERSION,
            structure_resolver_version=DEFAULT_STRUCTURE_RESOLVER_VERSION,
            structure_presentation_schema_version=(
                STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION
            ),
            workbench_schema_version=STRUCTURE_WORKBENCH_SCHEMA_VERSION,
            requirement_tree_projection_runtime_version=(
                REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
            ),
            requirement_mode="WORKING",
            requirement_set_id="",
        )
        candidate = get_structure_response_cache_candidate(static_identity)
        if candidate is None:
            return None
        _job, _payload, agent = _structure_agent_job_payload(
            project_id=project_id,
            document_id=document_id,
            document=document,
            require_final=False,
        )
        effective_identity = (
            agent.get("effective_structure_identity")
            if isinstance(agent.get("effective_structure_identity"), dict)
            else {}
        )
        effective_structure_id = str(effective_identity.get("snapshot_id") or "")
        _heads, table_revision_sha256 = _structure_table_projection_revision_state(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            structure_snapshot_id=effective_structure_id,
        )
        if (
            candidate.effective_structure_snapshot_id != effective_structure_id
            or candidate.table_projection_revision_sha256 != table_revision_sha256
        ):
            return None
        response_payload = json.loads(candidate.content)
        workbench = response_payload.get("structure_workbench")
        raw_tree = (
            workbench.get("requirement_tree") if isinstance(workbench, dict) else None
        )
        tree = RequirementTreeSnapshotV1.model_validate(raw_tree)
    except (ApplicationError, TypeError, ValueError):
        return None
    if (
        tree.mode.value != "WORKING"
        or tree.state.value != "READY"
        or tree.source_identity.project_id != project_id
        or tree.source_identity.document_id != document_id
        or tree.source_identity.source_sha256 != source_sha256
        or tree.structure_identity.effective_snapshot_id != effective_structure_id
    ):
        return None
    return tree


def build_atomization_scope_state_read_view(
    snapshot: AtomizationScopeStateSnapshot,
    *,
    lineage_active: bool,
    observed_at_utc: datetime | None = None,
) -> AtomizationScopeStateReadView:
    attempt = snapshot.attempt
    attempt_payload = attempt.model_dump(mode="json") if attempt is not None else None
    observed_at = observed_at_utc or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("scope state observation time must be timezone-aware")
    if (
        attempt is not None
        and attempt.status is AtomizationScopeAttemptStatus.RUNNING
        and not lineage_active
        and observed_at - attempt.updated_at_utc >= _RUNNING_ATTEMPT_STALE_AFTER
    ):
        assert attempt_payload is not None
        resumable_checkpoint_ids = set(
            getattr(snapshot, "valid_checkpoint_review_node_ids", ())
        )
        attempt_payload.update(
            {
                "status": AtomizationScopeAttemptStatus.INTERRUPTED.value,
                "persisted_status": AtomizationScopeAttemptStatus.RUNNING.value,
                "retryable": True,
                "checkpoint_resumable": bool(
                    resumable_checkpoint_ids & set(attempt.executed_review_node_ids)
                ),
                "error_code": "PROCESS_INTERRUPTED",
                "read_projection": "ABANDONED_RUNNING_ATTEMPT",
            }
        )
    return AtomizationScopeStateReadView(
        snapshot=snapshot,
        attempt_payload=attempt_payload,
    )


def serialize_requirement_atomization_scope_state(
    *,
    session: LlmRequirementReviewSession,
    coverage: RequirementEnrichmentCoverageReceipt | None,
    classification_snapshot: EntryClassificationSnapshotV1 | None,
    repository: RequirementEnrichmentRepositoryPort,
    read_view: AtomizationScopeStateReadView | None = None,
) -> dict[str, Any]:
    receipt = None
    attempt_payload = None
    if coverage is not None and coverage.eligible:
        lineage = build_requirement_enrichment_lineage(session, coverage=coverage)
        if read_view is None:
            from reqpilotx_server.application.extraction import enrichment_workspace

            snapshot = repository.get_atomization_scope_state_snapshot(
                project_id=session.project_id,
                document_id=session.document_id,
                session_id=session.session_id,
                review_revision=session.reviewed_result.revision,
                lineage_sha256=lineage.lineage_sha256,
            )
            read_view = build_atomization_scope_state_read_view(
                snapshot,
                lineage_active=(
                    enrichment_workspace._requirement_atomization_is_active(
                        lineage.lineage_sha256
                    )
                ),
            )
        receipt = read_view.snapshot.receipt
        attempt_payload = read_view.attempt_payload
    current_content_sha = (
        stable_effective_entry_classification_sha256(classification_snapshot)
        if classification_snapshot is not None
        else ""
    )
    classification_by_id = (
        {item.workflow_node_id: item for item in classification_snapshot.entries}
        if classification_snapshot is not None
        else {}
    )
    eligible_review_node_ids = tuple(
        item.review_node_id
        for item in session.reviewed_result.nodes
        if item.include_in_view
        and item.semantic_class is SemanticClass.REQUIREMENT
        and item.decision_mode is RequirementDecisionMode.DIRECT
        and item.review_node_id in classification_by_id
        and classification_by_id[item.review_node_id].effective_type
        is EntryClassificationType.TECHNICAL
    )
    if classification_snapshot is None:
        binding_state = "CLASSIFICATION_UNAVAILABLE"
    elif receipt is None:
        binding_state = "NOT_STARTED"
    elif (
        receipt.entry_classification_snapshot_id == classification_snapshot.snapshot_id
        and receipt.entry_classification_sha256
        == classification_snapshot.payload_sha256
    ):
        binding_state = "CURRENT"
    elif receipt.entry_classification_content_sha256 == current_content_sha:
        binding_state = "CONTENT_EQUIVALENT"
    else:
        binding_state = "STALE"
    current_revision = receipt.scope_revision if receipt is not None else 0
    return {
        "schema_version": "requirement-atomization-scope-state/v1",
        "capability": {
            "schema_version": "requirement-atomization-scope-capability/v1",
            "available": bool(
                coverage is not None
                and coverage.eligible
                and classification_snapshot is not None
            ),
            "explicit_trigger_required": True,
            "read_has_side_effects": False,
            "operations": ["APPLY", "UNDO"],
            "scopes": ["CURRENT", "SELECTED", "BRANCH", "ALL"],
            "unselected_policy": "SOURCE_BOUND_PASSTHROUGH",
        },
        "current_scope_revision": current_revision,
        "eligible_review_node_ids": list(eligible_review_node_ids),
        "eligible_review_node_count": len(eligible_review_node_ids),
        "classification_binding_state": binding_state,
        "classification_snapshot_id": (
            classification_snapshot.snapshot_id
            if classification_snapshot is not None
            else ""
        ),
        "classification_revision": (
            classification_snapshot.revision
            if classification_snapshot is not None
            else 0
        ),
        "classification_sha256": (
            classification_snapshot.payload_sha256
            if classification_snapshot is not None
            else ""
        ),
        "classification_content_sha256": current_content_sha,
        "can_undo": bool(receipt is not None and receipt.active_revision > 0),
        "current_atomization_run_id": (
            receipt.atomization_run_id if receipt is not None else ""
        ),
        "current_atomization_run_revision": (
            receipt.atomization_run_revision if receipt is not None else 0
        ),
        "current_receipt": (
            receipt.model_dump(mode="json") if receipt is not None else None
        ),
        "last_attempt": (attempt_payload if attempt_payload is not None else None),
    }


def execute_requirement_atomization_scope(
    *,
    project_id: str,
    document_id: str,
    session: LlmRequirementReviewSession,
    coverage: RequirementEnrichmentCoverageReceipt,
    classification_snapshot: EntryClassificationSnapshotV1,
    working_tree: RequirementTreeSnapshotV1 | None = None,
    payload: RequirementAtomizationScopeRunRequest,
    repository: RequirementEnrichmentRepositoryPort,
    remote_provider: RequirementAtomizationProviderPort,
    max_concurrent_provider_calls: int,
    current_classification_loader: (
        Callable[[], EntryClassificationSnapshotV1] | None
    ) = None,
    current_execution_identity_loader: (
        Callable[[], AtomizationScopeExecutionIdentity] | None
    ) = None,
    lease_heartbeat_interval_seconds: float = (
        ATOMIZATION_SCOPE_RUNNING_ATTEMPT_STALE_AFTER_SECONDS / 3
    ),
) -> RequirementAtomizationScopeExecution:
    """Append one explicit scope revision without changing the core atomizer."""

    _validate_scope_binding(
        project_id=project_id,
        document_id=document_id,
        session=session,
        classification_snapshot=classification_snapshot,
        working_tree=working_tree,
        payload=payload,
    )
    expected_target_hash = stable_atomization_target_scope_sha256(
        project_id=project_id,
        document_id=document_id,
        session_id=session.session_id,
        command_id=payload.command_id,
        operation=payload.operation,
        scope=payload.scope,
        expected_review_revision=payload.expected_review_revision,
        expected_scope_revision=payload.expected_scope_revision,
        expected_entry_classification_snapshot_id=(
            payload.expected_entry_classification_snapshot_id
        ),
        expected_entry_classification_revision=(
            payload.expected_entry_classification_revision
        ),
        expected_entry_classification_sha256=(
            payload.expected_entry_classification_sha256
        ),
        expected_working_tree_id=payload.expected_working_tree_id,
        expected_working_tree_sha256=payload.expected_working_tree_sha256,
        expected_effective_structure_id=payload.expected_effective_structure_id,
        anchor_tree_node_id=payload.anchor_tree_node_id,
        target_review_node_ids=payload.target_review_node_ids,
        unselected_policy=payload.unselected_policy,
    )
    if payload.target_scope_sha256 != expected_target_hash:
        raise ValueError("atomization target scope hash does not match exact targets")
    _validate_canonical_target_scope(
        classification_snapshot=classification_snapshot,
        working_tree=working_tree,
        payload=payload,
    )
    lineage = build_requirement_enrichment_lineage(session, coverage=coverage)
    expected_execution_identity = (
        build_atomization_scope_execution_identity(
            session=session,
            coverage=coverage,
            classification_snapshot=classification_snapshot,
            working_tree=working_tree,
        )
        if working_tree is not None
        else None
    )
    if (
        current_execution_identity_loader is not None
        and expected_execution_identity is None
    ):
        raise ValueError("execution identity guard requires the current working tree")
    replay = repository.get_atomization_scope_command_result(
        project_id=project_id,
        document_id=document_id,
        session_id=session.session_id,
        lineage_sha256=lineage.lineage_sha256,
        command_id=payload.command_id,
        command_sha256=expected_target_hash,
    )
    if replay is not None:
        receipt, run = replay
        return RequirementAtomizationScopeExecution(
            run=run,
            receipt=receipt,
            replayed=True,
        )
    current = repository.get_latest_atomization_scope_receipt(
        project_id=project_id,
        document_id=document_id,
        session_id=session.session_id,
        lineage_sha256=lineage.lineage_sha256,
    )
    current_revision = current.scope_revision if current is not None else 0
    if current_revision != payload.expected_scope_revision:
        raise ValueError("atomization scope revision has changed")

    entries_by_id = {
        item.workflow_node_id: item for item in classification_snapshot.entries
    }
    unknown = tuple(
        item for item in payload.target_review_node_ids if item not in entries_by_id
    )
    if unknown:
        raise ValueError(f"atomization request contains unknown target: {unknown[0]}")
    reviewed_by_id = {
        item.review_node_id: item for item in session.reviewed_result.nodes
    }
    direct_ids = tuple(
        item.review_node_id
        for item in session.reviewed_result.nodes
        if item.include_in_view
        and item.semantic_class is SemanticClass.REQUIREMENT
        and item.decision_mode is RequirementDecisionMode.DIRECT
    )
    eligible_ids = frozenset(
        review_node_id
        for review_node_id in direct_ids
        if (
            review_node_id in entries_by_id
            and entries_by_id[review_node_id].effective_type
            is EntryClassificationType.TECHNICAL
        )
    )
    previous_targets = tuple(
        review_node_id
        for review_node_id in (
            current.cumulative_review_node_ids if current is not None else ()
        )
        if review_node_id in eligible_ids
    )
    skipped: list[AtomizationScopeSkippedTargetV1] = []
    executed: list[str] = []
    if payload.operation == "APPLY":
        for review_node_id in payload.target_review_node_ids:
            entry = entries_by_id[review_node_id]
            reviewed = reviewed_by_id.get(review_node_id)
            reason = _scope_skip_reason(entry.effective_type, reviewed)
            if reason is not None:
                skipped.append(
                    AtomizationScopeSkippedTargetV1(
                        review_node_id=review_node_id,
                        reason=reason,
                    )
                )
            else:
                executed.append(review_node_id)
        canonical_workflow_order = tuple(
            item.workflow_node_id for item in classification_snapshot.entries
        )
        executed_set = set(executed)
        executed = [
            review_node_id
            for review_node_id in canonical_workflow_order
            if review_node_id in executed_set
        ]
        skipped_by_id = {item.review_node_id: item for item in skipped}
        skipped = [
            skipped_by_id[review_node_id]
            for review_node_id in canonical_workflow_order
            if review_node_id in skipped_by_id
        ]
        cumulative_set = {*previous_targets, *executed}
        cumulative = tuple(item for item in direct_ids if item in cumulative_set)
        active_revision = current_revision + 1
        previous_active_revision = current.active_revision if current is not None else 0
    else:
        if current is None:
            raise ValueError("atomization scope has no revision to undo")
        target_revision = current.previous_active_revision
        target_receipt = (
            repository.get_atomization_scope_receipt_revision(
                project_id=project_id,
                document_id=document_id,
                session_id=session.session_id,
                lineage_sha256=lineage.lineage_sha256,
                scope_revision=target_revision,
            )
            if target_revision > 0
            else None
        )
        cumulative = tuple(
            item
            for item in (
                target_receipt.cumulative_review_node_ids
                if target_receipt is not None
                else ()
            )
            if item in eligible_ids
        )
        active_revision = target_revision
        previous_active_revision = (
            target_receipt.previous_active_revision if target_receipt is not None else 0
        )

    latest_attempt = repository.get_latest_atomization_scope_attempt(
        project_id=project_id,
        document_id=document_id,
        session_id=session.session_id,
        lineage_sha256=lineage.lineage_sha256,
    )
    attempt_sequence = (
        latest_attempt.attempt_sequence + 1 if latest_attempt is not None else 1
    )
    attempt_started_at = datetime.now(timezone.utc)
    running_attempt = _build_scope_attempt(
        project_id=project_id,
        document_id=document_id,
        session=session,
        payload=payload,
        lineage_sha256=lineage.lineage_sha256,
        attempt_sequence=attempt_sequence,
        requested=payload.target_review_node_ids,
        executed=tuple(executed),
        skipped=tuple(skipped),
        cumulative=cumulative,
        status=AtomizationScopeAttemptStatus.RUNNING,
        completed=(),
        failed=(),
        checkpoint_created=(),
        checkpoint_reused=(),
        checkpoint_invalidated=(),
        provider_call_count=0,
        retryable=False,
        error_code="",
        run=None,
        receipt=None,
        started_at=attempt_started_at,
        updated_at=attempt_started_at,
    )
    repository.begin_atomization_scope_attempt(
        attempt=running_attempt,
        expected_scope_revision=payload.expected_scope_revision,
    )
    heartbeat = _AtomizationScopeAttemptHeartbeat(
        repository=repository,
        attempt=running_attempt,
        interval_seconds=lease_heartbeat_interval_seconds,
    )
    heartbeat.start()

    selected_provider = _CheckpointedSelectedRequirementAtomizationProvider(
        provider=remote_provider,
        repository=repository,
        project_id=project_id,
        document_id=document_id,
        session_id=session.session_id,
        review_revision=session.reviewed_result.revision,
        lineage_sha256=lineage.lineage_sha256,
        scope_attempt_id=running_attempt.attempt_id,
        remote_capable_review_node_ids=frozenset(executed),
    )
    provider = ScopedRequirementAtomizationProvider(
        selected_review_node_ids=frozenset(cumulative),
        selected_provider=selected_provider,
        unselected_provider=LocalPassThroughRequirementAtomizationProvider(),
    )
    try:
        if payload.operation == "UNDO" and target_receipt is not None:
            target_run = repository.get_atomization(
                project_id=project_id,
                document_id=document_id,
                session_id=session.session_id,
                atomization_run_id=target_receipt.atomization_run_id,
            )
            if target_run is None or target_run.lineage != lineage:
                raise RuntimeError("undo target atomization run is unavailable")
            base_run = target_run
        else:
            base_run = build_requirement_atomization_run(
                session,
                coverage=coverage,
                provider=provider,
                max_concurrent_provider_calls=max_concurrent_provider_calls,
            )
    except Exception as exc:
        heartbeat.stop()
        heartbeat.assert_owned()
        interrupted = _interrupted_scope_attempt(
            running_attempt,
            selected_provider=selected_provider,
            error=exc,
        )
        repository.update_atomization_scope_attempt(
            attempt=interrupted,
            expected_status=AtomizationScopeAttemptStatus.RUNNING,
        )
        raise RequirementAtomizationScopeExecutionError(
            str(exc),
            requested_review_node_ids=payload.target_review_node_ids,
            executed_review_node_ids=tuple(executed),
            skipped_targets=tuple(skipped),
            failed_review_node_ids=selected_provider.failed_review_node_ids,
            completed_review_node_ids=interrupted.completed_review_node_ids,
            provider_call_count=len(selected_provider.provider_call_review_node_ids),
            checkpoint_created_count=len(
                selected_provider.checkpoint_created_review_node_ids
            ),
            checkpoint_reuse_count=len(
                selected_provider.checkpoint_reused_review_node_ids
            ),
            retryable=interrupted.retryable,
            checkpoint_resumable=interrupted.checkpoint_resumable,
        ) from exc
    except BaseException:
        heartbeat.stop()
        raise

    scope_revision = current_revision + 1
    run = (
        _copy_scope_run_revision(base_run, revision=scope_revision)
        if payload.operation == "UNDO" and target_receipt is not None
        else _build_scope_revision_run(
            base_run,
            session=session,
            revision=scope_revision,
        )
    )
    requested = payload.target_review_node_ids
    executed_tuple = tuple(executed)
    previous_set = set(previous_targets)
    new_ids = tuple(item for item in executed_tuple if item not in previous_set)
    reused_ids = tuple(item for item in executed_tuple if item in previous_set)
    pass_through = tuple(item for item in direct_ids if item not in set(cumulative))
    receipt = _build_scope_receipt(
        project_id=project_id,
        document_id=document_id,
        session=session,
        classification_snapshot=classification_snapshot,
        payload=payload,
        lineage_sha256=lineage.lineage_sha256,
        scope_revision=scope_revision,
        active_revision=active_revision,
        previous_active_revision=previous_active_revision,
        supersedes_receipt_id=current.receipt_id if current is not None else "",
        run=run,
        requested=requested,
        executed=executed_tuple,
        new_ids=new_ids,
        reused_ids=reused_ids,
        skipped=tuple(skipped),
        pass_through=pass_through,
        cumulative=cumulative,
        selected_provider=selected_provider,
        created_at=datetime.now(timezone.utc),
    )
    succeeded_attempt = _build_scope_attempt(
        project_id=project_id,
        document_id=document_id,
        session=session,
        payload=payload,
        lineage_sha256=lineage.lineage_sha256,
        attempt_sequence=attempt_sequence,
        requested=requested,
        executed=executed_tuple,
        skipped=tuple(skipped),
        cumulative=cumulative,
        status=AtomizationScopeAttemptStatus.SUCCEEDED,
        completed=_completed_scope_attempt_ids(
            selected_provider,
            executed_review_node_ids=executed_tuple,
        ),
        failed=(),
        checkpoint_created=_ordered_review_node_ids(
            cumulative,
            selected_provider.checkpoint_created_review_node_ids,
        ),
        checkpoint_reused=_ordered_review_node_ids(
            cumulative,
            selected_provider.checkpoint_reused_review_node_ids,
        ),
        checkpoint_invalidated=(
            _ordered_review_node_ids(
                cumulative,
                selected_provider.checkpoint_invalidated_review_node_ids,
            )
        ),
        provider_call_count=len(selected_provider.provider_call_review_node_ids),
        retryable=False,
        error_code="",
        run=run,
        receipt=receipt,
        started_at=attempt_started_at,
        updated_at=datetime.now(timezone.utc),
    )
    committed = False
    try:
        heartbeat.stop()
        heartbeat.assert_owned()
        if current_execution_identity_loader is not None:
            _assert_current_execution_identity(
                expected_execution_identity,
                current_execution_identity_loader(),
            )
        if current_classification_loader is not None:
            latest_classification = current_classification_loader()
            if (
                latest_classification.snapshot_id != classification_snapshot.snapshot_id
                or latest_classification.revision != classification_snapshot.revision
                or latest_classification.payload_sha256
                != classification_snapshot.payload_sha256
            ):
                raise ValueError("entry classification snapshot has changed")
        stored_run, stored_receipt, created = (
            repository.create_atomization_scope_revision(
                run=run,
                receipt=receipt,
                attempt=succeeded_attempt,
                expected_scope_revision=payload.expected_scope_revision,
            )
        )
        committed = True
        if current_execution_identity_loader is not None:
            _assert_current_execution_identity(
                expected_execution_identity,
                current_execution_identity_loader(),
            )
    except Exception as exc:
        if not committed:
            repository.update_atomization_scope_attempt(
                attempt=_interrupted_scope_attempt(
                    running_attempt,
                    selected_provider=selected_provider,
                    error=exc,
                ),
                expected_status=AtomizationScopeAttemptStatus.RUNNING,
            )
        raise
    return RequirementAtomizationScopeExecution(
        run=stored_run,
        receipt=stored_receipt,
        replayed=not created,
    )


def _validate_scope_binding(
    *,
    project_id: str,
    document_id: str,
    session: LlmRequirementReviewSession,
    classification_snapshot: EntryClassificationSnapshotV1,
    working_tree: RequirementTreeSnapshotV1 | None,
    payload: RequirementAtomizationScopeRunRequest,
) -> None:
    if session.project_id != project_id or session.document_id != document_id:
        raise ValueError("atomization scope session binding is invalid")
    if payload.expected_review_revision != session.reviewed_result.revision:
        raise ValueError("review revision has changed")
    if (
        classification_snapshot.project_id != project_id
        or classification_snapshot.sor_document_id != document_id
        or classification_snapshot.source_sha256 != session.prediction.source_sha256
        or classification_snapshot.effective_structure_id
        != session.prediction.structure_snapshot_id
    ):
        raise ValueError("classification snapshot binding is stale")
    if (
        canonical_entry_classification_payload_sha256(classification_snapshot)
        != classification_snapshot.payload_sha256
    ):
        raise ValueError("classification snapshot hash is invalid")
    if (
        payload.expected_entry_classification_snapshot_id
        != classification_snapshot.snapshot_id
        or payload.expected_entry_classification_revision
        != classification_snapshot.revision
        or payload.expected_entry_classification_sha256
        != classification_snapshot.payload_sha256
    ):
        raise ValueError("entry classification snapshot has changed")
    if (
        payload.expected_working_tree_id != classification_snapshot.working_tree_id
        or payload.expected_working_tree_sha256
        != classification_snapshot.working_tree_sha256
        or payload.expected_effective_structure_id
        != classification_snapshot.effective_structure_id
    ):
        raise ValueError("working tree identity has changed")
    if working_tree is not None:
        if (
            working_tree.source_identity.project_id != project_id
            or working_tree.source_identity.document_id != document_id
            or working_tree.source_identity.source_sha256
            != classification_snapshot.source_sha256
            or working_tree.structure_identity.effective_snapshot_id
            != classification_snapshot.effective_structure_id
        ):
            raise ValueError("working tree source binding is stale")
        tree_sha256 = canonical_requirement_tree_sha256(working_tree)
        if (
            tree_sha256 != classification_snapshot.working_tree_sha256
            or classification_snapshot.working_tree_id != tree_sha256
        ):
            raise ValueError("working tree identity has changed")


def build_atomization_scope_execution_identity(
    *,
    session: LlmRequirementReviewSession,
    coverage: RequirementEnrichmentCoverageReceipt,
    classification_snapshot: EntryClassificationSnapshotV1,
    working_tree: RequirementTreeSnapshotV1,
) -> AtomizationScopeExecutionIdentity:
    lineage = build_requirement_enrichment_lineage(session, coverage=coverage)
    working_tree_sha256 = canonical_requirement_tree_sha256(working_tree)
    return AtomizationScopeExecutionIdentity(
        source_sha256=session.prediction.source_sha256,
        lineage_sha256=lineage.lineage_sha256,
        review_revision=session.reviewed_result.revision,
        reviewed_result_sha256=lineage.reviewed_result_sha256,
        coverage_sha256=coverage.coverage_sha256,
        entry_classification_snapshot_id=classification_snapshot.snapshot_id,
        entry_classification_revision=classification_snapshot.revision,
        entry_classification_sha256=classification_snapshot.payload_sha256,
        working_tree_id=classification_snapshot.working_tree_id,
        working_tree_sha256=working_tree_sha256,
        effective_structure_id=working_tree.structure_identity.effective_snapshot_id,
    )


def _assert_current_execution_identity(
    expected: AtomizationScopeExecutionIdentity | None,
    current: AtomizationScopeExecutionIdentity,
) -> None:
    if expected is None or current != expected:
        raise ValueError("atomization execution identity has changed")


def _validate_canonical_target_scope(
    *,
    classification_snapshot: EntryClassificationSnapshotV1,
    working_tree: RequirementTreeSnapshotV1 | None,
    payload: RequirementAtomizationScopeRunRequest,
) -> None:
    if payload.operation == "UNDO":
        return
    if working_tree is None:
        raise ValueError(f"{payload.scope} scope requires the current working tree")
    ordered_nodes = entry_classification_nodes_in_tree_order(working_tree)
    canonical_tree_ids = tuple(item.id for item in ordered_nodes)
    canonical_workflow_ids = tuple(
        item.workflow_node_id or item.id for item in ordered_nodes
    )
    if canonical_tree_ids != tuple(
        item.tree_node_id for item in classification_snapshot.entries
    ) or canonical_workflow_ids != tuple(
        item.workflow_node_id for item in classification_snapshot.entries
    ):
        raise ValueError("classification snapshot does not match the working tree")
    if payload.scope == "ALL":
        if payload.target_review_node_ids != canonical_workflow_ids:
            raise ValueError("ALL target range must match canonical tree order")
        return
    if payload.scope == "SELECTED":
        canonical_workflow_id_set = set(canonical_workflow_ids)
        unknown = tuple(
            item
            for item in payload.target_review_node_ids
            if item not in canonical_workflow_id_set
        )
        if unknown:
            raise ValueError(
                f"atomization request contains unknown target: {unknown[0]}"
            )
        selected_ids = set(payload.target_review_node_ids)
        canonical_selected_ids = tuple(
            item for item in canonical_workflow_ids if item in selected_ids
        )
        if payload.target_review_node_ids != canonical_selected_ids:
            raise ValueError("SELECTED target range must match canonical tree order")
        return
    if payload.scope not in {"CURRENT", "BRANCH"}:
        return
    node_by_id = {item.id: item for item in working_tree.nodes}
    anchor_tree_node_id = payload.anchor_tree_node_id or ""
    if anchor_tree_node_id not in node_by_id:
        raise ValueError(f"{payload.scope} anchor is not in the current working tree")
    workflow_by_tree_id = {
        item.tree_node_id: item.workflow_node_id
        for item in classification_snapshot.entries
    }
    if payload.scope == "CURRENT":
        workflow_node_id = workflow_by_tree_id.get(anchor_tree_node_id)
        if workflow_node_id is None:
            raise ValueError("CURRENT anchor is not one canonical business entry")
        if payload.target_review_node_ids != (workflow_node_id,):
            raise ValueError("CURRENT target must match its canonical tree anchor")
        return
    expected: list[str] = []
    pending = [anchor_tree_node_id]
    while pending:
        tree_node_id = pending.pop()
        node = node_by_id[tree_node_id]
        workflow_node_id = workflow_by_tree_id.get(tree_node_id)
        if workflow_node_id is not None:
            expected.append(workflow_node_id)
        pending.extend(reversed(node.child_ids))
    if payload.target_review_node_ids != tuple(expected):
        raise ValueError("BRANCH target range must match canonical descendants")


def _completed_scope_attempt_ids(
    provider: _CheckpointedSelectedRequirementAtomizationProvider,
    *,
    executed_review_node_ids: tuple[str, ...],
) -> tuple[str, ...]:
    return _ordered_review_node_ids(
        executed_review_node_ids,
        (
            *provider.checkpoint_reused_review_node_ids,
            *provider.checkpoint_created_review_node_ids,
        ),
    )


def _ordered_review_node_ids(
    canonical_review_node_ids: tuple[str, ...],
    values: tuple[str, ...],
) -> tuple[str, ...]:
    selected = set(values)
    return tuple(
        review_node_id
        for review_node_id in canonical_review_node_ids
        if review_node_id in selected
    )


def _interrupted_scope_attempt(
    running: AtomizationScopeAttemptV1,
    *,
    selected_provider: _CheckpointedSelectedRequirementAtomizationProvider,
    error: Exception,
) -> AtomizationScopeAttemptV1:
    completed = _completed_scope_attempt_ids(
        selected_provider,
        executed_review_node_ids=running.executed_review_node_ids,
    )
    failed = _ordered_review_node_ids(
        running.cumulative_review_node_ids,
        selected_provider.failed_review_node_ids,
    )
    return _build_scope_attempt(
        project_id=running.project_id,
        document_id=running.document_id,
        session_id=running.session_id,
        review_revision=running.review_revision,
        payload=None,
        command_id=running.command_id,
        command_sha256=running.command_sha256,
        expected_scope_revision=running.expected_scope_revision,
        operation=running.operation,
        scope=running.scope,
        lineage_sha256=running.lineage_sha256,
        attempt_sequence=running.attempt_sequence,
        requested=running.requested_review_node_ids,
        executed=running.executed_review_node_ids,
        skipped=running.skipped_targets,
        cumulative=running.cumulative_review_node_ids,
        status=AtomizationScopeAttemptStatus.INTERRUPTED,
        completed=completed,
        failed=failed,
        checkpoint_created=_ordered_review_node_ids(
            running.cumulative_review_node_ids,
            selected_provider.checkpoint_created_review_node_ids,
        ),
        checkpoint_reused=_ordered_review_node_ids(
            running.cumulative_review_node_ids,
            selected_provider.checkpoint_reused_review_node_ids,
        ),
        checkpoint_invalidated=(
            _ordered_review_node_ids(
                running.cumulative_review_node_ids,
                selected_provider.checkpoint_invalidated_review_node_ids,
            )
        ),
        provider_call_count=len(selected_provider.provider_call_review_node_ids),
        retryable=bool(failed),
        error_code=_scope_attempt_error_code(error),
        run=None,
        receipt=None,
        started_at=running.started_at_utc,
        updated_at=datetime.now(timezone.utc),
    )


def _scope_attempt_error_code(error: Exception) -> str:
    message = str(error).lower()
    if "historical atomization checkpoint" in message:
        return "HISTORICAL_CHECKPOINT_UNAVAILABLE"
    if "classification" in message and "changed" in message:
        return "CLASSIFICATION_IDENTITY_CHANGED"
    if isinstance(error, RequirementEnrichmentConflictError):
        return "SCOPE_WRITE_CONFLICT"
    return type(error).__name__[:160] or "ATOMIZATION_SCOPE_INTERRUPTED"


def _build_scope_attempt(
    *,
    project_id: str,
    document_id: str,
    lineage_sha256: str,
    attempt_sequence: int,
    requested: tuple[str, ...],
    executed: tuple[str, ...],
    skipped: tuple[AtomizationScopeSkippedTargetV1, ...],
    cumulative: tuple[str, ...],
    status: AtomizationScopeAttemptStatus,
    completed: tuple[str, ...],
    failed: tuple[str, ...],
    checkpoint_created: tuple[str, ...],
    checkpoint_reused: tuple[str, ...],
    checkpoint_invalidated: tuple[str, ...],
    provider_call_count: int,
    retryable: bool,
    error_code: str,
    run: RequirementAtomizationRun | None,
    receipt: AtomizationScopeReceiptV1 | None,
    started_at: datetime,
    updated_at: datetime,
    session: LlmRequirementReviewSession | None = None,
    payload: RequirementAtomizationScopeRunRequest | None = None,
    session_id: str = "",
    review_revision: int = 0,
    command_id: str = "",
    command_sha256: str = "",
    expected_scope_revision: int = 0,
    operation: AtomizationScopeOperation | str = AtomizationScopeOperation.APPLY,
    scope: AtomizationTargetScope | str = AtomizationTargetScope.SELECTED,
) -> AtomizationScopeAttemptV1:
    if session is not None:
        session_id = session.session_id
        review_revision = session.reviewed_result.revision
    if payload is not None:
        command_id = payload.command_id
        command_sha256 = payload.target_scope_sha256
        expected_scope_revision = payload.expected_scope_revision
        operation = payload.operation
        scope = payload.scope
    attempt_id = stable_atomization_scope_attempt_id(
        lineage_sha256=lineage_sha256,
        attempt_sequence=attempt_sequence,
        command_sha256=command_sha256,
    )
    fields = {
        "attempt_id": attempt_id,
        "payload_sha256": "0" * 64,
        "project_id": project_id,
        "document_id": document_id,
        "session_id": session_id,
        "review_revision": review_revision,
        "lineage_sha256": lineage_sha256,
        "attempt_sequence": attempt_sequence,
        "command_id": command_id,
        "command_sha256": command_sha256,
        "expected_scope_revision": expected_scope_revision,
        "operation": AtomizationScopeOperation(operation),
        "scope": AtomizationTargetScope(scope),
        "status": status,
        "requested_review_node_ids": requested,
        "executed_review_node_ids": executed,
        "skipped_targets": skipped,
        "cumulative_review_node_ids": cumulative,
        "completed_review_node_ids": completed,
        "failed_review_node_ids": failed,
        "checkpoint_created_review_node_ids": checkpoint_created,
        "checkpoint_reused_review_node_ids": checkpoint_reused,
        "checkpoint_invalidated_review_node_ids": checkpoint_invalidated,
        "provider_call_count": provider_call_count,
        "retryable": retryable,
        "checkpoint_resumable": (
            bool(completed)
            if status is AtomizationScopeAttemptStatus.INTERRUPTED
            else False
        ),
        "error_code": error_code,
        "atomization_run_id": run.atomization_run_id if run is not None else "",
        "atomization_run_revision": run.revision if run is not None else 0,
        "receipt_id": receipt.receipt_id if receipt is not None else "",
        "started_at_utc": started_at,
        "updated_at_utc": updated_at,
    }
    draft = AtomizationScopeAttemptV1.model_construct(**fields)
    fields["payload_sha256"] = canonical_atomization_scope_attempt_sha256(draft)
    return AtomizationScopeAttemptV1.model_validate(fields)


def _scope_skip_reason(
    effective_type: EntryClassificationType | None,
    reviewed: object | None,
) -> AtomizationScopeSkipReason | None:
    if effective_type is None:
        return AtomizationScopeSkipReason.UNCLASSIFIED
    if effective_type is EntryClassificationType.NON_TECHNICAL:
        return AtomizationScopeSkipReason.NON_TECHNICAL
    if effective_type is EntryClassificationType.INFORMATION:
        return AtomizationScopeSkipReason.INFORMATION
    if (
        reviewed is None
        or getattr(reviewed, "semantic_class", None) is not SemanticClass.REQUIREMENT
    ):
        return AtomizationScopeSkipReason.NOT_REQUIREMENT
    if not getattr(reviewed, "include_in_view", False):
        return AtomizationScopeSkipReason.NOT_INCLUDED
    if getattr(reviewed, "decision_mode", None) is not RequirementDecisionMode.DIRECT:
        return AtomizationScopeSkipReason.NOT_DIRECT
    return None


def _build_scope_revision_run(
    base_run: RequirementAtomizationRun,
    *,
    session: LlmRequirementReviewSession,
    revision: int,
) -> RequirementAtomizationRun:
    reviewed_by_id = {
        item.review_node_id: item for item in session.reviewed_result.nodes
    }
    atom_counts: dict[str, int] = {}
    for node in base_run.atomic_nodes:
        atom_counts[node.review_node_id] = atom_counts.get(node.review_node_id, 0) + 1
    parent_by_review_id: dict[str, RequirementAtomizationNode] = {}
    for review_node_id, count in atom_counts.items():
        if count <= 1:
            continue
        reviewed = reviewed_by_id[review_node_id]
        children = tuple(
            item
            for item in base_run.atomic_nodes
            if item.review_node_id == review_node_id
        )
        assert reviewed.role is not None and reviewed.requirement_subtype is not None
        parent_id = stable_atomization_node_id(
            lineage_sha256=base_run.lineage.lineage_sha256,
            review_node_id=review_node_id,
            source_spans=reviewed.source_spans,
            decision_mode=RequirementDecisionMode.ROLLUP,
        )
        parent_by_review_id[review_node_id] = RequirementAtomizationNode(
            node_id=parent_id,
            atomic_node_id=None,
            kind=RequirementAtomizationNodeKind.ROLLUP,
            parent_node_id=children[0].parent_node_id,
            review_node_id=review_node_id,
            source_semantic_unit_ids=reviewed.source_semantic_unit_ids,
            source_owner_structure_node_ids=reviewed.source_owner_structure_node_ids,
            source_spans=reviewed.source_spans,
            source_order=reviewed.source_order,
            role=reviewed.role,
            decision_mode=RequirementDecisionMode.ROLLUP,
            requirement_subtype=reviewed.requirement_subtype,
            source_text="".join(item.source_text for item in children),
            atomic_statement=None,
        )
    nodes: list[RequirementAtomizationNode] = []
    inserted: set[str] = set()
    for node in base_run.nodes:
        parent = parent_by_review_id.get(node.review_node_id)
        if parent is not None and node.review_node_id not in inserted:
            nodes.append(parent)
            inserted.add(node.review_node_id)
        nodes.append(
            node.model_copy(update={"parent_node_id": parent.node_id})
            if parent is not None
            and node.kind is RequirementAtomizationNodeKind.ATOMIC_OBLIGATION
            else node
        )
    frozen_nodes = tuple(nodes)
    run_id = stable_atomization_run_id(
        lineage_sha256=base_run.lineage.lineage_sha256,
        revision=revision,
        algorithm_version=_SCOPED_RUN_ALGORITHM_VERSION,
        nodes=frozen_nodes,
        provider_runs=base_run.provider_runs,
    )
    return RequirementAtomizationRun(
        atomization_run_id=run_id,
        revision=revision,
        algorithm_version=_SCOPED_RUN_ALGORITHM_VERSION,
        lineage=base_run.lineage,
        nodes=frozen_nodes,
        provider_runs=base_run.provider_runs,
        created_at_utc=datetime.now(timezone.utc),
    )


def _copy_scope_run_revision(
    source: RequirementAtomizationRun,
    *,
    revision: int,
) -> RequirementAtomizationRun:
    run_id = stable_atomization_run_id(
        lineage_sha256=source.lineage.lineage_sha256,
        revision=revision,
        algorithm_version=source.algorithm_version,
        nodes=source.nodes,
        provider_runs=source.provider_runs,
    )
    return RequirementAtomizationRun(
        atomization_run_id=run_id,
        revision=revision,
        algorithm_version=source.algorithm_version,
        lineage=source.lineage,
        nodes=source.nodes,
        provider_runs=source.provider_runs,
        created_at_utc=datetime.now(timezone.utc),
    )


def _build_scope_receipt(
    *,
    project_id: str,
    document_id: str,
    session: LlmRequirementReviewSession,
    classification_snapshot: EntryClassificationSnapshotV1,
    payload: RequirementAtomizationScopeRunRequest,
    lineage_sha256: str,
    scope_revision: int,
    active_revision: int,
    previous_active_revision: int,
    supersedes_receipt_id: str,
    run: RequirementAtomizationRun,
    requested: tuple[str, ...],
    executed: tuple[str, ...],
    new_ids: tuple[str, ...],
    reused_ids: tuple[str, ...],
    skipped: tuple[AtomizationScopeSkippedTargetV1, ...],
    pass_through: tuple[str, ...],
    cumulative: tuple[str, ...],
    selected_provider: _CheckpointedSelectedRequirementAtomizationProvider,
    created_at: datetime,
) -> AtomizationScopeReceiptV1:
    command_sha256 = payload.target_scope_sha256
    receipt_id = stable_atomization_scope_receipt_id(
        lineage_sha256=lineage_sha256,
        scope_revision=scope_revision,
        command_sha256=command_sha256,
        atomization_run_id=run.atomization_run_id,
    )
    fields = {
        "receipt_id": receipt_id,
        "payload_sha256": "0" * 64,
        "project_id": project_id,
        "document_id": document_id,
        "session_id": session.session_id,
        "review_revision": session.reviewed_result.revision,
        "lineage_sha256": lineage_sha256,
        "entry_classification_snapshot_id": classification_snapshot.snapshot_id,
        "entry_classification_revision": classification_snapshot.revision,
        "entry_classification_sha256": classification_snapshot.payload_sha256,
        "entry_classification_content_sha256": (
            stable_effective_entry_classification_sha256(classification_snapshot)
        ),
        "scope_revision": scope_revision,
        "active_revision": active_revision,
        "previous_active_revision": previous_active_revision,
        "supersedes_receipt_id": supersedes_receipt_id,
        "command_id": payload.command_id,
        "command_sha256": command_sha256,
        "operation": AtomizationScopeOperation(payload.operation),
        "scope": AtomizationTargetScope(payload.scope),
        "target_scope_sha256": payload.target_scope_sha256,
        "unselected_policy": AtomizationUnselectedPolicy(payload.unselected_policy),
        "requested_review_node_ids": requested,
        "executed_review_node_ids": executed,
        "new_review_node_ids": new_ids,
        "reused_review_node_ids": reused_ids,
        "skipped_targets": skipped,
        "succeeded_review_node_ids": executed,
        "failed_review_node_ids": (),
        "pass_through_review_node_ids": pass_through,
        "cumulative_review_node_ids": cumulative,
        "provider_call_count": len(selected_provider.provider_call_review_node_ids),
        "checkpoint_created_count": len(
            selected_provider.checkpoint_created_review_node_ids
        ),
        "checkpoint_reuse_count": len(
            selected_provider.checkpoint_reused_review_node_ids
        ),
        "resumable": False,
        "atomization_run_id": run.atomization_run_id,
        "atomization_run_revision": run.revision,
        "created_at_utc": created_at,
    }
    draft = AtomizationScopeReceiptV1.model_construct(**fields)
    fields["payload_sha256"] = canonical_atomization_scope_receipt_sha256(draft)
    return AtomizationScopeReceiptV1.model_validate(fields)


__all__ = [
    "AtomizationScopeExecutionIdentity",
    "RequirementAtomizationScopeExecution",
    "RequirementAtomizationScopeExecutionError",
    "ScopedRequirementAtomizationProvider",
    "build_atomization_scope_execution_identity",
    "create_requirement_atomization_scope_run",
    "execute_requirement_atomization_scope",
    "load_current_entry_classification_context",
    "load_current_entry_classification_snapshot",
    "serialize_requirement_atomization_scope_state",
]
