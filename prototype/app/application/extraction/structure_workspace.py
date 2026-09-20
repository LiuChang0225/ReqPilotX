from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Literal

from app import db
from app.adapters.documents.pdf_source_ir_adapter import (
    PDF_SOURCE_IR_ADAPTER_VERSION,
    PdfSourceIRAdapter,
)
from app.adapters.documents.preflight import LocalDocumentPreflight
from app.application.errors import ApplicationError
from app.application.extraction.build_source_ir import build_source_ir
from app.application.extraction.contracts import (
    StructureMutationBindingRequest,
    StructurePatchRequest,
    StructureUndoRequest,
)
from app.application.extraction.orchestrate_structure_agent import (
    StructureManualMutationResult,
    apply_manual_structure_operations,
    derive_structure_derivation_scope,
    undo_current_structure_revision,
)
from app.application.extraction.resolve_structure import (
    DEFAULT_STRUCTURE_RESOLVER_VERSION,
    build_structure_tree,
)
from app.application.extraction.revise_structure import (
    apply_structure_patch,
    validate_structure_patch_proposal,
    validate_structure_revision_chain,
)
from app.application.agent_workflow.contracts import AgentStructureApplyReceiptV1
from app.application.extraction.serialize_structure_workbench import (
    STRUCTURE_WORKBENCH_SCHEMA_VERSION,
    serialize_structure_recovery_workbench,
    serialize_structure_workbench,
)
from app.application.extraction.structure_presentation import (
    STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION,
)
from app.application.transport import (
    BinaryArtifact,
    RequestContext,
    ResponseSink,
)
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.extraction.requirement_tree import RequirementTreeState
from app.domain.extraction.structure import StructureTreeSnapshot
from app.domain.extraction.structure_revision import (
    EffectiveStructureSnapshotV1,
    StructurePatchProposalV1,
    StructureRevisionAction,
    StructureRevisionRejected,
    StructureRevisionRejectionCode,
    StructureRevisionV1,
)
from app.infrastructure.runtime_settings import DatabaseBackend
from app.ports.llm_requirement_review_repository import (
    LlmRequirementReviewIncompatibleError,
    LlmRequirementReviewRepositoryError,
)
from app.ports.requirement_enrichment_repository import (
    RequirementEnrichmentRepositoryError,
)
from app.services.llm_extraction_result_cache import (
    LlmExtractionResultCache,
)

logger = logging.getLogger(__name__)

_STRUCTURE_MUTATION_LOCK = RLock()
_STRUCTURE_REVISION_CONTEXT_CACHE_LOCK = RLock()
_MAX_CACHED_STRUCTURE_REVISION_CONTEXTS = 3
_StructureRevisionContext = tuple[
    SourceIRSnapshot,
    StructureTreeSnapshot,
    tuple[StructureRevisionV1, ...],
]
_ValidatedStructureRevisionContext = tuple[
    _StructureRevisionContext,
    EffectiveStructureSnapshotV1,
]
_StructureRevisionContextCacheKey = tuple[str, ...]
_STRUCTURE_REVISION_CONTEXT_CACHE: OrderedDict[
    _StructureRevisionContextCacheKey,
    _ValidatedStructureRevisionContext,
] = OrderedDict()
_STRUCTURE_SERVING_JOB_KEY = "_structure_serving_job"


def _canonical_json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _source_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _structure_revision_context_cache_key(
    *,
    path: Path,
    expected_source_sha256: str,
    agent: dict[str, Any],
) -> _StructureRevisionContextCacheKey:
    base_identity = (
        agent.get("base_structure_identity")
        if isinstance(agent.get("base_structure_identity"), dict)
        else {}
    )
    effective_identity = (
        agent.get("effective_structure_identity")
        if isinstance(agent.get("effective_structure_identity"), dict)
        else {}
    )
    return (
        str(path.resolve()),
        expected_source_sha256,
        PDF_SOURCE_IR_ADAPTER_VERSION,
        DEFAULT_STRUCTURE_RESOLVER_VERSION,
        _canonical_json_sha256(agent.get("processing_plan")),
        _canonical_json_sha256(agent.get("revision_chain")),
        _canonical_json_sha256(base_identity),
        _canonical_json_sha256(effective_identity),
    )


def _clear_structure_revision_context_cache() -> None:
    with _STRUCTURE_REVISION_CONTEXT_CACHE_LOCK:
        _STRUCTURE_REVISION_CONTEXT_CACHE.clear()


def _structure_api_error(
    code: str,
    message: str,
    *,
    status_code: int = 409,
    reason_code: str | None = None,
    latest_structure_agent: dict[str, Any] | None = None,
    latest_structure_workbench: dict[str, Any] | None = None,
) -> ApplicationError:
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    detail: dict[str, Any] = {"code": code, "message": message}
    if reason_code is not None:
        detail["reason_code"] = reason_code
    if latest_structure_agent is not None:
        detail["latest_structure_agent"] = latest_structure_agent
    if latest_structure_workbench is not None:
        detail["latest_structure_workbench"] = latest_structure_workbench
    return ApplicationError(
        status_code=status_code,
        detail=detail,
        headers=_ASSET_RESPONSE_HEADERS,
    )


def _serialized_structure_mutation(
    function: Callable[..., dict[str, Any]],
) -> Callable[..., dict[str, Any]]:
    @wraps(function)
    def locked(*args: Any, **kwargs: Any) -> dict[str, Any]:
        with _STRUCTURE_MUTATION_LOCK:
            return function(*args, **kwargs)

    return locked


