from __future__ import annotations

import copy
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from threading import RLock, Thread
from typing import Any, Callable

from reqpilotx_server.adapters.persistence.postgres_jobs import JobConflictError
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.extraction.contracts import StructureOptimizationStartRequest
from reqpilotx_server.application.extraction.optimize_structure import (
    StructureOptimizationProposalOutcomeV1,
    StructureOptimizationProposalStatus,
    apply_structure_optimization_proposal,
    propose_structure_optimization_with_provider,
)
from reqpilotx_server.application.extraction.orchestrate_structure_agent import (
    StructureManualMutationResult,
)
from reqpilotx_server.application.extraction.provider_source_policy import (
    ProviderSourceIRRejected,
    require_complete_source_ir_for_provider,
)
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.domain.extraction.structure_revision import (
    AddStructureRelationOperationV1,
    BoundarySplitStructureOperationV1,
    ContinuationMergeStructureOperationV1,
    EffectiveStructureSnapshotV1,
    LogicalReorderStructureOperationV1,
    RemoveStructureRelationOperationV1,
    ReparentStructureOperationV1,
    StructureRevisionRejected,
    StructureRevisionV1,
)
from reqpilotx_server.domain.extraction.processing_profile import ResolvedProcessingPlanV1
from reqpilotx_server.infrastructure.runtime_settings import DatabaseBackend
from reqpilotx_server.ports.job_queue import DurableJob, JobExecutionContext, JobStatus


logger = logging.getLogger(__name__)

STRUCTURE_OPTIMIZATION_JOB_SCHEMA_VERSION = "structure-optimization-job/v1"
_LOCAL_OPTIMIZATION_LOCK = RLock()
_LOCAL_OPTIMIZATION_JOBS: dict[str, dict[str, Any]] = {}
_TERMINAL_JOB_STATUSES = {
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
}