def _structure_agent_job_payload(
    *,
    project_id: str,
    document_id: str,
    document: dict[str, Any],
    require_final: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from app.application.extraction.document_jobs import (
        _get_llm_extraction_job,
        _get_latest_successful_llm_extraction_job,
        _llm_extraction_cache_matches_runtime,
        _llm_extraction_result_matches_runtime,
        _llm_job_visual_strategy,
        _restored_llm_extraction_job,
    )
    from app.application.runtime import STORAGE_DIR

    source_sha256 = str(document.get("source_sha256") or "")
    job = _get_llm_extraction_job(
        project_id,
        document_id,
        source_sha256=source_sha256,
    )
    if job is None and re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        job = _restored_llm_extraction_job(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
    if job is None:
        raise _structure_api_error(
            "structure_agent_result_unavailable",
            "No source-bound Document Requirement Agent result is available.",
            status_code=404,
        )
    status = str(job.get("status") or "").upper()
    payload = job.get("result") if status == "COMPLETED" else job.get("partial_result")
    serving_job: dict[str, Any] | None = None
    should_try_cached_result = status in {
        "FAILED",
        "INTERRUPTED",
        "CANCELLED",
    } or (
        not isinstance(payload, dict)
        and status in {"PENDING", "RUNNING", "CANCEL_REQUESTED"}
    )
    if should_try_cached_result:
        cached = LlmExtractionResultCache(STORAGE_DIR).load(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
        if isinstance(cached, dict) and _llm_extraction_cache_matches_runtime(cached):
            payload = copy.deepcopy(cached)
            serving_job = {
                "job_id": "",
                "project_id": project_id,
                "document_id": document_id,
                "source_sha256": source_sha256,
                "status": "COMPLETED",
                "stage": "cached_previous_result",
                "error_code": "",
            }
            cached_agent = (
                payload.get("structure_agent")
                if isinstance(payload.get("structure_agent"), dict)
                else None
            )
            if isinstance(cached_agent, dict):
                cached_agent["editable"] = False
                cached_agent["read_only_reason"] = (
                    "structure_derivation_rerun_in_progress"
                    if status in {"PENDING", "RUNNING", "CANCEL_REQUESTED"}
                    else "structure_derivation_rerun_not_complete"
                )
    if not isinstance(payload, dict) and status in {
        "PENDING",
        "RUNNING",
        "CANCEL_REQUESTED",
    }:
        previous = _get_latest_successful_llm_extraction_job(
            project_id,
            document_id,
            source_sha256=source_sha256,
        )
        previous_payload = previous.get("result") if previous is not None else None
        if isinstance(
            previous_payload, dict
        ) and _llm_extraction_result_matches_runtime(
            previous_payload,
            expected_visual_strategy=_llm_job_visual_strategy(previous),
        ):
            payload = copy.deepcopy(previous_payload)
            serving_job = {**previous, "status": "COMPLETED"}
            previous_agent = (
                payload.get("structure_agent")
                if isinstance(payload.get("structure_agent"), dict)
                else None
            )
            if isinstance(previous_agent, dict):
                previous_agent["editable"] = False
                previous_agent["read_only_reason"] = (
                    "structure_derivation_rerun_in_progress"
                )
    if not isinstance(payload, dict) and status in {
        "FAILED",
        "INTERRUPTED",
        "CANCELLED",
    }:
        previous = _get_latest_successful_llm_extraction_job(
            project_id,
            document_id,
            source_sha256=source_sha256,
        )
        if previous is not None:
            previous_payload = previous.get("result")
            if isinstance(previous_payload, dict):
                payload = copy.deepcopy(previous_payload)
                serving_job = {
                    **previous,
                    "status": "COMPLETED",
                }
                previous_agent = (
                    payload.get("structure_agent")
                    if isinstance(payload.get("structure_agent"), dict)
                    else None
                )
                if isinstance(previous_agent, dict):
                    previous_agent["editable"] = False
                    previous_agent["read_only_reason"] = (
                        "structure_derivation_rerun_not_complete"
                    )
    if require_final and status != "COMPLETED":
        latest_payload = payload if isinstance(payload, dict) else None
        latest_agent = (
            latest_payload.get("structure_agent")
            if isinstance(latest_payload, dict)
            and isinstance(latest_payload.get("structure_agent"), dict)
            else None
        )
        raise _structure_api_error(
            "structure_result_read_only",
            "Structure mutations require a completed final result.",
            latest_structure_agent=latest_agent,
        )
    if not isinstance(payload, dict):
        raise _structure_api_error(
            "structure_agent_result_unavailable",
            "The current Agent result has no Structure payload.",
            status_code=404,
        )
    agent = payload.get("structure_agent")
    if not isinstance(agent, dict):
        raise _structure_api_error(
            "structure_agent_schema_incompatible",
            "This result uses an unsupported schema and must be regenerated.",
        )
    if str(payload.get("source_sha256") or source_sha256) != source_sha256:
        raise _structure_api_error(
            "structure_source_binding_mismatch",
            "The Agent result is not bound to the current document source.",
        )
    if serving_job is not None:
        job[_STRUCTURE_SERVING_JOB_KEY] = serving_job
    return job, payload, agent


def _structure_serving_job(job: dict[str, Any]) -> dict[str, Any]:
    """Return the completed artifact identity used only for read projection.

    The outer job remains the newest attempt and is the only value serialized
    as the top-level extraction status or accepted by mutation guards.
    """

    serving_job = job.get(_STRUCTURE_SERVING_JOB_KEY)
    if (
        isinstance(serving_job, dict)
        and str(serving_job.get("status") or "").upper() == "COMPLETED"
    ):
        return serving_job
    return job


def _rebuild_validated_structure_revision_context(
    *,
    document: dict[str, Any],
    agent: dict[str, Any],
) -> _ValidatedStructureRevisionContext:
    from app.application.runtime import _runtime_record_path

    path = _runtime_record_path(document)
    agent_snapshot = copy.deepcopy(agent)
    expected_source_sha256 = str(document.get("source_sha256") or "")
    if not path.is_file():
        raise _structure_api_error(
            "structure_source_unavailable",
            "The source document is unavailable.",
            status_code=404,
        )
    try:
        actual_source_sha256 = _source_file_sha256(path)
    except OSError as exc:
        raise _structure_api_error(
            "structure_source_unavailable",
            "The source document is unavailable.",
            status_code=404,
        ) from exc
    if actual_source_sha256 != expected_source_sha256:
        raise _structure_api_error(
            "structure_source_binding_mismatch",
            "The source or deterministic base Structure no longer matches.",
        )
    try:
        cache_key = _structure_revision_context_cache_key(
            path=path,
            expected_source_sha256=expected_source_sha256,
            agent=agent_snapshot,
        )
    except (TypeError, ValueError) as exc:
        raise _structure_api_error(
            "structure_agent_identity_mismatch",
            "The stored Structure DTO has a non-canonical identity.",
        ) from exc
    with _STRUCTURE_REVISION_CONTEXT_CACHE_LOCK:
        cached = _STRUCTURE_REVISION_CONTEXT_CACHE.get(cache_key)
        if cached is not None:
            _STRUCTURE_REVISION_CONTEXT_CACHE.move_to_end(cache_key)
            return cached
    try:
        raw_plan = agent_snapshot.get("processing_plan")
        if isinstance(raw_plan, dict):
            from app.application.extraction.processing_pipeline import (
                build_source_ir_for_plan,
            )
            from app.domain.extraction.processing_profile import (
                ResolvedProcessingPlanV1,
            )

            plan = ResolvedProcessingPlanV1.model_validate(raw_plan)
            if plan.source_sha256 != expected_source_sha256:
                raise ValueError("processing plan source hash mismatch")
            source_ir = build_source_ir_for_plan(path, plan=plan)
        else:
            report = LocalDocumentPreflight().inspect(path)
            if report.source_sha256 != expected_source_sha256:
                raise ValueError("source hash mismatch")
            source_ir = build_source_ir(
                path,
                preflight_report=report,
                adapters=(PdfSourceIRAdapter(),),
            )
        base_tree = build_structure_tree(source_ir)
    except Exception as exc:
        raise _structure_api_error(
            "structure_source_binding_mismatch",
            "The source or deterministic base Structure no longer matches.",
        ) from exc
    raw_revisions = agent_snapshot.get("revision_chain")
    try:
        revisions = tuple(
            StructureRevisionV1.model_validate(item)
            for item in (raw_revisions if isinstance(raw_revisions, list) else ())
        )
        current = validate_structure_revision_chain(
            source_ir,
            base_tree,
            revisions,
        )
    except Exception as exc:
        raise _structure_api_error(
            "structure_revision_chain_invalid",
            "The stored Structure revision chain cannot be replayed.",
        ) from exc
    base_identity = (
        agent_snapshot.get("base_structure_identity")
        if isinstance(agent_snapshot.get("base_structure_identity"), dict)
        else {}
    )
    effective_identity = (
        agent_snapshot.get("effective_structure_identity")
        if isinstance(agent_snapshot.get("effective_structure_identity"), dict)
        else {}
    )
    identity_checks = (
        (
            "source_ir_id",
            str(base_identity.get("source_ir_id") or "") == source_ir.source_ir_id,
        ),
        (
            "source_sha256",
            str(base_identity.get("source_sha256") or "")
            == source_ir.source_sha256,
        ),
        (
            "base_snapshot_id",
            str(base_identity.get("snapshot_id") or "")
            == base_tree.structure_snapshot_id,
        ),
        (
            "base_canonical_sha256",
            str(base_identity.get("canonical_sha256") or "")
            == current.base_structure_canonical_sha256,
        ),
        (
            "resolver_id",
            str(base_identity.get("resolver_id") or "") == base_tree.resolver_id,
        ),
        (
            "resolver_version",
            str(base_identity.get("resolver_version") or "")
            == base_tree.resolver_version,
        ),
        (
            "effective_snapshot_id",
            str(effective_identity.get("snapshot_id") or "")
            == current.effective_snapshot_id,
        ),
        (
            "effective_tree_sha256",
            str(effective_identity.get("tree_sha256") or "")
            == current.effective_tree_sha256,
        ),
        (
            "revision_chain_sha256",
            str(effective_identity.get("revision_chain_sha256") or "")
            == current.revision_chain_sha256,
        ),
        (
            "revision_number",
            int(effective_identity.get("revision_number") or 0)
            == current.revision_number,
        ),
        (
            "head_revision_id",
            effective_identity.get("head_revision_id") == current.head_revision_id,
        ),
    )
    identity_mismatches = [name for name, matches in identity_checks if not matches]
    if identity_mismatches:
        raise _structure_api_error(
            "structure_agent_identity_mismatch",
            "The stored Structure DTO does not match its replayed revision chain.",
            reason_code="identity_mismatch:" + ",".join(identity_mismatches),
        )
    context = (source_ir, base_tree, revisions)
    validated_context = (context, current)
    with _STRUCTURE_REVISION_CONTEXT_CACHE_LOCK:
        existing = _STRUCTURE_REVISION_CONTEXT_CACHE.get(cache_key)
        if existing is not None:
            _STRUCTURE_REVISION_CONTEXT_CACHE.move_to_end(cache_key)
            return existing
        _STRUCTURE_REVISION_CONTEXT_CACHE[cache_key] = validated_context
        while (
            len(_STRUCTURE_REVISION_CONTEXT_CACHE)
            > _MAX_CACHED_STRUCTURE_REVISION_CONTEXTS
        ):
            _STRUCTURE_REVISION_CONTEXT_CACHE.popitem(last=False)
    return validated_context


def _rebuild_structure_revision_context(
    *,
    document: dict[str, Any],
    agent: dict[str, Any],
) -> _StructureRevisionContext:
    context, _ = _rebuild_validated_structure_revision_context(
        document=document,
        agent=agent,
    )
    return context


def _serialize_structure_workbench_for_api(
    *,
    project_id: str,
    document_id: str,
    document: dict[str, Any],
    job: dict[str, Any],
    payload: dict[str, Any],
    agent: dict[str, Any],
    source_ir: SourceIRSnapshot | None = None,
    base_tree: StructureTreeSnapshot | None = None,
    revisions: tuple[StructureRevisionV1, ...] = (),
    table_projection_revision_heads: list[dict[str, Any]] | None = None,
    enrichment_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.application.extraction.table_projection_overrides import (
        apply_table_projection_overrides,
    )

    effective_payload = apply_table_projection_overrides(
        project_id=project_id,
        document_id=document_id,
        source_sha256=str(document.get("source_sha256") or ""),
        payload=payload,
        revision_heads=table_projection_revision_heads,
    )
    if enrichment_state is None:
        enrichment_state = _structure_workbench_enrichment_state(
            project_id=project_id,
            document_id=document_id,
            payload=effective_payload,
        )
    return serialize_structure_workbench(
        project_id=project_id,
        document_id=document_id,
        document=document,
        job=_structure_serving_job(job),
        payload=effective_payload,
        structure_agent=agent,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=revisions,
        runtime_source_ir_adapter_version=PDF_SOURCE_IR_ADAPTER_VERSION,
        runtime_structure_resolver_version=DEFAULT_STRUCTURE_RESOLVER_VERSION,
        enrichment_state=enrichment_state,
    )


def _structure_workbench_enrichment_state(
    *,
    project_id: str,
    document_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Load only a valid persisted review lineage for Workbench capabilities."""
    from app.application.extraction.enrichment_workspace import (
        _parse_requirement_enrichment_coverage,
        _requirement_atomization_provider_available,
        _requirement_refinement_provider_available,
        _serialize_requirement_enrichment_state,
    )
    from app.application.extraction.review_support import (
        _llm_requirement_review_repository,
    )

    review = payload.get("llm_review")
    persistence = review.get("persistence") if isinstance(review, dict) else None
    session_id = str(
        persistence.get("session_id") if isinstance(persistence, dict) else ""
    )
    if not session_id:
        return {}
    try:
        session = _llm_requirement_review_repository().get(
            project_id=project_id,
            document_id=document_id,
            session_id=session_id,
        )
        if session is None:
            return {}
        try:
            coverage = _parse_requirement_enrichment_coverage(payload)
            coverage_error_code = ""
        except (TypeError, ValueError) as exc:
            coverage = None
            coverage_error_code = str(exc)
        state = _serialize_requirement_enrichment_state(
            session,
            coverage=coverage,
            coverage_error_code=coverage_error_code,
        )
    except (
        ValueError,
        LlmRequirementReviewIncompatibleError,
        LlmRequirementReviewRepositoryError,
        RequirementEnrichmentRepositoryError,
    ):
        return {}
    state["provider_available"] = _requirement_refinement_provider_available(
        project_id,
        document_id,
    )
    state["atomization_provider_available"] = (
        _requirement_atomization_provider_available(project_id, document_id)
    )
    return state


def _validate_structure_mutation_binding(
    request: StructureMutationBindingRequest,
    *,
    source_ir: SourceIRSnapshot,
    base_tree: StructureTreeSnapshot,
    revisions: tuple[StructureRevisionV1, ...],
    latest_structure_agent: dict[str, Any],
    latest_structure_workbench: dict[str, Any],
) -> None:
    current = validate_structure_revision_chain(source_ir, base_tree, revisions)
    if (
        request.source_ir_id != source_ir.source_ir_id
        or request.source_sha256 != source_ir.source_sha256
        or request.base_structure_snapshot_id != base_tree.structure_snapshot_id
        or request.base_structure_canonical_sha256
        != current.base_structure_canonical_sha256
    ):
        raise _structure_api_error(
            "structure_source_binding_mismatch",
            "The patch source/base identity does not match the current document.",
        )
    if (
        request.source_ir_adapter_version != source_ir.adapter_version
        or request.structure_resolver_version != base_tree.resolver_version
        or request.structure_resolver_version != DEFAULT_STRUCTURE_RESOLVER_VERSION
    ):
        raise _structure_api_error(
            "structure_runtime_binding_mismatch",
            "The patch runtime identity does not match this backend.",
        )
    if (
        request.expected_revision_number != current.revision_number
        or request.expected_effective_snapshot_id != current.effective_snapshot_id
    ):
        raise _structure_api_error(
            "structure_revision_conflict",
            "The Structure revision changed after this edit was prepared.",
            latest_structure_agent=latest_structure_agent,
            latest_structure_workbench=latest_structure_workbench,
        )


def _structure_requirement_owner_ids(
    result: dict[str, Any],
) -> tuple[str, ...]:
    owner_ids: set[str] = set()
    for item in result.get("items") or ():
        if not isinstance(item, dict):
            continue
        target = (
            item.get("target_structure")
            if isinstance(item.get("target_structure"), dict)
            else {}
        )
        owner_id = str(target.get("id") or "")
        if owner_id:
            owner_ids.add(owner_id)
    review = result.get("llm_review")
    prediction = (
        review.get("model_prediction")
        if isinstance(review, dict) and isinstance(review.get("model_prediction"), dict)
        else {}
    )
    for item in prediction.get("nodes") or ():
        if not isinstance(item, dict):
            continue
        owner_id = str(item.get("owner_structure_node_id") or "")
        if owner_id:
            owner_ids.add(owner_id)
    return tuple(sorted(owner_ids))


def _structure_revision_summary(
    revision: StructureRevisionV1,
) -> dict[str, Any]:
    return {
        "revision_id": revision.revision_id,
        "revision_number": revision.revision_number,
        "parent_revision_id": revision.parent_revision_id,
        "action": revision.action.value,
        "patch_id": revision.patch.patch_id if revision.patch is not None else None,
        "undo_target_revision_id": revision.undo_target_revision_id,
        "effective_snapshot_id": revision.effective_snapshot_id,
        "impact": revision.impact.model_dump(mode="json"),
    }


def _mark_structure_derivations(
    result: dict[str, Any],
    *,
    previous_effective_snapshot_id: str,
    current_effective_snapshot_id: str,
    stale_structure_node_ids: tuple[str, ...],
    reusable_structure_node_ids: tuple[str, ...],
) -> None:
    stale = set(stale_structure_node_ids)
    reusable = set(reusable_structure_node_ids)
    for collection_name in ("items", "requirement_groups", "review_groups"):
        collection = result.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            target = (
                item.get("target_structure")
                if isinstance(item.get("target_structure"), dict)
                else {}
            )
            owner_id = str(
                target.get("id")
                or item.get("boundary_structure_node_id")
                or item.get("structure_node_id")
                or ""
            )
            if owner_id in stale:
                item["structure_derivation"] = {
                    "status": "STALE",
                    "effective_snapshot_id": previous_effective_snapshot_id,
                    "required_effective_snapshot_id": current_effective_snapshot_id,
                }
            elif owner_id in reusable:
                item["structure_derivation"] = {
                    "status": "REUSED",
                    "effective_snapshot_id": current_effective_snapshot_id,
                    "reused_from_effective_snapshot_id": (
                        previous_effective_snapshot_id
                    ),
                }
    pipeline = result.get("pipeline")
    if isinstance(pipeline, dict):
        for key in ("semantic", "requirement_view"):
            stage = pipeline.get(key)
            if not isinstance(stage, dict):
                continue
            # A structure-only/local run deliberately has no semantic or
            # requirement-view derivation to invalidate or reuse.  Rewriting
            # NOT_RUN here makes that valid result contradict its own runtime
            # contract and causes the next GET to fail closed as incompatible.
            if str(stage.get("status") or "").upper() == "NOT_RUN":
                continue
            stage["status"] = (
                "STALE_SCOPES_PENDING"
                if stale_structure_node_ids
                else "REUSED_FOR_EFFECTIVE_STRUCTURE"
            )
            stage["effective_snapshot_id"] = current_effective_snapshot_id
            stage["stale_structure_node_count"] = len(stale_structure_node_ids)
    review = result.get("llm_review")
    if isinstance(review, dict):
        review["structure_binding"] = {
            "status": "STALE" if stale_structure_node_ids else "REUSED",
            "previous_effective_snapshot_id": previous_effective_snapshot_id,
            "required_effective_snapshot_id": current_effective_snapshot_id,
            "editable": False,
        }


def _updated_result_after_structure_mutation(
    *,
    result: dict[str, Any],
    agent: dict[str, Any],
    source_ir: SourceIRSnapshot,
    mutation: StructureManualMutationResult,
    action: Literal["PATCH", "UNDO"],
    origin: Literal["HUMAN", "AI"] = "HUMAN",
    provider_run_id: str | None = None,
    optimization_job_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = copy.deepcopy(result)
    updated_agent = copy.deepcopy(agent)
    effective = mutation.apply_result.effective_snapshot
    previous_identity = (
        agent.get("effective_structure_identity")
        if isinstance(agent.get("effective_structure_identity"), dict)
        else {}
    )
    previous_effective_snapshot_id = str(previous_identity.get("snapshot_id") or "")
    owner_ids = _structure_requirement_owner_ids(updated)
    scope = derive_structure_derivation_scope(
        effective_snapshot_id=effective.effective_snapshot_id,
        requirement_structure_node_ids=owner_ids,
        impact=mutation.apply_result.impact,
    )
    updated_agent["effective_structure_identity"] = {
        "snapshot_id": effective.effective_snapshot_id,
        "tree_sha256": effective.effective_tree_sha256,
        "revision_chain_sha256": effective.revision_chain_sha256,
        "revision_number": effective.revision_number,
        "head_revision_id": effective.head_revision_id,
    }
    updated_agent["current_revision"] = _structure_revision_summary(
        mutation.apply_result.revision
    )
    updated_agent["revision_chain"] = [
        revision.model_dump(mode="json") for revision in mutation.revisions
    ]
    updated_agent["revision_summaries"] = [
        _structure_revision_summary(revision) for revision in mutation.revisions
    ]
    updated_agent["effective_nodes"] = [
        node.model_dump(mode="json") for node in effective.nodes
    ]
    updated_agent["effective_node_source_spans"] = [
        binding.model_dump(mode="json") for binding in effective.node_source_spans
    ]
    updated_agent["effective_relations"] = [
        relation.model_dump(mode="json") for relation in effective.relations
    ]
    from app.application.extraction.structure_presentation import (
        build_structure_presentation_index,
    )

    updated["structure_presentation_index"] = build_structure_presentation_index(
        source_ir,
        structure=effective,
    )
    updated_agent["stale_scope"] = {
        "structure_node_ids": list(scope.stale_structure_node_ids),
        "reusable_structure_node_ids": list(scope.reusable_structure_node_ids),
        "affected_section_node_ids": list(
            mutation.apply_result.impact.affected_section_node_ids
        ),
        "changed_relation_ids": list(mutation.apply_result.impact.changed_relation_ids),
    }
    updated_agent["editable"] = True
    updated_agent["read_only_reason"] = None
    if action == "PATCH":
        assert mutation.proposal is not None
        patch = mutation.apply_result.revision.patch
        assert patch is not None
        updated_agent.setdefault("proposal_summaries", []).append(
            {
                "finding_id": None,
                "provider_run_id": provider_run_id,
                "optimization_job_id": optimization_job_id,
                "origin": origin,
                "status": "APPLIED",
                "proposal_id": mutation.proposal.proposal_id,
                "patch_id": patch.patch_id,
                "revision_id": mutation.apply_result.revision.revision_id,
                "operations": [
                    operation.operation for operation in mutation.proposal.operations
                ],
                "rejection_code": None,
            }
        )
        updated_agent.setdefault("patch_summaries", []).append(
            {
                "patch_id": patch.patch_id,
                "accepted_proposal_id": patch.accepted_proposal_id,
                "expected_revision_number": patch.expected_revision_number,
                "operations": [operation.operation for operation in patch.operations],
            }
        )
    stages = []
    for stage in updated_agent.get("stages") or ():
        if not isinstance(stage, dict):
            continue
        item = dict(stage)
        stage_name = str(item.get("stage") or "")
        if stage_name in {"SOURCE", "BASE_STRUCTURE", "STRUCTURE_REPAIR"}:
            item["status"] = "COMPLETE"
        elif stage_name == "REQUIREMENT_CLASSIFICATION":
            item["status"] = "PENDING" if scope.stale_structure_node_ids else "COMPLETE"
            item["stale_structure_node_ids"] = list(scope.stale_structure_node_ids)
        else:
            item["status"] = "PENDING"
        stages.append(item)
    updated_agent["stages"] = stages
    updated_agent["rerun"] = {
        "status": ("REQUIRED" if scope.stale_structure_node_ids else "NOT_REQUIRED"),
        "mode": "AFFECTED_STRUCTURE_SCOPE",
        "stale_structure_node_ids": list(scope.stale_structure_node_ids),
        "reusable_structure_node_ids": list(scope.reusable_structure_node_ids),
    }
    updated["structure_agent"] = updated_agent
    updated["status"] = "REVIEW_REQUIRED"
    _mark_structure_derivations(
        updated,
        previous_effective_snapshot_id=previous_effective_snapshot_id,
        current_effective_snapshot_id=effective.effective_snapshot_id,
        stale_structure_node_ids=scope.stale_structure_node_ids,
        reusable_structure_node_ids=scope.reusable_structure_node_ids,
    )
    return updated, updated_agent


def _persist_structure_mutation_result(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job: dict[str, Any],
    result: dict[str, Any],
) -> None:
    from app.application.extraction.document_jobs import (
        _DURABLE_RESULT_CAS_TOKEN_KEY,
        _LLM_EXTRACTION_JOB_LOCK,
        _LLM_EXTRACTION_JOBS,
        _persist_document_extraction_result_artifact,
        _update_llm_extraction_job,
    )
    from app.application.runtime import (
        RUNTIME_SETTINGS,
        STORAGE_DIR,
        _durable_jobs,
    )

    if str(job.get("job_id") or ""):
        if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
            expected_result = job.get(_DURABLE_RESULT_CAS_TOKEN_KEY)
            if isinstance(expected_result, dict):
                durable_artifact_lifecycle = db.is_postgresql_runtime()
                artifact_operation_id = (
                    f"artifact-cas-{uuid.uuid4().hex}"
                    if durable_artifact_lifecycle
                    else ""
                )
                result_receipt = _persist_document_extraction_result_artifact(
                    result,
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=source_sha256,
                    job_id=(
                        str(job.get("job_id") or "")
                        if durable_artifact_lifecycle
                        else ""
                    ),
                    artifact_operation_id=artifact_operation_id,
                    expected_result=(
                        expected_result if durable_artifact_lifecycle else None
                    ),
                )
                try:
                    cas_arguments: dict[str, Any] = {
                        "job_id": str(job.get("job_id") or ""),
                        "expected_result": expected_result,
                        "result": result_receipt,
                    }
                    if durable_artifact_lifecycle:
                        cas_arguments["artifact_operation_id"] = artifact_operation_id
                    updated = _durable_jobs().compare_and_set_succeeded_result(
                        **cas_arguments
                    )
                except BaseException:
                    if durable_artifact_lifecycle:
                        candidates = db.abort_extraction_result_artifact_cas_operation(
                            operation_id=artifact_operation_id
                        )
                        from app.application.extraction.source_asset_objects import (
                            delete_unreferenced_extraction_result_artifacts,
                        )

                        delete_unreferenced_extraction_result_artifacts(candidates)
                    raise
                if updated is None and durable_artifact_lifecycle:
                    candidates = db.abort_extraction_result_artifact_cas_operation(
                        operation_id=artifact_operation_id
                    )
                    from app.application.extraction.source_asset_objects import (
                        delete_unreferenced_extraction_result_artifacts,
                    )

                    delete_unreferenced_extraction_result_artifacts(candidates)
                elif updated is not None and durable_artifact_lifecycle:
                    from app.application.extraction.source_asset_objects import (
                        delete_unreferenced_extraction_result_artifacts,
                    )

                    old_key = str(expected_result.get("storage_key") or "")
                    if old_key:
                        delete_unreferenced_extraction_result_artifacts((old_key,))
            else:
                updated = None
        else:
            updated = _update_llm_extraction_job(
                document_id,
                str(job.get("job_id") or ""),
                result=result,
            )
        if updated is None:
            raise _structure_api_error(
                "structure_revision_conflict",
                "The extraction job changed while the Structure revision was saved.",
            )
    else:
        with _LLM_EXTRACTION_JOB_LOCK:
            _LLM_EXTRACTION_JOBS[document_id] = {
                **job,
                "result": result,
            }
    LlmExtractionResultCache(STORAGE_DIR).save(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        payload=result,
    )


def _store_structure_rerun_state(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Keep the last stable Structure visible while its derivations rerun."""
    from app.application.extraction.document_jobs import (
        _LLM_EXTRACTION_JOB_LOCK,
        _LLM_EXTRACTION_JOBS,
    )
    from app.application.runtime import STORAGE_DIR

    LlmExtractionResultCache(STORAGE_DIR).save(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        payload=result,
    )
    with _LLM_EXTRACTION_JOB_LOCK:
        current = _LLM_EXTRACTION_JOBS.get(document_id)
        if (
            not current
            or str(current.get("project_id") or "") != project_id
            or str(current.get("source_sha256") or "") != source_sha256
        ):
            return None
        if str(current.get("status") or "").upper() == "COMPLETED":
            current["result"] = result
        else:
            # This field is process-local on purpose. The source-bound full
            # result cache remains the restart-safe copy.
            current["previous_result"] = result
        current["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        return dict(current)


def _queue_structure_derivation_rerun(
    *,
    project_id: str,
    document: dict[str, Any],
    previous_job: dict[str, Any],
    result: dict[str, Any],
    structure_agent: dict[str, Any],
    structure_revisions: tuple[StructureRevisionV1, ...],
) -> dict[str, Any]:
    """Schedule a revision-preserving recompute after one Structure mutation.

    V0 invalidates only the calculated affected scope, but executes a bounded
    full-document semantic run so the new review partition is rebuilt as one
    coherent exact-cover result. The distinction is explicit in the payload;
    true affected-only provider execution remains an optimization, not a
    correctness claim.
    """
    from app.application.extraction.document_jobs import (
        _enqueue_durable_llm_document_extraction,
        _llm_extraction_worker_error_details,
        _queue_local_structure_document_extraction,
        _queue_llm_document_extraction,
    )
    from app.application.extraction.provider_support import (
        _build_llm_shadow_semantic_provider,
    )
    from app.application.runtime import RUNTIME_SETTINGS
    from app.application.extraction.processing_pipeline import (
        processing_plan_visual_strategy,
    )
    from app.application.processing_profiles import resolve_processing_plan
    from app.domain.extraction.processing_profile import ResolvedProcessingPlanV1

    document_id = str(document.get("id") or "")
    source_sha256 = str(document.get("source_sha256") or "")
    rerun = (
        structure_agent.get("rerun")
        if isinstance(structure_agent.get("rerun"), dict)
        else {}
    )
    stale_ids = tuple(rerun.get("stale_structure_node_ids") or ())
    rerun.update(
        {
            "invalidation_scope": "AFFECTED_STRUCTURE_SCOPE",
            "execution_scope": ("FULL_DOCUMENT" if stale_ids else "NOT_REQUIRED"),
            "mode": ("FULL_DOCUMENT_RECOMPUTE" if stale_ids else "NOT_REQUIRED"),
            "job_id": None,
            "error_code": "",
        }
    )
    structure_agent["rerun"] = rerun
    result["structure_agent"] = structure_agent
    if not stale_ids:
        rerun["status"] = "NOT_REQUIRED"
        return previous_job

    rerun["status"] = "QUEUING"
    current_job = (
        _store_structure_rerun_state(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            result=result,
        )
        or previous_job
    )
    try:
        raw_processing_plan = result.get("processing_plan") or structure_agent.get(
            "processing_plan"
        )
        processing_plan = (
            ResolvedProcessingPlanV1.model_validate(raw_processing_plan)
            if isinstance(raw_processing_plan, dict)
            else resolve_processing_plan(
                project_id=project_id,
                document_id=document_id,
            )
        )
        refreshed_visual_strategy = processing_plan_visual_strategy(processing_plan)
        if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
            queued = _enqueue_durable_llm_document_extraction(
                project_id=project_id,
                document=document,
                processing_plan=processing_plan,
                visual_provider="off",
                visual_strategy=refreshed_visual_strategy,
                structure_revision_chain_sha256=str(
                    structure_agent.get("effective_structure_identity", {}).get(
                        "revision_chain_sha256"
                    )
                    or ""
                ),
            )
        elif processing_plan.llm_provider == "none":
            queued = _queue_local_structure_document_extraction(
                project_id=project_id,
                document=document,
                processing_plan=processing_plan,
                structure_revisions=structure_revisions,
            )
        else:
            provider = _build_llm_shadow_semantic_provider(processing_plan)
            queued = _queue_llm_document_extraction(
                project_id=project_id,
                document=document,
                provider=provider,
                processing_plan=processing_plan,
                structure_revisions=structure_revisions,
                authorized_visual_runtime=None,
            )
    except Exception as exc:
        rerun["status"] = "BLOCKED"
        rerun.update(_llm_extraction_worker_error_details(exc))
        result["structure_agent"] = structure_agent
        return (
            _store_structure_rerun_state(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                result=result,
            )
            or current_job
        )

    rerun["status"] = (
        "RUNNING" if str(queued.get("status") or "").upper() == "RUNNING" else "QUEUED"
    )
    rerun["job_id"] = str(queued.get("job_id") or "") or None
    result["structure_agent"] = structure_agent
    return (
        _store_structure_rerun_state(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            result=result,
        )
        or queued
    )


def _map_structure_revision_rejection(
    exc: StructureRevisionRejected,
    *,
    latest_structure_agent: dict[str, Any],
    latest_structure_workbench: dict[str, Any],
) -> ApplicationError:
    if exc.code is StructureRevisionRejectionCode.STALE_BASE_REVISION:
        return _structure_api_error(
            "structure_revision_conflict",
            "The Structure revision changed after this edit was prepared.",
            reason_code=exc.code.value,
            latest_structure_agent=latest_structure_agent,
            latest_structure_workbench=latest_structure_workbench,
        )
    if exc.code in {
        StructureRevisionRejectionCode.SOURCE_BINDING_MISMATCH,
        StructureRevisionRejectionCode.BASE_TREE_MISMATCH,
        StructureRevisionRejectionCode.REVISION_CHAIN_INVALID,
    }:
        return _structure_api_error(
            "structure_source_binding_mismatch",
            "The Structure mutation is not bound to the current source lineage.",
            reason_code=exc.code.value,
        )
    return _structure_api_error(
        "structure_patch_validator_rejected",
        "The Core Structure validator rejected this mutation.",
        status_code=422,
        reason_code=exc.code.value,
        latest_structure_agent=latest_structure_agent,
        latest_structure_workbench=latest_structure_workbench,
    )


def _structure_response_document_identity(
    document: dict[str, Any],
) -> dict[str, Any]:
    return {
        "file_name": str(document.get("file_name") or ""),
        "preview_generation_id": str(document.get("preview_generation_id") or ""),
        "page_count": max(int(document.get("page_count") or 0), 0),
    }


def _structure_response_review_session_id(payload: dict[str, Any]) -> str:
    review = payload.get("llm_review")
    persistence = review.get("persistence") if isinstance(review, dict) else None
    return str(persistence.get("session_id") if isinstance(persistence, dict) else "")


def _structure_response_payload_runtime_identity(
    payload: dict[str, Any],
) -> dict[str, Any]:
    pipeline = payload.get("pipeline")
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    plan = payload.get("processing_plan")
    plan = plan if isinstance(plan, dict) else {}
    identity: dict[str, Any] = {
        "schema_version": str(payload.get("schema_version") or ""),
        "mode": str(payload.get("mode") or ""),
        "requirement_tree_projection_runtime_version": str(
            payload.get("requirement_tree_projection_runtime_version") or ""
        ),
        "processing_plan_schema_version": str(plan.get("schema_version") or ""),
        "processing_plan_canonical_sha256": str(plan.get("canonical_sha256") or ""),
    }
    for layer in ("source_ir", "structure", "semantic", "requirement_view"):
        value = pipeline.get(layer)
        value = value if isinstance(value, dict) else {}
        identity[layer] = {
            key: value.get(key)
            for key in (
                "id",
                "version",
                "classifier_id",
                "classifier_runtime_version",
                "planning_runtime_version",
                "assembler_runtime_version",
                "document_runtime_version",
                "projector_id",
                "status",
            )
            if value.get(key) is not None
        }
    review = payload.get("llm_review")
    review = review if isinstance(review, dict) else {}
    prediction = review.get("model_prediction")
    prediction = prediction if isinstance(prediction, dict) else {}
    reviewed = review.get("reviewed_result")
    reviewed = reviewed if isinstance(reviewed, dict) else {}
    identity["review_schema_versions"] = {
        "session": str(review.get("schema_version") or ""),
        "prediction": str(prediction.get("schema_version") or ""),
        "reviewed_result": str(reviewed.get("schema_version") or ""),
    }
    return identity


def _structure_table_projection_revision_state(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
) -> tuple[list[dict[str, Any]], str]:
    from app.adapters.persistence.table_projection_revisions import (
        list_effective_table_projection_revisions,
    )
    from app.application.extraction.structure_response_cache import (
        canonical_json_sha256,
    )

    if not structure_snapshot_id:
        return [], canonical_json_sha256([])
    heads = list_effective_table_projection_revisions(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        structure_snapshot_id=structure_snapshot_id,
    )
    identity_rows: list[dict[str, Any]] = []
    for head in heads:
        projection_json = str(head.get("projection_json") or "")
        evidence_json = str(head.get("evidence_bindings_json") or "")
        identity_rows.append(
            {
                "id": str(head.get("id") or ""),
                "logical_table_id": str(head.get("logical_table_id") or ""),
                "revision": int(head.get("revision") or 0),
                "action": str(head.get("action") or ""),
                "projection_id": str(head.get("projection_id") or ""),
                "projection_sha256": str(head.get("projection_sha256") or ""),
                "projection_json_sha256": hashlib.sha256(
                    projection_json.encode("utf-8")
                ).hexdigest(),
                "evidence_bindings_json_sha256": hashlib.sha256(
                    evidence_json.encode("utf-8")
                ).hexdigest(),
                "supersedes_id": str(head.get("supersedes_id") or ""),
                "created_at": str(head.get("created_at") or ""),
            }
        )
    identity_rows.sort(
        key=lambda item: (item["logical_table_id"], item["revision"], item["id"])
    )
    return heads, canonical_json_sha256(identity_rows)


def _structure_response_artifact(
    *,
    content: bytes,
    etag: str,
    request: RequestContext,
) -> BinaryArtifact:
    from app.application.extraction.structure_response_cache import (
        if_none_match_matches,
    )

    headers = {
        "Cache-Control": "private, no-cache, max-age=0",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    if if_none_match_matches(request.headers.get("if-none-match", ""), etag):
        return BinaryArtifact(
            content=b"",
            status_code=304,
            headers=headers,
        )
    return BinaryArtifact(
        content=content,
        media_type="application/json",
        headers=headers,
    )


def get_document_structure_agent(
    project_id: str,
    document_id: str,
    response: ResponseSink,
    request: RequestContext | None = None,
) -> dict[str, Any] | BinaryArtifact:
    from app.application.extraction.document_jobs import (
        _document_extraction_result_artifact_receipt_identity,
        _llm_extraction_result_matches_runtime,
        _serialize_llm_extraction_job,
    )
    from app.application.extraction.structure_response_cache import (
        build_complete_structure_response_identity,
        build_static_structure_response_identity,
        cache_structure_response,
        canonical_json_bytes,
        canonical_json_sha256,
        get_structure_response_cache_candidate,
        strong_etag,
        StructureResponseCacheEntry,
    )
    from app.application.extraction.review_support import _require_project_document
    from app.application.extraction.requirement_tree import (
        REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION,
    )
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    document = _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    source_sha256 = str(document.get("source_sha256") or "")
    artifact_receipt = (
        _document_extraction_result_artifact_receipt_identity(
            project_id,
            document_id,
            source_sha256=source_sha256,
        )
        if request is not None
        else {}
    )
    static_response_identity = ""
    if artifact_receipt:
        static_response_identity = build_static_structure_response_identity(
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
        candidate = get_structure_response_cache_candidate(static_response_identity)
        if candidate is not None:
            _heads, table_revision_sha256 = _structure_table_projection_revision_state(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                structure_snapshot_id=(candidate.effective_structure_snapshot_id),
            )
            enrichment_payload = (
                {
                    "llm_review": {
                        "persistence": {
                            "session_id": candidate.review_session_id,
                        }
                    }
                }
                if candidate.review_session_id
                else {}
            )
            enrichment_state = _structure_workbench_enrichment_state(
                project_id=project_id,
                document_id=document_id,
                payload=enrichment_payload,
            )
            enrichment_sha256 = canonical_json_sha256(enrichment_state)
            if (
                table_revision_sha256 == candidate.table_projection_revision_sha256
                and enrichment_sha256 == candidate.enrichment_state_sha256
            ):
                assert request is not None
                return _structure_response_artifact(
                    content=candidate.content,
                    etag=candidate.etag,
                    request=request,
                )
    try:
        job, payload, agent = _structure_agent_job_payload(
            project_id=project_id,
            document_id=document_id,
            document=document,
            require_final=False,
        )
    except ApplicationError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = str(detail.get("code") or "")
        if code not in {
            "structure_agent_result_unavailable",
            "structure_agent_schema_incompatible",
        }:
            raise
        from app.application.extraction.document_jobs import (
            _get_llm_extraction_job,
            _serialize_llm_extraction_job,
        )

        recovery_job = _get_llm_extraction_job(
            project_id,
            document_id,
            source_sha256=source_sha256,
        )
        if (
            not recovery_job
            or str(recovery_job.get("status") or "").upper() != "COMPLETED"
        ):
            raise
        recovery = serialize_structure_recovery_workbench(
            project_id=project_id,
            document_id=document_id,
            document=document,
            job=recovery_job,
            state=RequirementTreeState.REQUIRES_REEXTRACTION,
            detail_code=(
                "RESULT_UNAVAILABLE"
                if code == "structure_agent_result_unavailable"
                else "RESULT_SCHEMA_INCOMPATIBLE"
            ),
        )
        return {
            "project_id": project_id,
            "document_id": document_id,
            "job": _serialize_llm_extraction_job(recovery_job),
            "structure_agent": {
                "editable": False,
                "read_only_reason": "requires_reextraction",
            },
            "structure_workbench": recovery,
            "serving_impact": "NONE",
        }
    source_ir: SourceIRSnapshot | None = None
    base_tree: StructureTreeSnapshot | None = None
    revisions: tuple[StructureRevisionV1, ...] = ()
    if str(_structure_serving_job(job).get("status") or "").upper() == "COMPLETED":
        if not _llm_extraction_result_matches_runtime(payload):
            recovery = serialize_structure_recovery_workbench(
                project_id=project_id,
                document_id=document_id,
                document=document,
                job=job,
                state=RequirementTreeState.REQUIRES_REEXTRACTION,
                detail_code="RUNTIME_INCOMPATIBLE",
            )
            return {
                "project_id": project_id,
                "document_id": document_id,
                "job": _serialize_llm_extraction_job(job),
                "structure_agent": {
                    "editable": False,
                    "read_only_reason": "requires_reextraction",
                },
                "structure_workbench": recovery,
                "serving_impact": "NONE",
            }
        try:
            raw_revisions = agent.get("revision_chain")
            revisions = tuple(
                StructureRevisionV1.model_validate(item)
                for item in (raw_revisions if isinstance(raw_revisions, list) else ())
            )
        except (TypeError, ValueError):
            recovery = serialize_structure_recovery_workbench(
                project_id=project_id,
                document_id=document_id,
                document=document,
                job=job,
                state=RequirementTreeState.REQUIRES_REEXTRACTION,
                detail_code="REVISION_LINEAGE_INCOMPATIBLE",
            )
            return {
                "project_id": project_id,
                "document_id": document_id,
                "job": _serialize_llm_extraction_job(job),
                "structure_agent": {
                    "editable": False,
                    "read_only_reason": "requires_reextraction",
                },
                "structure_workbench": recovery,
                "serving_impact": "NONE",
            }
        # A completed durable result is already source- and runtime-bound.  The
        # read surface projects that immutable DTO directly; replaying the full
        # PDF SourceIR and StructureTree on every page load made a read take
        # seconds.  Mutations, optimization and publication still call
        # _rebuild_structure_revision_context before writing.
    effective_identity = (
        agent.get("effective_structure_identity")
        if isinstance(agent.get("effective_structure_identity"), dict)
        else {}
    )
    table_projection_revision_heads, table_revision_sha256 = (
        _structure_table_projection_revision_state(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            structure_snapshot_id=str(effective_identity.get("snapshot_id") or ""),
        )
    )
    enrichment_state = _structure_workbench_enrichment_state(
        project_id=project_id,
        document_id=document_id,
        payload=payload,
    )
    enrichment_sha256 = canonical_json_sha256(enrichment_state)
    workbench = _serialize_structure_workbench_for_api(
        project_id=project_id,
        document_id=document_id,
        document=document,
        job=job,
        payload=payload,
        agent=agent,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=revisions,
        table_projection_revision_heads=table_projection_revision_heads,
        enrichment_state=enrichment_state,
    )
    from app.application.extraction.document_jobs import (
        _document_extraction_result_artifact_identity,
    )
    from app.application.extraction.runtime_read_models import (
        cache_document_extraction_runtime_read_model,
    )

    artifact_identity = str(artifact_receipt.get("artifact_sha256") or "")
    if not artifact_identity:
        artifact_identity = _document_extraction_result_artifact_identity(
            project_id,
            document_id,
            source_sha256=source_sha256,
        )
    cache_document_extraction_runtime_read_model(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_identity=artifact_identity,
        payload=payload,
        workbench=workbench,
    )
    result = {
        "project_id": project_id,
        "document_id": document_id,
        "job": _serialize_llm_extraction_job(job),
        "structure_agent": agent,
        "structure_workbench": workbench,
        "serving_impact": "NONE",
    }
    if request is None or not artifact_receipt or not static_response_identity:
        return result

    requirement_tree = (
        workbench.get("requirement_tree")
        if isinstance(workbench.get("requirement_tree"), dict)
        else {}
    )
    base_identity = (
        agent.get("base_structure_identity")
        if isinstance(agent.get("base_structure_identity"), dict)
        else {}
    )
    if (
        str(requirement_tree.get("mode") or "") != "WORKING"
        or not str(base_identity.get("snapshot_id") or "")
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(base_identity.get("canonical_sha256") or ""),
        )
        or not str(effective_identity.get("snapshot_id") or "")
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(effective_identity.get("tree_sha256") or ""),
        )
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(effective_identity.get("revision_chain_sha256") or ""),
        )
    ):
        return result

    current_receipt = _document_extraction_result_artifact_receipt_identity(
        project_id,
        document_id,
        source_sha256=source_sha256,
    )
    if current_receipt != artifact_receipt:
        return result
    job_identity = _serialize_llm_extraction_job(job)
    complete_identity = build_complete_structure_response_identity(
        static_identity_sha256=static_response_identity,
        base_structure_identity=base_identity,
        effective_structure_identity=effective_identity,
        revision_chain_sha256=canonical_json_sha256(
            agent.get("revision_chain")
            if isinstance(agent.get("revision_chain"), list)
            else []
        ),
        job_identity=job_identity,
        payload_runtime_identity=(
            _structure_response_payload_runtime_identity(payload)
        ),
        table_projection_revision_sha256=table_revision_sha256,
        enrichment_state_sha256=enrichment_sha256,
    )
    content = canonical_json_bytes(result)
    etag = strong_etag(content)
    cached = cache_structure_response(
        StructureResponseCacheEntry(
            static_identity_sha256=static_response_identity,
            complete_identity_sha256=complete_identity,
            content=content,
            etag=etag,
            effective_structure_snapshot_id=str(
                effective_identity.get("snapshot_id") or ""
            ),
            review_session_id=_structure_response_review_session_id(payload),
            table_projection_revision_sha256=table_revision_sha256,
            enrichment_state_sha256=enrichment_sha256,
        )
    )
    if cached is None:
        cached = StructureResponseCacheEntry(
            static_identity_sha256=static_response_identity,
            complete_identity_sha256=complete_identity,
            content=content,
            etag=etag,
            effective_structure_snapshot_id=str(
                effective_identity.get("snapshot_id") or ""
            ),
            review_session_id=_structure_response_review_session_id(payload),
            table_projection_revision_sha256=table_revision_sha256,
            enrichment_state_sha256=enrichment_sha256,
        )
    return _structure_response_artifact(
        content=cached.content,
        etag=cached.etag,
        request=request,
    )


@_serialized_structure_mutation
def apply_document_structure_patch(
    project_id: str,
    document_id: str,
    payload: StructurePatchRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    from app.application.extraction.document_jobs import (
        _llm_extraction_result_matches_runtime,
        _serialize_llm_extraction_job,
    )
    from app.application.extraction.review_support import _require_project_document
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    document = _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    job, result, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=True,
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
    latest_workbench = _serialize_structure_workbench_for_api(
        project_id=project_id,
        document_id=document_id,
        document=document,
        job=job,
        payload=result,
        agent=agent,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=revisions,
    )
    _validate_structure_mutation_binding(
        payload,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=revisions,
        latest_structure_agent=agent,
        latest_structure_workbench=latest_workbench,
    )
    try:
        mutation = apply_manual_structure_operations(
            source_ir,
            base_tree,
            revisions,
            payload.operations,
            expected_revision_number=payload.expected_revision_number,
            expected_effective_snapshot_id=(payload.expected_effective_snapshot_id),
        )
    except StructureRevisionRejected as exc:
        raise _map_structure_revision_rejection(
            exc,
            latest_structure_agent=agent,
            latest_structure_workbench=latest_workbench,
        ) from exc
    updated_result, updated_agent = _updated_result_after_structure_mutation(
        result=result,
        agent=agent,
        source_ir=source_ir,
        mutation=mutation,
        action="PATCH",
    )
    _persist_structure_mutation_result(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_ir.source_sha256,
        job=job,
        result=updated_result,
    )
    rerun_job = _queue_structure_derivation_rerun(
        project_id=project_id,
        document=document,
        previous_job=job,
        result=updated_result,
        structure_agent=updated_agent,
        structure_revisions=mutation.revisions,
    )
    updated_workbench = _serialize_structure_workbench_for_api(
        project_id=project_id,
        document_id=document_id,
        document=document,
        job=rerun_job,
        payload=updated_result,
        agent=updated_agent,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=mutation.revisions,
    )
    return {
        "schema_version": "structure-mutation-result/v1",
        "accepted": True,
        "action": "PATCH",
        "structure_agent": updated_agent,
        "structure_workbench": updated_workbench,
        "rerun": {
            **updated_agent["rerun"],
            "job": _serialize_llm_extraction_job(rerun_job),
        },
        "serving_impact": "NONE",
    }


@_serialized_structure_mutation
def apply_stored_agent_structure_patch(
    project_id: str,
    document_id: str,
    proposal: StructurePatchProposalV1,
    *,
    command_id: str,
) -> AgentStructureApplyReceiptV1:
    """Apply one durable Agent proposal through the existing validator/CAS path.

    The Agent never calls this function. A first-party Apply action loads the
    immutable proposal record, then invokes this typed use case. Replaying the
    same accepted proposal returns the already-appended revision.
    """

    from app.application.extraction.document_jobs import (
        _llm_extraction_result_matches_runtime,
    )
    from app.application.extraction.review_support import _require_project_document

    if not isinstance(proposal, StructurePatchProposalV1):
        raise TypeError("stored Agent Apply requires StructurePatchProposalV1")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", command_id):
        raise ValueError("command_id has an invalid shape")
    document = _require_project_document(project_id, document_id)
    job, result, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=True,
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
    latest_workbench = _serialize_structure_workbench_for_api(
        project_id=project_id,
        document_id=document_id,
        document=document,
        job=job,
        payload=result,
        agent=agent,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=revisions,
    )
    for revision in revisions:
        patch = revision.patch
        if (
            revision.action is StructureRevisionAction.APPLY
            and patch is not None
            and patch.accepted_proposal_id == proposal.proposal_id
        ):
            if (
                patch.source_ir_id != proposal.source_ir_id
                or patch.source_sha256 != proposal.source_sha256
                or patch.source_ir_canonical_sha256
                != proposal.source_ir_canonical_sha256
                or patch.base_structure_snapshot_id
                != proposal.base_structure_snapshot_id
                or patch.base_structure_canonical_sha256
                != proposal.base_structure_canonical_sha256
                or patch.expected_revision_number != proposal.expected_revision_number
                or patch.expected_effective_snapshot_id
                != proposal.expected_effective_snapshot_id
                or patch.operations != proposal.operations
            ):
                raise _structure_api_error(
                    "agent_structure_proposal_identity_conflict",
                    "The stored Agent proposal ID conflicts with revision history.",
                )
            return AgentStructureApplyReceiptV1(
                structure_revision_id=revision.revision_id,
                structure_revision_number=revision.revision_number,
                effective_structure_snapshot_id=revision.effective_snapshot_id,
                appended=False,
            )
    try:
        patch = validate_structure_patch_proposal(
            proposal,
            source_ir,
            base_tree,
            revisions,
        )
        applied = apply_structure_patch(patch, source_ir, base_tree, revisions)
    except StructureRevisionRejected as exc:
        raise _map_structure_revision_rejection(
            exc,
            latest_structure_agent=agent,
            latest_structure_workbench=latest_workbench,
        ) from exc
    mutation = StructureManualMutationResult(
        proposal=proposal,
        apply_result=applied,
        revisions=(
            revisions if not applied.appended else (*revisions, applied.revision)
        ),
    )
    updated_result, updated_agent = _updated_result_after_structure_mutation(
        result=result,
        agent=agent,
        source_ir=source_ir,
        mutation=mutation,
        action="PATCH",
        origin="AI",
    )
    _persist_structure_mutation_result(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_ir.source_sha256,
        job=job,
        result=updated_result,
    )
    _queue_structure_derivation_rerun(
        project_id=project_id,
        document=document,
        previous_job=job,
        result=updated_result,
        structure_agent=updated_agent,
        structure_revisions=mutation.revisions,
    )
    return AgentStructureApplyReceiptV1(
        structure_revision_id=applied.revision.revision_id,
        structure_revision_number=applied.revision.revision_number,
        effective_structure_snapshot_id=applied.effective_snapshot.effective_snapshot_id,
        appended=applied.appended,
    )


@_serialized_structure_mutation
def undo_document_structure_revision(
    project_id: str,
    document_id: str,
    payload: StructureUndoRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    from app.application.extraction.document_jobs import (
        _llm_extraction_result_matches_runtime,
        _serialize_llm_extraction_job,
    )
    from app.application.extraction.review_support import _require_project_document
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    document = _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    job, result, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=True,
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
    latest_workbench = _serialize_structure_workbench_for_api(
        project_id=project_id,
        document_id=document_id,
        document=document,
        job=job,
        payload=result,
        agent=agent,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=revisions,
    )
    _validate_structure_mutation_binding(
        payload,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=revisions,
        latest_structure_agent=agent,
        latest_structure_workbench=latest_workbench,
    )
    try:
        mutation = undo_current_structure_revision(
            source_ir,
            base_tree,
            revisions,
            expected_revision_number=payload.expected_revision_number,
            expected_effective_snapshot_id=(payload.expected_effective_snapshot_id),
        )
    except StructureRevisionRejected as exc:
        raise _map_structure_revision_rejection(
            exc,
            latest_structure_agent=agent,
            latest_structure_workbench=latest_workbench,
        ) from exc
    updated_result, updated_agent = _updated_result_after_structure_mutation(
        result=result,
        agent=agent,
        source_ir=source_ir,
        mutation=mutation,
        action="UNDO",
    )
    _persist_structure_mutation_result(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_ir.source_sha256,
        job=job,
        result=updated_result,
    )
    rerun_job = _queue_structure_derivation_rerun(
        project_id=project_id,
        document=document,
        previous_job=job,
        result=updated_result,
        structure_agent=updated_agent,
        structure_revisions=mutation.revisions,
    )
    updated_workbench = _serialize_structure_workbench_for_api(
        project_id=project_id,
        document_id=document_id,
        document=document,
        job=rerun_job,
        payload=updated_result,
        agent=updated_agent,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=mutation.revisions,
    )
    return {
        "schema_version": "structure-mutation-result/v1",
        "accepted": True,
        "action": "UNDO",
        "structure_agent": updated_agent,
        "structure_workbench": updated_workbench,
        "rerun": {
            **updated_agent["rerun"],
            "job": _serialize_llm_extraction_job(rerun_job),
        },
        "serving_impact": "NONE",
    }