def start_document_structure_optimization(
    project_id: str,
    document_id: str,
    response: ResponseSink,
    payload: StructureOptimizationStartRequest | None = None,
) -> dict[str, Any]:
    """Start or reuse one source/effective-revision-bound optimizer run."""

    from reqpilotx_server.application.extraction.provider_support import (
        _build_structure_optimizer_provider,
    )
    from reqpilotx_server.application.runtime import (
        RUNTIME_SETTINGS,
        _ASSET_RESPONSE_HEADERS,
        _durable_jobs,
    )

    response.headers.update(_ASSET_RESPONSE_HEADERS)
    document, _, extraction_result, _, source_ir, base_tree, revisions, current = (
        _load_current_structure(project_id, document_id)
    )
    try:
        require_complete_source_ir_for_provider(
            source_ir,
            operation="structure_optimization",
        )
    except ProviderSourceIRRejected as exc:
        raise ApplicationError(
            status_code=409,
            code="structure_optimization_requires_reextraction",
            detail={
                "code": "structure_optimization_requires_reextraction",
                "reason_code": exc.code.value,
                "message": (
                    "Source capture is incomplete. Re-extract the document "
                    "before running Structure optimization."
                ),
                "source_ir_status": exc.source_ir_status.value,
                "required_source_ir_status": "COMPLETE",
                "blocked_by": ["source_capture"],
                "next_action": "reextract_source",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc
    request = payload or StructureOptimizationStartRequest()
    plan = _structure_optimization_processing_plan(
        project_id=project_id,
        document_id=document_id,
        extraction_result=extraction_result,
        llm_provider_override=request.llm_provider_override,
    )
    provider = _build_structure_optimizer_provider(plan)
    job_payload = {
        "schema_version": STRUCTURE_OPTIMIZATION_JOB_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "source_ir_id": source_ir.source_ir_id,
        "source_sha256": source_ir.source_sha256,
        "base_structure_snapshot_id": base_tree.structure_snapshot_id,
        "expected_revision_number": current.revision_number,
        "expected_effective_snapshot_id": current.effective_snapshot_id,
        "expected_effective_tree_sha256": current.effective_tree_sha256,
        "expected_revision_chain_sha256": current.revision_chain_sha256,
        "expected_head_revision_id": current.head_revision_id,
        "processing_plan": plan.model_dump(mode="json"),
        "llm_provider": plan.llm_provider,
        "provider_id": provider.provider_id,
        "provider_version": provider.provider_version,
        "model_id": provider.model_id,
        "stage": "QUEUED",
    }
    idempotency_key = hashlib.sha256(
        json.dumps(
            job_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        try:
            job = _durable_jobs().enqueue(
                kind="structure.optimize",
                aggregate_type="sor-document",
                aggregate_id=document_id,
                idempotency_key=idempotency_key,
                payload=job_payload,
                priority=20,
                max_attempts=2,
            )
        except JobConflictError as exc:
            current_job = _durable_jobs().get_latest(
                kind="structure.optimize",
                aggregate_type="sor-document",
                aggregate_id=document_id,
            )
            if (
                current_job is not None
                and current_job.idempotency_key == idempotency_key
            ):
                job = current_job
            else:
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": "structure_optimization_already_running",
                        "message": (
                            "Another Structure optimization already owns this document."
                        ),
                    },
                    headers=_ASSET_RESPONSE_HEADERS,
                ) from exc
        snapshot = _optimization_snapshot_from_durable(job)
    else:
        snapshot = _start_local_structure_optimization(
            payload=job_payload,
            idempotency_key=idempotency_key,
            provider=provider,
        )

    if snapshot["status"] not in _TERMINAL_JOB_STATUSES:
        response.status_code = 202
    return _optimization_response(project_id, document_id, snapshot)


def _structure_optimization_processing_plan(
    *,
    project_id: str,
    document_id: str,
    extraction_result: dict[str, Any],
    llm_provider_override: str | None,
) -> ResolvedProcessingPlanV1:
    from pydantic import ValidationError

    from reqpilotx_server.application.processing_profiles import (
        require_capabilities,
        required_llm_capability_ids,
        resolve_processing_plan,
    )

    plan: ResolvedProcessingPlanV1 | None = None
    if llm_provider_override is None:
        try:
            plan = ResolvedProcessingPlanV1.model_validate(
                extraction_result.get("processing_plan")
            )
        except (ValidationError, TypeError, ValueError):
            plan = None
    if plan is None or llm_provider_override is not None:
        plan = resolve_processing_plan(
            project_id=project_id,
            document_id=document_id,
            llm_override=llm_provider_override,
            skip_document_capability_gate=True,
        )
    if plan.llm_provider == "none":
        raise ApplicationError(
            status_code=409,
            code="structure_optimizer_llm_not_selected",
            detail={
                "reason_code": "structure_optimizer_llm_not_selected",
                "message": (
                    "Structure optimization requires a ready Aliyun or Azure "
                    "OpenAI provider."
                ),
                "blocked_by": ["llm_provider"],
                "settings_path": f"/projects/{project_id}/settings",
            },
        )
    require_capabilities(
        required_llm_capability_ids(plan.llm_provider),
        operation="structure.optimization",
    )
    return plan


def get_document_structure_optimization_status(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.runtime import (
        RUNTIME_SETTINGS,
        _ASSET_RESPONSE_HEADERS,
        _durable_jobs,
    )

    _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        job = _durable_jobs().get_latest(
            kind="structure.optimize",
            aggregate_type="sor-document",
            aggregate_id=document_id,
        )
        snapshot = (
            _optimization_snapshot_from_durable(job)
            if job is not None
            else _idle_optimization_snapshot()
        )
    else:
        with _LOCAL_OPTIMIZATION_LOCK:
            current = _LOCAL_OPTIMIZATION_JOBS.get(document_id)
            snapshot = (
                _public_local_snapshot(current)
                if current is not None
                and str(current.get("project_id") or "") == project_id
                else _idle_optimization_snapshot()
            )
    return _optimization_response(project_id, document_id, snapshot)


def run_durable_structure_optimization(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.provider_support import (
        _build_structure_optimizer_provider,
    )

    payload = dict(durable_job.payload or {})
    plan = ResolvedProcessingPlanV1.model_validate(payload.get("processing_plan"))
    return _execute_structure_optimization(
        payload=payload,
        job_id=durable_job.job_id,
        provider=_build_structure_optimizer_provider(plan),
        publish_progress=lambda stage: context.heartbeat(progress={"stage": stage}),
        begin_publication=context.begin_publication,
        cancellation_requested=context.cancellation_requested,
    )


def structure_optimization_is_running(
    project_id: str,
    document_id: str,
) -> bool:
    """Application guard used by downstream workflows."""

    from reqpilotx_server.application.runtime import RUNTIME_SETTINGS, _durable_jobs

    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        job = _durable_jobs().get_latest(
            kind="structure.optimize",
            aggregate_type="sor-document",
            aggregate_id=document_id,
        )
        return bool(
            job is not None
            and str(job.payload.get("project_id") or "") == project_id
            and job.status
            in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.CANCEL_REQUESTED,
            }
        )
    with _LOCAL_OPTIMIZATION_LOCK:
        job = _LOCAL_OPTIMIZATION_JOBS.get(document_id)
        return bool(
            job
            and str(job.get("project_id") or "") == project_id
            and str(job.get("status") or "") not in _TERMINAL_JOB_STATUSES
        )


def _start_local_structure_optimization(
    *,
    payload: dict[str, Any],
    idempotency_key: str,
    provider: Any,
) -> dict[str, Any]:
    document_id = str(payload["document_id"])
    project_id = str(payload["project_id"])
    with _LOCAL_OPTIMIZATION_LOCK:
        current = _LOCAL_OPTIMIZATION_JOBS.get(document_id)
        if (
            current
            and str(current.get("project_id") or "") == project_id
            and str(current.get("idempotency_key") or "") == idempotency_key
        ):
            return _public_local_snapshot(current)
        if current and str(current.get("status") or "") not in _TERMINAL_JOB_STATUSES:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "structure_optimization_already_running",
                    "message": (
                        "Another Structure optimization already owns this document."
                    ),
                },
            )
        now = datetime.now(timezone.utc).isoformat()
        job_id = f"structure-optimization-{uuid.uuid4().hex}"
        job = {
            **payload,
            "job_id": job_id,
            "idempotency_key": idempotency_key,
            "status": "PENDING",
            "stage": "QUEUED",
            "result": None,
            "error_code": "",
            "created_at_utc": now,
            "updated_at_utc": now,
            "finished_at_utc": "",
            "non_cancellable": False,
        }
        _LOCAL_OPTIMIZATION_JOBS[document_id] = job
        worker = Thread(
            target=_run_local_structure_optimization,
            kwargs={
                "document_id": document_id,
                "job_id": job_id,
                "payload": dict(payload),
                "provider": provider,
            },
            name=f"structure-optimization-{document_id[:8]}",
            daemon=True,
        )
        worker.start()
        return _public_local_snapshot(job)


def _run_local_structure_optimization(
    *,
    document_id: str,
    job_id: str,
    payload: dict[str, Any],
    provider: Any,
) -> None:
    def publish_progress(stage: str) -> None:
        _update_local_job(document_id, job_id, status="RUNNING", stage=stage)

    def begin_publication() -> None:
        _update_local_job(
            document_id,
            job_id,
            status="RUNNING",
            stage="APPLYING",
            non_cancellable=True,
        )

    try:
        result = _execute_structure_optimization(
            payload=payload,
            job_id=job_id,
            provider=provider,
            publish_progress=publish_progress,
            begin_publication=begin_publication,
            cancellation_requested=lambda: False,
        )
    except Exception:
        logger.exception(
            "Structure optimization failed. document_id=%s job_id=%s",
            document_id,
            job_id,
        )
        _update_local_job(
            document_id,
            job_id,
            status="FAILED",
            stage="FAILED",
            error_code="structure_optimization_failed",
            finished_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        return
    _update_local_job(
        document_id,
        job_id,
        status="SUCCEEDED",
        stage="COMPLETED",
        result=result,
        finished_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def _execute_structure_optimization(
    *,
    payload: dict[str, Any],
    job_id: str,
    provider: Any,
    publish_progress: Callable[[str], None],
    begin_publication: Callable[[], Any],
    cancellation_requested: Callable[[], bool],
) -> dict[str, Any]:
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    from reqpilotx_server.application.extraction import structure_workspace

    with structure_workspace._STRUCTURE_MUTATION_LOCK:
        recovered = _recover_applied_structure_optimization(
            payload=payload,
            job_id=job_id,
        )
    if recovered is not None:
        return recovered

    try:
        initial = _load_current_structure(project_id, document_id)
    except ApplicationError:
        return _stale_result(payload, reason_code="current_structure_unavailable")
    _, _, _, _, source_ir, base_tree, revisions, current = initial
    if not _payload_matches_current(payload, current):
        return _stale_result(payload, reason_code="stale_before_overview")
    try:
        require_complete_source_ir_for_provider(
            source_ir,
            operation="structure_optimization",
        )
    except ProviderSourceIRRejected as exc:
        return _requires_reextraction_result(
            payload,
            source_ir_status=exc.source_ir_status.value,
            reason_code=exc.code.value,
        )

    publish_progress("OVERVIEW")
    proposal = propose_structure_optimization_with_provider(
        source_ir,
        base_tree,
        revisions,
        provider,
    )
    if proposal.status is not StructureOptimizationProposalStatus.PROPOSED:
        return _result_from_proposal(proposal)
    if cancellation_requested():
        return {
            **_result_from_proposal(proposal),
            "status": "CANCELLED",
        }

    begin_publication()
    with structure_workspace._STRUCTURE_MUTATION_LOCK:
        try:
            (
                document,
                job,
                result,
                agent,
                fresh_source_ir,
                fresh_base_tree,
                fresh_revisions,
                fresh_current,
            ) = _load_current_structure(project_id, document_id)
            applied = apply_structure_optimization_proposal(
                proposal,
                fresh_source_ir,
                fresh_base_tree,
                fresh_revisions,
            )
        except ProviderSourceIRRejected as exc:
            return _requires_reextraction_result(
                payload,
                source_ir_status=exc.source_ir_status.value,
                reason_code=exc.code.value,
                proposal=proposal,
            )
        except (ApplicationError, StructureRevisionRejected, ValueError):
            return _stale_result(
                payload,
                reason_code="stale_before_apply",
                proposal=proposal,
            )
        if applied.apply_result is None or not applied.apply_result.appended:
            return _stale_result(
                payload,
                reason_code="duplicate_or_non_appending_apply",
                proposal=proposal,
            )
        mutation = StructureManualMutationResult(
            proposal=proposal.proposal,
            apply_result=applied.apply_result,
            revisions=(*fresh_revisions, applied.apply_result.revision),
        )
        updated_result, updated_agent = (
            structure_workspace._updated_result_after_structure_mutation(
                result=result,
                agent=agent,
                source_ir=fresh_source_ir,
                mutation=mutation,
                action="PATCH",
                origin="AI",
                provider_run_id=proposal.repair.run.provider_run_id,
                optimization_job_id=job_id,
            )
        )
        applied_result = _applied_result_from_proposal(
            proposal,
            before=fresh_current,
            after=applied.effective_snapshot,
            revision=applied.apply_result.revision,
            impact=applied.apply_result.impact.model_dump(mode="json"),
            rerun=dict(updated_agent.get("rerun") or {}),
        )
        revision = applied.apply_result.revision
        updated_agent["optimization"] = {
            "job_id": job_id,
            "status": "APPLIED",
            "proposal_id": (
                proposal.proposal.proposal_id if proposal.proposal is not None else None
            ),
            "revision_id": revision.revision_id,
            "overview_issue_count": len(proposal.overview.issues),
            "operation_count": len(proposal.repair.operations),
            "provider_run_ids": [run.provider_run_id for run in proposal.provider_runs],
            "receipt": copy.deepcopy(applied_result),
        }
        updated_result["structure_agent"] = updated_agent
        try:
            structure_workspace._persist_structure_mutation_result(
                project_id=project_id,
                document_id=document_id,
                source_sha256=fresh_source_ir.source_sha256,
                job=job,
                result=updated_result,
            )
        except ApplicationError:
            return _stale_result(
                payload,
                reason_code="compare_and_set_conflict",
                proposal=proposal,
            )
        rerun_job = structure_workspace._queue_structure_derivation_rerun(
            project_id=project_id,
            document=document,
            previous_job=job,
            result=updated_result,
            structure_agent=updated_agent,
            structure_revisions=mutation.revisions,
        )
        applied_result["rerun"] = {
            **dict(updated_agent.get("rerun") or {}),
            "job_id": str(rerun_job.get("job_id") or "") or None,
            "job_status": str(rerun_job.get("status") or ""),
        }
        updated_agent["optimization"]["receipt"] = copy.deepcopy(applied_result)
        updated_result["structure_agent"] = updated_agent
        structure_workspace._store_structure_rerun_state(
            project_id=project_id,
            document_id=document_id,
            source_sha256=fresh_source_ir.source_sha256,
            result=updated_result,
        )

    return applied_result


def _recover_applied_structure_optimization(
    *,
    payload: dict[str, Any],
    job_id: str,
) -> dict[str, Any] | None:
    """Return the committed receipt when a retry follows the extraction CAS.

    The source-bound extraction result is the recovery journal. A retry may
    repair a missing derivation-rerun enqueue, but it never calls the optimizer
    provider again and never appends a second Structure revision.
    """

    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    try:
        (
            document,
            current_job,
            result,
            agent,
            source_ir,
            _,
            revisions,
            current,
        ) = _load_current_structure(
            project_id,
            document_id,
            require_final=False,
        )
    except ApplicationError:
        return None
    optimization = (
        agent.get("optimization") if isinstance(agent.get("optimization"), dict) else {}
    )
    if (
        str(optimization.get("job_id") or "") != job_id
        or str(optimization.get("status") or "") != "APPLIED"
    ):
        return None
    revision_id = str(optimization.get("revision_id") or "")
    revision = next(
        (item for item in revisions if item.revision_id == revision_id),
        None,
    )
    if revision is None or not _applied_revision_matches_payload(revision, payload):
        return None
    if revision.patch is None or (
        str(optimization.get("proposal_id") or "")
        != revision.patch.accepted_proposal_id
    ):
        return None
    receipt = optimization.get("receipt")
    if not isinstance(receipt, dict):
        return None
    recovered = copy.deepcopy(receipt)
    receipt_revision = (
        recovered.get("revision") if isinstance(recovered.get("revision"), dict) else {}
    )
    if (
        str(recovered.get("status") or "") != "APPLIED"
        or str(receipt_revision.get("revision_id") or "") != revision.revision_id
        or str(recovered.get("effective_snapshot_id") or "")
        != revision.effective_snapshot_id
    ):
        return None

    if current.head_revision_id != revision.revision_id:
        recovered["rerun"] = {
            **dict(recovered.get("rerun") or {}),
            "status": "SUPERSEDED",
            "superseded_by_revision_id": current.head_revision_id,
        }
        return recovered

    agent_rerun = agent.get("rerun") if isinstance(agent.get("rerun"), dict) else {}
    receipt_rerun = (
        recovered.get("rerun") if isinstance(recovered.get("rerun"), dict) else {}
    )
    existing_rerun_job_id = str(
        agent_rerun.get("job_id") or receipt_rerun.get("job_id") or ""
    )
    if not existing_rerun_job_id and str(agent_rerun.get("status") or "") not in {
        "NOT_REQUIRED",
    }:
        from reqpilotx_server.application.extraction import structure_workspace

        rerun_job = structure_workspace._queue_structure_derivation_rerun(
            project_id=project_id,
            document=document,
            previous_job=current_job,
            result=result,
            structure_agent=agent,
            structure_revisions=revisions,
        )
        recovered["rerun"] = {
            **dict(agent.get("rerun") or {}),
            "job_id": str(rerun_job.get("job_id") or "") or None,
            "job_status": str(rerun_job.get("status") or ""),
        }
        optimization["receipt"] = copy.deepcopy(recovered)
        agent["optimization"] = optimization
        result["structure_agent"] = agent
        structure_workspace._store_structure_rerun_state(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_ir.source_sha256,
            result=result,
        )
    elif agent_rerun:
        recovered["rerun"] = {
            **receipt_rerun,
            **agent_rerun,
            "job_id": existing_rerun_job_id or None,
        }
    return recovered


def _applied_revision_matches_payload(
    revision: StructureRevisionV1,
    payload: dict[str, Any],
) -> bool:
    return bool(
        revision.source_ir_id == str(payload.get("source_ir_id") or "")
        and revision.source_sha256 == str(payload.get("source_sha256") or "")
        and revision.base_structure_snapshot_id
        == str(payload.get("base_structure_snapshot_id") or "")
        and revision.revision_number
        == int(payload.get("expected_revision_number") or 0) + 1
        and revision.parent_revision_id == payload.get("expected_head_revision_id")
        and revision.previous_effective_snapshot_id
        == str(payload.get("expected_effective_snapshot_id") or "")
        and revision.previous_effective_tree_sha256
        == str(payload.get("expected_effective_tree_sha256") or "")
        and revision.previous_revision_chain_sha256
        == str(payload.get("expected_revision_chain_sha256") or "")
    )


def _applied_result_from_proposal(
    proposal: StructureOptimizationProposalOutcomeV1,
    *,
    before: EffectiveStructureSnapshotV1,
    after: EffectiveStructureSnapshotV1,
    revision: StructureRevisionV1,
    impact: dict[str, Any],
    rerun: dict[str, Any],
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction import structure_workspace

    result = _result_from_proposal(proposal)
    changes = _structure_change_summaries(
        proposal,
        before=before,
        after=after,
    )
    return {
        **result,
        "status": "APPLIED",
        "previous_effective_snapshot_id": proposal.expected_effective_snapshot_id,
        "effective_snapshot_id": after.effective_snapshot_id,
        "revision": structure_workspace._structure_revision_summary(revision),
        "changes": changes,
        "impact": impact,
        "impact_summary": {
            "direct_node_count": len(impact.get("direct_structure_node_ids") or ()),
            "affected_node_count": len(impact.get("affected_structure_node_ids") or ()),
            "affected_section_count": len(
                impact.get("affected_section_node_ids") or ()
            ),
            "changed_relation_count": len(impact.get("changed_relation_ids") or ()),
        },
        "rerun": rerun,
    }


def _structure_change_summaries(
    proposal: StructureOptimizationProposalOutcomeV1,
    *,
    before: EffectiveStructureSnapshotV1,
    after: EffectiveStructureSnapshotV1,
) -> list[dict[str, Any]]:
    repair = proposal.repair
    if repair is None:
        return []
    before_nodes = {node.structure_node_id: node for node in before.nodes}
    before_relations = {relation.relation_id: relation for relation in before.relations}
    after_relations = {relation.relation_id: relation for relation in after.relations}
    after_spans = {
        binding.structure_node_id: binding.source_spans
        for binding in after.node_source_spans
    }
    summaries: list[dict[str, Any]] = []
    for operation_index, operation in enumerate(repair.operations):
        summary: dict[str, Any] = {
            "operation_index": operation_index,
            "operation": operation.operation,
            "structure_node_ids": [],
            "relation_ids": [],
            "before": {},
            "after": {},
        }
        if isinstance(operation, ReparentStructureOperationV1):
            node = before_nodes[operation.structure_node_id]
            summary["structure_node_ids"] = [
                operation.structure_node_id,
                operation.new_parent_structure_node_id,
            ]
            summary["before"] = {
                "parent_structure_node_id": node.parent_structure_node_id,
                "sibling_index": _child_index(
                    before,
                    parent_id=node.parent_structure_node_id,
                    child_id=node.structure_node_id,
                ),
            }
            summary["after"] = {
                "parent_structure_node_id": operation.new_parent_structure_node_id,
                "sibling_index": operation.new_sibling_index,
            }
        elif isinstance(operation, LogicalReorderStructureOperationV1):
            summary["structure_node_ids"] = [
                operation.parent_structure_node_id,
                *operation.ordered_child_structure_node_ids,
            ]
            summary["before"] = {
                "ordered_child_structure_node_ids": _child_ids(
                    before,
                    operation.parent_structure_node_id,
                )
            }
            summary["after"] = {
                "ordered_child_structure_node_ids": list(
                    operation.ordered_child_structure_node_ids
                )
            }
        elif isinstance(operation, BoundarySplitStructureOperationV1):
            created_ids = [
                node_id
                for part in operation.parts
                for node_id, spans in after_spans.items()
                if spans == part.source_spans and node_id not in before_nodes
            ]
            summary["structure_node_ids"] = [
                operation.structure_node_id,
                *created_ids,
            ]
            summary["before"] = {
                "removed_structure_node_id": operation.structure_node_id,
                "part_count": 1,
            }
            summary["after"] = {
                "created_structure_node_ids": created_ids,
                "part_count": len(operation.parts),
            }
        elif isinstance(operation, ContinuationMergeStructureOperationV1):
            summary["structure_node_ids"] = [
                operation.primary_structure_node_id,
                operation.continuation_structure_node_id,
            ]
            summary["before"] = {
                "primary_structure_node_id": operation.primary_structure_node_id,
                "continuation_structure_node_id": (
                    operation.continuation_structure_node_id
                ),
            }
            summary["after"] = {
                "retained_structure_node_id": operation.primary_structure_node_id,
                "removed_structure_node_id": (operation.continuation_structure_node_id),
            }
        elif isinstance(operation, AddStructureRelationOperationV1):
            created_relations = [
                relation
                for relation_id, relation in after_relations.items()
                if relation_id not in before_relations
                and relation.kind == operation.kind
                and relation.source_structure_node_id
                == operation.source_structure_node_id
                and relation.target_structure_node_id
                == operation.target_structure_node_id
            ]
            relation_ids = [relation.relation_id for relation in created_relations]
            summary["structure_node_ids"] = [
                operation.source_structure_node_id,
                operation.target_structure_node_id,
            ]
            summary["relation_ids"] = relation_ids
            summary["after"] = {
                "relation_ids": relation_ids,
                "kind": operation.kind.value,
                "source_structure_node_id": (operation.source_structure_node_id),
                "target_structure_node_id": (operation.target_structure_node_id),
            }
        elif isinstance(operation, RemoveStructureRelationOperationV1):
            relation = before_relations[operation.relation_id]
            summary["structure_node_ids"] = [
                relation.source_structure_node_id,
                relation.target_structure_node_id,
            ]
            summary["relation_ids"] = [operation.relation_id]
            summary["before"] = {
                "relation_id": operation.relation_id,
                "kind": relation.kind.value,
                "source_structure_node_id": relation.source_structure_node_id,
                "target_structure_node_id": relation.target_structure_node_id,
            }
            summary["after"] = {"removed_relation_id": operation.relation_id}
        else:  # pragma: no cover - the operation union is closed.
            raise TypeError("unsupported Structure optimizer operation")
        summaries.append(summary)
    return summaries


def _child_ids(
    snapshot: EffectiveStructureSnapshotV1,
    parent_id: str | None,
) -> list[str]:
    return [
        node.structure_node_id
        for node in snapshot.nodes
        if node.parent_structure_node_id == parent_id
    ]


def _child_index(
    snapshot: EffectiveStructureSnapshotV1,
    *,
    parent_id: str | None,
    child_id: str,
) -> int:
    return _child_ids(snapshot, parent_id).index(child_id)


def _load_current_structure(
    project_id: str,
    document_id: str,
    *,
    require_final: bool = True,
) -> tuple[Any, ...]:
    from reqpilotx_server.application.extraction.document_jobs import (
        _llm_extraction_result_matches_runtime,
    )
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.extraction.structure_workspace import (
        _rebuild_structure_revision_context,
        _structure_agent_job_payload,
        _structure_api_error,
    )
    from reqpilotx_server.application.extraction.revise_structure import (
        validate_structure_revision_chain,
    )

    document = _require_project_document(project_id, document_id)
    job, result, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=require_final,
    )
    if not _llm_extraction_result_matches_runtime(result):
        raise _structure_api_error(
            "structure_runtime_binding_mismatch",
            "The completed result must be regenerated for this runtime.",
        )
    source_ir, base_tree, revisions = _rebuild_structure_revision_context(
        document=document,
        agent=agent,
    )
    current = validate_structure_revision_chain(source_ir, base_tree, revisions)
    return document, job, result, agent, source_ir, base_tree, revisions, current


def _payload_matches_current(payload: dict[str, Any], current: Any) -> bool:
    return bool(
        str(payload.get("source_ir_id") or "") == current.source_ir_id
        and str(payload.get("source_sha256") or "") == current.source_sha256
        and str(payload.get("base_structure_snapshot_id") or "")
        == current.base_structure_snapshot_id
        and int(payload.get("expected_revision_number") or 0) == current.revision_number
        and str(payload.get("expected_effective_snapshot_id") or "")
        == current.effective_snapshot_id
        and str(payload.get("expected_effective_tree_sha256") or "")
        == current.effective_tree_sha256
        and str(payload.get("expected_revision_chain_sha256") or "")
        == current.revision_chain_sha256
        and payload.get("expected_head_revision_id") == current.head_revision_id
    )


def _result_from_proposal(
    proposal: StructureOptimizationProposalOutcomeV1,
) -> dict[str, Any]:
    repair = proposal.repair
    return {
        "schema_version": "structure-optimization-result/v1",
        "status": proposal.status.value,
        "source_ir_id": proposal.source_ir_id,
        "base_structure_snapshot_id": proposal.base_structure_snapshot_id,
        "base_revision": proposal.expected_revision_number,
        "previous_effective_snapshot_id": proposal.expected_effective_snapshot_id,
        "overview": {
            "document_region_count": len(proposal.overview.document_regions),
            "issue_count": len(proposal.overview.issues),
            "region_kinds": [
                region.kind.value for region in proposal.overview.document_regions
            ],
            "reason_codes": sorted(
                {
                    reason_code
                    for issue in proposal.overview.issues
                    for reason_code in issue.reason_codes
                }
            ),
        },
        "repair": {
            "operation_count": len(repair.operations) if repair is not None else 0,
            "operations": (
                [operation.operation for operation in repair.operations]
                if repair is not None
                else []
            ),
        },
        "provider_runs": [
            run.model_dump(mode="json") for run in proposal.provider_runs
        ],
        "revision": None,
        "changes": [],
        "impact": None,
        "impact_summary": None,
        "rerun": None,
    }


def _stale_result(
    payload: dict[str, Any],
    *,
    reason_code: str,
    proposal: StructureOptimizationProposalOutcomeV1 | None = None,
) -> dict[str, Any]:
    result = (
        _result_from_proposal(proposal)
        if proposal is not None
        else {
            "schema_version": "structure-optimization-result/v1",
            "source_ir_id": str(payload.get("source_ir_id") or ""),
            "base_structure_snapshot_id": str(
                payload.get("base_structure_snapshot_id") or ""
            ),
            "base_revision": int(payload.get("expected_revision_number") or 0),
            "previous_effective_snapshot_id": str(
                payload.get("expected_effective_snapshot_id") or ""
            ),
            "overview": {
                "document_region_count": 0,
                "issue_count": 0,
                "region_kinds": [],
                "reason_codes": [],
            },
            "repair": {"operation_count": 0, "operations": []},
            "provider_runs": [],
            "revision": None,
            "changes": [],
            "impact": None,
            "impact_summary": None,
            "rerun": None,
        }
    )
    return {
        **result,
        "status": "STALE",
        "reason_code": reason_code,
    }


def _requires_reextraction_result(
    payload: dict[str, Any],
    *,
    source_ir_status: str,
    reason_code: str,
    proposal: StructureOptimizationProposalOutcomeV1 | None = None,
) -> dict[str, Any]:
    return {
        **_stale_result(
            payload,
            reason_code=reason_code,
            proposal=proposal,
        ),
        "status": "REQUIRES_REEXTRACTION",
        "source_ir_status": source_ir_status,
        "required_source_ir_status": "COMPLETE",
        "next_action": "reextract_source",
    }


def _optimization_snapshot_from_durable(job: DurableJob) -> dict[str, Any]:
    result = dict(job.result) if isinstance(job.result, dict) else None
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "stage": str(
            (job.progress or {}).get("stage")
            or (result or {}).get("status")
            or job.payload.get("stage")
            or "QUEUED"
        ),
        "optimization_status": (
            str(result.get("status") or "") or None if result is not None else None
        ),
        "result": result,
        "error_code": job.error_code,
        "created_at_utc": job.created_at.isoformat(),
        "updated_at_utc": job.updated_at.isoformat(),
        "finished_at_utc": (
            job.updated_at.isoformat()
            if job.status
            in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
            else ""
        ),
    }


def _public_local_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else None
    return {
        "job_id": str(job.get("job_id") or ""),
        "status": str(job.get("status") or "IDLE"),
        "stage": str(job.get("stage") or "IDLE"),
        "optimization_status": (
            str(result.get("status") or "") or None if result is not None else None
        ),
        "result": dict(result) if result is not None else None,
        "error_code": str(job.get("error_code") or ""),
        "created_at_utc": str(job.get("created_at_utc") or ""),
        "updated_at_utc": str(job.get("updated_at_utc") or ""),
        "finished_at_utc": str(job.get("finished_at_utc") or ""),
    }


def _idle_optimization_snapshot() -> dict[str, Any]:
    return {
        "job_id": None,
        "status": "IDLE",
        "stage": "IDLE",
        "optimization_status": None,
        "result": None,
        "error_code": "",
        "created_at_utc": "",
        "updated_at_utc": "",
        "finished_at_utc": "",
    }


def _optimization_response(
    project_id: str,
    document_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": STRUCTURE_OPTIMIZATION_JOB_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "job": snapshot,
        "serving_impact": "NONE",
    }


def _update_local_job(
    document_id: str,
    job_id: str,
    **updates: Any,
) -> None:
    with _LOCAL_OPTIMIZATION_LOCK:
        job = _LOCAL_OPTIMIZATION_JOBS.get(document_id)
        if job is None or str(job.get("job_id") or "") != job_id:
            return
        job.update(updates)
        job["updated_at_utc"] = datetime.now(timezone.utc).isoformat()


__all__ = [
    "STRUCTURE_OPTIMIZATION_JOB_SCHEMA_VERSION",
    "get_document_structure_optimization_status",
    "run_durable_structure_optimization",
    "start_document_structure_optimization",
    "structure_optimization_is_running",
]
