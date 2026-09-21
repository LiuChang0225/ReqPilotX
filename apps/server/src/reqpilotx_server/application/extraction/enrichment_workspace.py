from __future__ import annotations

import logging
from threading import Lock
from typing import Any

from reqpilotx_server import db
from reqpilotx_server.adapters.documents.bailian_requirement_atomization_provider import (
    BailianRequirementAtomizationProvider,
)
from reqpilotx_server.adapters.documents.bailian_requirement_refinement_provider import (
    BailianRequirementRefinementProvider,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.extraction.contracts import (
    RequirementAtomizationRunRequest,
    RequirementAtomizationScopeRunRequest,
    RequirementPublicationPreviewRequest,
    RequirementPublicationPreviewRequestV2,
    RequirementPublishDraftRequest,
    RequirementPublishDraftRequestV2,
    RequirementRefinementDecisionRequest,
    RequirementRefinementSuggestionRequest,
)
from reqpilotx_server.application.extraction.llm_provision_batch import (
    MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS,
)
from reqpilotx_server.application.extraction.requirement_enrichment import (
    build_requirement_atomization_run,
    build_requirement_enrichment_lineage,
    build_requirement_publication_preview,
    build_requirement_refinement_suggestion,
    revise_requirement_refinement_suggestion,
)
from reqpilotx_server.application.runtime import RUNTIME_STATE
from reqpilotx_server.application.transport import (
    ResponseSink,
)
from reqpilotx_server.domain.extraction.llm_requirement_review import (
    LlmRequirementReviewSession,
)
from reqpilotx_server.domain.extraction.requirement_enrichment import (
    RequirementAtomizationRun,
    RequirementEnrichmentCoverageReceipt,
    RequirementRefinementOperation,
    stable_requirement_enrichment_coverage_sha256,
)
from reqpilotx_server.domain.extraction.semantic_terminal_receipts import (
    SemanticUnitTerminalReceipt,
)
from reqpilotx_server.ports.llm_requirement_review_repository import (
    LlmRequirementReviewIncompatibleError,
    LlmRequirementReviewRepositoryError,
)
from reqpilotx_server.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderError,
    RequirementAtomizationProviderPort,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationProviderResult,
)
from reqpilotx_server.ports.requirement_enrichment_repository import (
    RequirementEnrichmentConflictError,
    RequirementEnrichmentRepositoryError,
    RequirementEnrichmentRepositoryPort,
)
from reqpilotx_server.ports.requirement_refinement_provider import (
    RequirementRefinementProviderError,
)
from reqpilotx_server.services.llm_extraction_result_cache import (
    LlmExtractionResultCache,
)

logger = logging.getLogger(__name__)

_REQUIREMENT_ATOMIZATION_LOCKS_GUARD = Lock()

_REQUIREMENT_ATOMIZATION_LOCKS: dict[str, Lock] = {}

_REQUIREMENT_ATOMIZATION_ACTIVE_LINEAGES: set[str] = set()


def create_requirement_atomization_run(
    project_id: str,
    document_id: str,
    session_id: str,
    payload: RequirementAtomizationRunRequest | RequirementAtomizationScopeRunRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    if isinstance(payload, RequirementAtomizationScopeRunRequest):
        from reqpilotx_server.application.extraction.atomization_scope import (
            create_requirement_atomization_scope_run,
        )

        return create_requirement_atomization_scope_run(
            project_id,
            document_id,
            session_id,
            payload,
            response,
        )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    _reject_requirement_enrichment_during_structure_optimization(
        project_id,
        document_id,
    )
    _, session = _require_requirement_enrichment_session(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
        expected_review_revision=payload.expected_review_revision,
    )
    coverage = _require_current_requirement_enrichment_coverage(session)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    repository = _requirement_enrichment_repository()
    expected_lineage = build_requirement_enrichment_lineage(
        session,
        coverage=coverage,
    )
    existing = repository.get_latest_atomization(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
        atomization_revision=0,
    )
    if existing is not None and existing.lineage == expected_lineage:
        return {
            "schema_version": "requirement-atomization-run-response/v1",
            "project_id": project_id,
            "document_id": document_id,
            "serving_impact": "NONE",
            "atomization_run": existing.model_dump(mode="json"),
        }
    lineage_lock = _requirement_atomization_lineage_lock(
        expected_lineage.lineage_sha256
    )
    try:
        with lineage_lock:
            existing = repository.get_latest_atomization(
                project_id=project_id,
                document_id=document_id,
                session_id=session_id,
                atomization_revision=0,
            )
            if existing is not None and existing.lineage == expected_lineage:
                run = existing
            else:
                _set_requirement_atomization_active(
                    expected_lineage.lineage_sha256,
                    active=True,
                )
                try:
                    provider = _CheckpointingRequirementAtomizationProvider(
                        _build_requirement_atomization_execution_provider(
                            project_id,
                            document_id,
                        ),
                        repository,
                        project_id=project_id,
                        document_id=document_id,
                        session_id=session_id,
                        review_revision=session.reviewed_result.revision,
                        lineage_sha256=expected_lineage.lineage_sha256,
                    )
                    run = repository.create_or_get_atomization(
                        build_requirement_atomization_run(
                            session,
                            coverage=coverage,
                            provider=provider,
                            max_concurrent_provider_calls=(
                                MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS
                            ),
                        )
                    )
                finally:
                    _set_requirement_atomization_active(
                        expected_lineage.lineage_sha256,
                        active=False,
                    )
    except RequirementAtomizationProviderError as exc:
        raise _requirement_enrichment_http_error(
            status_code=503,
            code=exc.code,
            message=str(exc),
        ) from exc
    except RequirementEnrichmentConflictError as exc:
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_atomization_conflict",
            message=str(exc),
        ) from exc
    except (ValueError, RequirementEnrichmentRepositoryError) as exc:
        raise _requirement_enrichment_http_error(
            status_code=422,
            code="requirement_atomization_invalid",
            message=str(exc),
        ) from exc
    return {
        "schema_version": "requirement-atomization-run-response/v1",
        "project_id": project_id,
        "document_id": document_id,
        "serving_impact": "NONE",
        "atomization_run": run.model_dump(mode="json"),
    }


def create_requirement_refinement_suggestion(
    project_id: str,
    document_id: str,
    session_id: str,
    payload: RequirementRefinementSuggestionRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    _reject_requirement_enrichment_during_structure_optimization(
        project_id,
        document_id,
    )
    _, session = _require_requirement_enrichment_session(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
        expected_review_revision=payload.expected_review_revision,
    )
    coverage = _require_current_requirement_enrichment_coverage(session)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    repository = _requirement_enrichment_repository()
    run = _require_requirement_atomization_run(
        repository=repository,
        session=session,
        coverage=coverage,
        atomization_run_id=payload.atomization_run_id,
    )
    existing = next(
        (
            item
            for item in repository.list_refinements(
                project_id=project_id,
                document_id=document_id,
                session_id=session_id,
                atomization_run_id=run.atomization_run_id,
            )
            if item.atomic_node_id == payload.atomic_node_id
        ),
        None,
    )
    if existing is not None:
        return {
            "schema_version": "requirement-refinement-suggestion-response/v1",
            "project_id": project_id,
            "document_id": document_id,
            "serving_impact": "NONE",
            "refinement_suggestion": existing.model_dump(mode="json"),
        }
    try:
        suggestion = build_requirement_refinement_suggestion(
            run=run,
            atomic_node_id=payload.atomic_node_id,
            provider=_build_requirement_refinement_provider(
                project_id,
                document_id,
            ),
        )
        suggestion = repository.create_or_get_refinement(suggestion)
    except RequirementRefinementProviderError as exc:
        raise _requirement_enrichment_http_error(
            status_code=503,
            code=exc.code,
            message=str(exc),
        ) from exc
    except RequirementEnrichmentConflictError as exc:
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_refinement_conflict",
            message=str(exc),
        ) from exc
    except (ValueError, RequirementEnrichmentRepositoryError) as exc:
        raise _requirement_enrichment_http_error(
            status_code=422,
            code="requirement_refinement_invalid",
            message=str(exc),
        ) from exc
    return {
        "schema_version": "requirement-refinement-suggestion-response/v1",
        "project_id": project_id,
        "document_id": document_id,
        "serving_impact": "NONE",
        "refinement_suggestion": suggestion.model_dump(mode="json"),
    }


def decide_requirement_refinement_suggestion(
    project_id: str,
    document_id: str,
    session_id: str,
    suggestion_id: str,
    payload: RequirementRefinementDecisionRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    _reject_requirement_enrichment_during_structure_optimization(
        project_id,
        document_id,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    _, session = _require_requirement_enrichment_session(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
    )
    coverage = _require_current_requirement_enrichment_coverage(session)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    repository = _requirement_enrichment_repository()
    current = repository.get_refinement(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
        suggestion_id=suggestion_id,
    )
    if current is None:
        raise _requirement_enrichment_http_error(
            status_code=404,
            code="requirement_refinement_not_found",
            message="The refinement suggestion was not found.",
        )
    run = _require_requirement_atomization_run(
        repository=repository,
        session=session,
        coverage=coverage,
        atomization_run_id=current.atomization_run_id,
    )
    if run.revision != current.atomization_revision:
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_refinement_lineage_stale",
            message="The atomization revision no longer matches the suggestion.",
        )
    try:
        updated = revise_requirement_refinement_suggestion(
            current,
            expected_revision=payload.expected_refinement_revision,
            operation=RequirementRefinementOperation(payload.operation),
            suggested_statement=payload.suggested_statement,
            rationale=payload.rationale,
            decision_note=payload.decision_note,
        )
        updated = repository.update_refinement(
            suggestion=updated,
            expected_revision=payload.expected_refinement_revision,
        )
    except RequirementEnrichmentConflictError as exc:
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_refinement_revision_conflict",
            message=str(exc),
        ) from exc
    except (ValueError, RequirementEnrichmentRepositoryError) as exc:
        status_code = 409 if "revision has changed" in str(exc) else 422
        raise _requirement_enrichment_http_error(
            status_code=status_code,
            code=(
                "requirement_refinement_revision_conflict"
                if status_code == 409
                else "requirement_refinement_invalid"
            ),
            message=str(exc),
        ) from exc
    return {
        "schema_version": "requirement-refinement-decision-response/v1",
        "project_id": project_id,
        "document_id": document_id,
        "serving_impact": "NONE",
        "refinement_suggestion": updated.model_dump(mode="json"),
    }


def create_requirement_publication_preview(
    project_id: str,
    document_id: str,
    session_id: str,
    payload: RequirementPublicationPreviewRequest
    | RequirementPublicationPreviewRequestV2,
    response: ResponseSink,
) -> dict[str, Any]:
    _reject_requirement_enrichment_during_structure_optimization(
        project_id,
        document_id,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    if isinstance(payload, RequirementPublicationPreviewRequestV2):
        document, session = _require_requirement_enrichment_session(
            project_id=project_id,
            document_id=document_id,
            session_id=session_id,
            expected_review_revision=payload.expected_review_revision,
        )
        response.headers.update(_ASSET_RESPONSE_HEADERS)
        try:
            from reqpilotx_server.application.extraction.requirement_enrichment_publication_transport_v2 import (
                create_requirement_publication_binding_preview_v2,
            )

            preview = create_requirement_publication_binding_preview_v2(
                project_id=project_id,
                document_id=document_id,
                document=document,
                session=session,
                payload=payload,
                repository=_requirement_enrichment_repository(),
            )
        except RequirementEnrichmentConflictError as exc:
            raise _requirement_enrichment_http_error(
                status_code=409,
                code="requirement_publication_binding_preview_conflict",
                message=str(exc),
            ) from exc
        except (ValueError, RequirementEnrichmentRepositoryError) as exc:
            raise _requirement_enrichment_http_error(
                status_code=422,
                code="requirement_publication_binding_preview_invalid",
                message=str(exc),
            ) from exc
        counts = _requirement_publication_projection_v2_counts(preview.projection)
        return {
            "schema_version": (
                "requirement-enrichment-publication-preview-response/v2"
            ),
            "project_id": project_id,
            "document_id": document_id,
            "serving_impact": "NONE",
            "analysis_started": False,
            "atomization_invoked": False,
            "classification": {
                "snapshot_id": (preview.projection.entry_classification_snapshot_id),
                "revision": preview.projection.entry_classification_revision,
                "sha256": preview.projection.entry_classification_sha256,
            },
            "binding_preview": preview.model_dump(mode="json"),
            "counts": counts,
        }

    document, session = _require_requirement_enrichment_session(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
        expected_review_revision=payload.expected_review_revision,
    )
    coverage = _require_current_requirement_enrichment_coverage(session)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    repository = _requirement_enrichment_repository()
    run = _require_requirement_atomization_run(
        repository=repository,
        session=session,
        coverage=coverage,
        atomization_run_id=payload.atomization_run_id,
    )
    refinements = repository.list_refinements(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
        atomization_run_id=run.atomization_run_id,
    )
    try:
        preview = build_requirement_publication_preview(
            session=session,
            run=run,
            coverage=coverage,
            refinements=refinements,
            base_active_requirement_set_id=str(
                document.get("active_requirement_set_id") or ""
            ),
        )
        preview = repository.create_or_get_publication_preview(preview)
    except RequirementEnrichmentConflictError as exc:
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_publication_preview_conflict",
            message=str(exc),
        ) from exc
    except (ValueError, RequirementEnrichmentRepositoryError) as exc:
        raise _requirement_enrichment_http_error(
            status_code=422,
            code="requirement_publication_preview_invalid",
            message=str(exc),
        ) from exc
    return {
        "schema_version": "requirement-enrichment-publication-preview-response/v1",
        "project_id": project_id,
        "document_id": document_id,
        "serving_impact": "NONE",
        "publication_preview": preview.model_dump(mode="json"),
    }


def publish_requirement_enrichment_draft(
    project_id: str,
    document_id: str,
    session_id: str,
    payload: RequirementPublishDraftRequest | RequirementPublishDraftRequestV2,
    response: ResponseSink,
) -> dict[str, Any]:
    _reject_requirement_enrichment_during_structure_optimization(
        project_id,
        document_id,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    if isinstance(payload, RequirementPublishDraftRequestV2):
        document, session = _require_requirement_enrichment_session(
            project_id=project_id,
            document_id=document_id,
            session_id=session_id,
            expected_review_revision=payload.expected_review_revision,
        )
        response.headers.update(_ASSET_RESPONSE_HEADERS)
        try:
            from reqpilotx_server.application.extraction.requirement_tree_snapshots import (
                RequirementTreeSnapshotError,
            )
            from reqpilotx_server.application.extraction.requirement_enrichment_publication_transport_v2 import (
                publish_requirement_enrichment_draft_v2,
            )

            publication, requirement_tree, created = (
                publish_requirement_enrichment_draft_v2(
                    project_id=project_id,
                    document_id=document_id,
                    document=document,
                    session=session,
                    payload=payload,
                    repository=_requirement_enrichment_repository(),
                )
            )
        except LookupError as exc:
            raise _requirement_enrichment_http_error(
                status_code=404,
                code="requirement_publication_binding_preview_not_found",
                message=str(exc),
            ) from exc
        except RequirementEnrichmentConflictError as exc:
            raise _requirement_enrichment_http_error(
                status_code=409,
                code="requirement_publication_binding_conflict",
                message=str(exc),
            ) from exc
        except (
            RequirementTreeSnapshotError,
            ValueError,
            RequirementEnrichmentRepositoryError,
            ApplicationError,
        ) as exc:
            raise _requirement_enrichment_http_error(
                status_code=500,
                code="requirement_publication_binding_failed",
                message=str(exc),
            ) from exc
        preview = (
            _requirement_enrichment_repository().get_publication_binding_preview_v2(
                project_id=project_id,
                document_id=document_id,
                session_id=session_id,
                binding_preview_id=publication.binding_preview_id,
            )
        )
        if preview is None:
            raise _requirement_enrichment_http_error(
                status_code=500,
                code="requirement_publication_binding_failed",
                message="The finalized publication preview could not be reloaded.",
            )
        return {
            "schema_version": "requirement-enrichment-publish-draft-response/v2",
            "project_id": project_id,
            "document_id": document_id,
            "serving_impact": "DRAFT_ONLY",
            "analysis_started": False,
            "requirement_set_created": created,
            "requirement_set_activated": False,
            "classification": {
                "snapshot_id": preview.projection.entry_classification_snapshot_id,
                "revision": preview.projection.entry_classification_revision,
                "sha256": preview.projection.entry_classification_sha256,
            },
            "binding": {
                "binding_id": publication.binding_id,
                "sha256": publication.payload_sha256,
                "preview_id": publication.binding_preview_id,
                "preview_sha256": publication.binding_preview_sha256,
                "state": "READY",
            },
            "counts": _requirement_publication_projection_v2_counts(preview.projection),
            "publication": publication.model_dump(mode="json"),
            "requirement_tree": requirement_tree.model_dump(mode="json"),
        }

    _, session = _require_requirement_enrichment_session(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
        expected_review_revision=payload.expected_review_revision,
    )
    coverage = _require_current_requirement_enrichment_coverage(session)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    repository = _requirement_enrichment_repository()
    run = _require_requirement_atomization_run(
        repository=repository,
        session=session,
        coverage=coverage,
        atomization_run_id=payload.atomization_run_id,
    )
    preview = repository.get_publication_preview(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
        preview_id=payload.preview_id,
    )
    if preview is None:
        raise _requirement_enrichment_http_error(
            status_code=404,
            code="requirement_publication_preview_not_found",
            message="The publication preview was not found.",
        )
    if (
        preview.publication_key != payload.publication_key
        or preview.atomization_run_id != run.atomization_run_id
        or preview.atomization_revision != run.revision
    ):
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_publication_binding_mismatch",
            message="The publish request does not match its persisted preview.",
        )
    current_refinements = repository.list_refinements(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
        atomization_run_id=run.atomization_run_id,
    )
    current_preview = build_requirement_publication_preview(
        session=session,
        run=run,
        coverage=coverage,
        refinements=current_refinements,
        base_active_requirement_set_id=preview.base_active_requirement_set_id,
    )
    if current_preview.publication_key != preview.publication_key:
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_publication_preview_stale",
            message=(
                "A refinement changed after this preview was created. "
                "Create a new publication preview before publishing."
            ),
        )
    try:
        publication = repository.publish_draft(preview)
    except RequirementEnrichmentConflictError as exc:
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_publication_conflict",
            message=str(exc),
        ) from exc
    except RequirementEnrichmentRepositoryError as exc:
        raise _requirement_enrichment_http_error(
            status_code=500,
            code="requirement_publication_failed",
            message=str(exc),
        ) from exc
    try:
        from reqpilotx_server.application.extraction.requirement_tree_snapshots import (
            RequirementTreeSnapshotError,
            persist_published_requirement_tree_snapshot,
        )

        requirement_tree = persist_published_requirement_tree_snapshot(
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=publication.requirement_set_id,
            publication_key=preview.publication_key,
            publication_entries=(
                item.model_dump(mode="json") for item in preview.entries
            ),
            expected_source_sha256=session.prediction.source_sha256,
            expected_structure_snapshot_id=(session.prediction.structure_snapshot_id),
        )
    except (RequirementTreeSnapshotError, ValueError, ApplicationError) as exc:
        if publication.created:
            db.discard_requirement_set(publication.requirement_set_id)
        raise _requirement_enrichment_http_error(
            status_code=500,
            code="requirement_tree_snapshot_failed",
            message=(
                "The DRAFT was not kept because its immutable requirement tree "
                f"could not be verified: {exc}"
            ),
        ) from exc
    return {
        "schema_version": "requirement-enrichment-publish-draft-response/v1",
        "project_id": project_id,
        "document_id": document_id,
        "serving_impact": "DRAFT_ONLY",
        "requirement_set_created": publication.created,
        "requirement_set_activated": False,
        "publication": publication.model_dump(mode="json"),
        "requirement_tree": requirement_tree.model_dump(mode="json"),
    }


def get_requirement_enrichment_state(
    project_id: str,
    document_id: str,
    session_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    document, session = _require_requirement_enrichment_session(
        project_id=project_id,
        document_id=document_id,
        session_id=session_id,
        mutation=False,
    )
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    coverage: RequirementEnrichmentCoverageReceipt | None = None
    coverage_error_code = ""
    try:
        coverage = _load_current_requirement_enrichment_coverage(session)
    except ValueError as exc:
        coverage_error_code = str(exc)
    from reqpilotx_server.application.extraction.atomization_scope import (
        _load_current_requirement_tree_for_state_read,
        build_atomization_scope_state_read_view,
        serialize_requirement_atomization_scope_state,
    )
    from reqpilotx_server.application.extraction.entry_classification_repository import (
        get_latest_entry_classification_snapshot_for_document,
    )
    from reqpilotx_server.domain.entry_classification import (
        build_initial_entry_classification_snapshot,
    )

    repository = _requirement_enrichment_repository()
    atomization_scope_read_view = None
    if coverage is not None and coverage.eligible:
        lineage = build_requirement_enrichment_lineage(
            session,
            coverage=coverage,
        )
        atomization_scope_read_view = build_atomization_scope_state_read_view(
            repository.get_atomization_scope_state_snapshot(
                project_id=session.project_id,
                document_id=session.document_id,
                session_id=session.session_id,
                review_revision=session.reviewed_result.revision,
                lineage_sha256=lineage.lineage_sha256,
            ),
            lineage_active=_requirement_atomization_is_active(lineage.lineage_sha256),
        )
    state = _serialize_requirement_enrichment_state(
        session,
        coverage=coverage,
        coverage_error_code=coverage_error_code,
        repository=repository,
        atomization_scope_read_view=atomization_scope_read_view,
    )

    classification_snapshot = None
    try:
        current_tree = _load_current_requirement_tree_for_state_read(
            project_id,
            document_id,
            document,
        )
        if current_tree is not None:
            current_classification_lineage = (
                build_initial_entry_classification_snapshot(current_tree)
            )
            working_tree_sha256 = current_classification_lineage.working_tree_sha256
            classification_snapshot = (
                get_latest_entry_classification_snapshot_for_document(
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=current_tree.source_identity.source_sha256,
                    working_tree_id=working_tree_sha256,
                    working_tree_sha256=working_tree_sha256,
                    effective_structure_id=(
                        current_tree.structure_identity.effective_snapshot_id
                    ),
                    semantic_snapshot_id=(
                        current_classification_lineage.semantic_snapshot_id
                    ),
                    policy_version=current_classification_lineage.policy_version,
                )
            )
    except (ApplicationError, ValueError):
        classification_snapshot = None
    state["atomization_scope"] = serialize_requirement_atomization_scope_state(
        session=session,
        coverage=coverage,
        classification_snapshot=classification_snapshot,
        repository=repository,
        read_view=atomization_scope_read_view,
    )
    state["provider_available"] = _requirement_refinement_provider_available(
        project_id,
        document_id,
    )
    state["atomization_provider_available"] = (
        _requirement_atomization_provider_available(project_id, document_id)
    )
    state["semantic_publication_capability"] = {
        "schema_version": "requirement-enrichment-semantic-publication-capability/v2",
        "preview_request_schema_version": (
            "requirement-enrichment-publication-preview-request/v2"
        ),
        "preview_response_schema_version": (
            "requirement-enrichment-publication-preview-response/v2"
        ),
        "publish_request_schema_version": (
            "requirement-enrichment-publish-draft-request/v2"
        ),
        "publish_response_schema_version": (
            "requirement-enrichment-publish-draft-response/v2"
        ),
        "atomization_states": ["NOT_RUN", "APPLIED"],
        "not_run_preview_invokes_atomization": False,
        "publish_starts_analysis": False,
        "default_dispositions": {
            "TECHNICAL": "ANALYZE",
            "NON_TECHNICAL": "RETAIN",
            "INFORMATION": "REFERENCE",
        },
    }
    return state


def _requirement_publication_projection_v2_counts(
    projection: Any,
) -> dict[str, int]:
    rows = tuple(projection.rows)
    return {
        "total": len(rows),
        "default_analyze": sum(
            item.default_disposition.value == "ANALYZE" for item in rows
        ),
        "default_retain": sum(
            item.default_disposition.value == "RETAIN" for item in rows
        ),
        "reference": sum(item.entry_kind.value == "REFERENCE" for item in rows),
        "atomized": sum(item.atomization_node_id is not None for item in rows),
    }


def _require_requirement_enrichment_session(
    *,
    project_id: str,
    document_id: str,
    session_id: str,
    expected_review_revision: int | None = None,
    mutation: bool = True,
) -> tuple[dict[str, Any], LlmRequirementReviewSession]:
    from reqpilotx_server.application.extraction.document_jobs import (
        _reject_review_mutation_for_stale_structure,
        _reject_review_mutation_while_extraction_runs,
    )
    from reqpilotx_server.application.extraction.review_support import (
        _llm_requirement_review_incompatible_http_error,
        _llm_requirement_review_repository,
        _require_project_document,
    )

    document = _require_project_document(project_id, document_id)
    if mutation:
        _reject_review_mutation_while_extraction_runs(
            project_id=project_id,
            document_id=document_id,
            source_sha256=str(document.get("source_sha256") or ""),
        )
    try:
        session = _llm_requirement_review_repository().get(
            project_id=project_id,
            document_id=document_id,
            session_id=session_id,
        )
    except LlmRequirementReviewIncompatibleError as exc:
        raise _llm_requirement_review_incompatible_http_error(exc) from exc
    except (ValueError, LlmRequirementReviewRepositoryError) as exc:
        raise _requirement_enrichment_http_error(
            status_code=422,
            code="requirement_enrichment_session_invalid",
            message=str(exc),
        ) from exc
    if session is None:
        raise _requirement_enrichment_http_error(
            status_code=404,
            code="requirement_enrichment_session_not_found",
            message="The source-bound review session was not found.",
        )
    if session.prediction.source_sha256 != str(document.get("source_sha256") or ""):
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_enrichment_source_changed",
            message="The source identity no longer matches the review session.",
        )
    if mutation:
        _reject_review_mutation_for_stale_structure(
            project_id=project_id,
            document_id=document_id,
            source_sha256=session.prediction.source_sha256,
            prediction_structure_snapshot_id=(session.prediction.structure_snapshot_id),
        )
    if (
        expected_review_revision is not None
        and session.reviewed_result.revision != expected_review_revision
    ):
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_enrichment_review_revision_conflict",
            message="The review session revision has changed.",
            detail={
                "current_review_revision": session.reviewed_result.revision,
            },
        )
    return document, session


def _require_requirement_atomization_run(
    *,
    repository: RequirementEnrichmentRepositoryPort,
    session: LlmRequirementReviewSession,
    coverage: RequirementEnrichmentCoverageReceipt,
    atomization_run_id: str,
) -> RequirementAtomizationRun:
    run = repository.get_atomization(
        project_id=session.project_id,
        document_id=session.document_id,
        session_id=session.session_id,
        atomization_run_id=atomization_run_id,
    )
    if run is None:
        raise _requirement_enrichment_http_error(
            status_code=404,
            code="requirement_atomization_not_found",
            message="The atomization run was not found.",
        )
    expected_lineage = build_requirement_enrichment_lineage(
        session,
        coverage=coverage,
    )
    if run.lineage != expected_lineage:
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_atomization_lineage_stale",
            message="The atomization run is stale for the current review revision.",
        )
    return run


def _load_current_requirement_enrichment_coverage(
    session: LlmRequirementReviewSession,
) -> RequirementEnrichmentCoverageReceipt:
    from reqpilotx_server.application.extraction.document_jobs import (
        _get_llm_extraction_job,
        _llm_extraction_result_matches_runtime,
    )
    from reqpilotx_server.application.runtime import STORAGE_DIR

    source_sha256 = session.prediction.source_sha256
    candidates: list[dict[str, Any]] = []
    current = _get_llm_extraction_job(
        session.project_id,
        session.document_id,
        source_sha256=source_sha256,
    )
    if (
        isinstance(current, dict)
        and str(current.get("status") or "").upper() == "COMPLETED"
        and isinstance(current.get("result"), dict)
    ):
        candidates.append(current["result"])
    cached = LlmExtractionResultCache(STORAGE_DIR).load(
        project_id=session.project_id,
        document_id=session.document_id,
        source_sha256=source_sha256,
    )
    if isinstance(cached, dict):
        candidates.append(cached)

    for result in candidates:
        if not _llm_extraction_result_matches_runtime(result):
            continue
        review = result.get("llm_review")
        prediction = (
            review.get("model_prediction") if isinstance(review, dict) else None
        )
        if (
            not isinstance(prediction, dict)
            or str(prediction.get("prediction_id") or "")
            != session.prediction.prediction_id
            or str(prediction.get("source_sha256") or "") != source_sha256
            or str(prediction.get("structure_snapshot_id") or "")
            != session.prediction.structure_snapshot_id
        ):
            continue
        try:
            return _parse_requirement_enrichment_coverage(result)
        except (TypeError, ValueError):
            continue
    raise ValueError("requirement_enrichment_coverage_unavailable")


def _parse_requirement_enrichment_coverage(
    result: dict[str, Any],
) -> RequirementEnrichmentCoverageReceipt:
    summary = result.get("semantic_coverage")
    raw_receipts = result.get("semantic_terminal_receipts")
    pipeline = result.get("pipeline")
    semantic = pipeline.get("semantic") if isinstance(pipeline, dict) else None
    if (
        not isinstance(summary, dict)
        or not isinstance(raw_receipts, list)
        or not isinstance(semantic, dict)
    ):
        raise ValueError("requirement_enrichment_coverage_missing")
    receipts = tuple(
        SemanticUnitTerminalReceipt.model_validate(item) for item in raw_receipts
    )

    def coverage_count(name: str, *, minimum: int = 0) -> int:
        value = summary.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError("requirement_enrichment_coverage_count_invalid")
        return value

    total_units = coverage_count("total_units", minimum=1)
    blocked_unit_count = coverage_count("blocked_unit_count")
    if coverage_count("terminal_receipt_count") != len(receipts) or coverage_count(
        "unresolved_unit_count"
    ) != sum(item.status.value == "UNRESOLVED" for item in receipts):
        raise ValueError("requirement_enrichment_coverage_count_mismatch")
    runtime_fields = {
        "planning_runtime_version": str(summary.get("planning_runtime_version") or ""),
        "classifier_runtime_version": str(
            summary.get("classifier_runtime_version") or ""
        ),
        "assembler_runtime_version": str(
            summary.get("assembler_runtime_version") or ""
        ),
        "document_runtime_version": str(summary.get("document_runtime_version") or ""),
    }
    if any(
        runtime_fields[key] != str(semantic.get(key) or "") for key in runtime_fields
    ):
        raise ValueError("requirement_enrichment_coverage_runtime_mismatch")
    coverage_sha256 = stable_requirement_enrichment_coverage_sha256(
        total_units=total_units,
        blocked_unit_count=blocked_unit_count,
        terminal_receipts=receipts,
        **runtime_fields,
    )
    return RequirementEnrichmentCoverageReceipt(
        total_units=total_units,
        blocked_unit_count=blocked_unit_count,
        terminal_receipts=receipts,
        coverage_sha256=coverage_sha256,
        **runtime_fields,
    )


def _require_current_requirement_enrichment_coverage(
    session: LlmRequirementReviewSession,
) -> RequirementEnrichmentCoverageReceipt:
    try:
        coverage = _load_current_requirement_enrichment_coverage(session)
    except ValueError as exc:
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_enrichment_coverage_unavailable",
            message=(
                "A current, runtime-compatible semantic coverage receipt is "
                "required. Rerun content preparation before enrichment."
            ),
        ) from exc
    if not coverage.eligible:
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="requirement_enrichment_coverage_incomplete",
            message=(
                "Atomization requires zero blocked and zero unresolved semantic units."
            ),
            detail={
                "total_units": coverage.total_units,
                "blocked_unit_count": coverage.blocked_unit_count,
                "unresolved_unit_count": coverage.unresolved_unit_count,
            },
        )
    return coverage


def _requirement_enrichment_repository() -> RequirementEnrichmentRepositoryPort:
    if RUNTIME_STATE.container is not None:
        return RUNTIME_STATE.container.requirement_enrichment_repository
    factory = RUNTIME_STATE.test_requirement_enrichment_repository_factory
    if factory is not None:
        return factory()
    raise RuntimeError("Requirement enrichment persistence is not initialized.")


class _CheckpointingRequirementAtomizationProvider:
    """Reuse each source-bound provider answer across retries and restarts."""

    def __init__(
        self,
        provider: RequirementAtomizationProviderPort,
        repository: RequirementEnrichmentRepositoryPort,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._project_id = project_id
        self._document_id = document_id
        self._session_id = session_id
        self._review_revision = review_revision
        self._lineage_sha256 = lineage_sha256

    def atomize(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult:
        cached = self._repository.get_atomization_checkpoint(
            project_id=self._project_id,
            document_id=self._document_id,
            session_id=self._session_id,
            review_revision=self._review_revision,
            lineage_sha256=self._lineage_sha256,
            request=request,
        )
        if cached is not None:
            return cached
        result = self._provider.atomize(request)
        return self._repository.create_or_get_atomization_checkpoint(
            project_id=self._project_id,
            document_id=self._document_id,
            session_id=self._session_id,
            review_revision=self._review_revision,
            lineage_sha256=self._lineage_sha256,
            request=request,
            result=result,
        )


def _requirement_atomization_lineage_lock(lineage_sha256: str) -> Lock:
    with _REQUIREMENT_ATOMIZATION_LOCKS_GUARD:
        return _REQUIREMENT_ATOMIZATION_LOCKS.setdefault(lineage_sha256, Lock())


def _set_requirement_atomization_active(
    lineage_sha256: str,
    *,
    active: bool,
) -> None:
    with _REQUIREMENT_ATOMIZATION_LOCKS_GUARD:
        if active:
            _REQUIREMENT_ATOMIZATION_ACTIVE_LINEAGES.add(lineage_sha256)
        else:
            _REQUIREMENT_ATOMIZATION_ACTIVE_LINEAGES.discard(lineage_sha256)


def _requirement_atomization_is_active(lineage_sha256: str) -> bool:
    with _REQUIREMENT_ATOMIZATION_LOCKS_GUARD:
        return lineage_sha256 in _REQUIREMENT_ATOMIZATION_ACTIVE_LINEAGES


def _build_requirement_atomization_provider(
    project_id: str,
    document_id: str,
) -> RequirementAtomizationProviderPort:
    from reqpilotx_server.application.extraction.provider_support import (
        _build_llm_shadow_semantic_provider,
    )

    plan = _require_enrichment_processing_plan(project_id, document_id)
    if plan.llm_provider == "codex":
        from reqpilotx_server.application.runtime import (
            _agent_customer_data_enabled,
            _codex_requirement_atomization_provider,
        )

        if not _agent_customer_data_enabled():
            raise ApplicationError(
                status_code=409,
                code="codex_customer_data_not_authorized",
                detail={
                    "reason_code": "codex_customer_data_not_authorized",
                    "message": (
                        "Codex requirement decomposition is disabled until the "
                        "local customer-data authorization is enabled."
                    ),
                    "blocked_by": ["agent_customer_data_authorization"],
                    "settings_path": "/settings",
                },
            )
        try:
            provider = _codex_requirement_atomization_provider()
        except RuntimeError as exc:
            raise ApplicationError(
                status_code=503,
                code="codex_atomization_runtime_unavailable",
                detail={
                    "reason_code": "codex_atomization_runtime_unavailable",
                    "message": "Codex requirement decomposition is not initialized.",
                    "blocked_by": ["codex.agent"],
                    "settings_path": "/settings",
                },
            ) from exc
        if not isinstance(provider, RequirementAtomizationProviderPort):
            raise ApplicationError(
                status_code=503,
                code="codex_atomization_runtime_invalid",
                detail={
                    "reason_code": "codex_atomization_runtime_invalid",
                    "message": "Codex decomposition has an invalid runtime binding.",
                    "blocked_by": ["codex.agent"],
                    "settings_path": "/settings",
                },
            )
        return provider
    return BailianRequirementAtomizationProvider(
        _build_llm_shadow_semantic_provider(plan)
    )


def _build_requirement_atomization_execution_provider(
    project_id: str,
    document_id: str,
) -> RequirementAtomizationProviderPort:
    return _build_requirement_atomization_provider(project_id, document_id)


def _requirement_atomization_provider_available(
    project_id: str,
    document_id: str,
) -> bool:
    """Report configuration readiness without issuing a remote request."""

    try:
        _build_requirement_atomization_provider(project_id, document_id)
    except (ApplicationError, TypeError, ValueError):
        return False
    return True


def _build_requirement_refinement_provider(
    project_id: str,
    document_id: str,
) -> BailianRequirementRefinementProvider:
    from reqpilotx_server.application.extraction.provider_support import (
        _build_llm_shadow_semantic_provider,
    )

    plan = _require_enrichment_processing_plan(project_id, document_id)
    return BailianRequirementRefinementProvider(
        _build_llm_shadow_semantic_provider(plan)
    )


def _requirement_refinement_provider_available(
    project_id: str,
    document_id: str,
) -> bool:
    """Report configuration readiness without issuing a remote request."""

    try:
        _build_requirement_refinement_provider(project_id, document_id)
    except (ApplicationError, TypeError, ValueError):
        return False
    return True


def _require_enrichment_processing_plan(
    project_id: str,
    document_id: str,
):
    from pydantic import ValidationError

    from reqpilotx_server.application.extraction.document_jobs import (
        _get_llm_extraction_job,
        _llm_extraction_result_matches_runtime,
    )
    from reqpilotx_server.application.processing_profiles import (
        require_capabilities,
        required_llm_capability_ids,
        resolve_processing_plan,
    )
    from reqpilotx_server.domain.extraction.processing_profile import ResolvedProcessingPlanV1

    document = db.get_sor_document(document_id)
    if document is None or str(document.get("project_id") or "") != project_id:
        raise ApplicationError(status_code=404, detail="Source document not found.")
    source_sha256 = str(document.get("source_sha256") or "")
    job = _get_llm_extraction_job(
        project_id,
        document_id,
        source_sha256=source_sha256,
    )
    candidates: list[dict[str, Any]] = []
    if (
        isinstance(job, dict)
        and isinstance(job.get("result"), dict)
        and str(job.get("status") or "").upper() == "COMPLETED"
    ):
        candidates.append(job["result"])
    from reqpilotx_server.application.runtime import STORAGE_DIR

    cached = LlmExtractionResultCache(STORAGE_DIR).load(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
    )
    if isinstance(cached, dict):
        candidates.append(cached)
    plan = None
    for result in candidates:
        if not _llm_extraction_result_matches_runtime(result):
            continue
        try:
            candidate_plan = ResolvedProcessingPlanV1.model_validate(
                result.get("processing_plan")
            )
        except (ValidationError, TypeError, ValueError):
            continue
        if candidate_plan.source_sha256 == source_sha256:
            plan = candidate_plan
            break
    if plan is None:
        plan = resolve_processing_plan(
            project_id=project_id,
            document_id=document_id,
            skip_document_capability_gate=True,
        )
    if plan.llm_provider == "none":
        raise ApplicationError(
            status_code=409,
            code="requirement_enrichment_llm_not_selected",
            detail={
                "reason_code": "requirement_enrichment_llm_not_selected",
                "message": ("Atomization and refinement require a ready LLM provider."),
                "blocked_by": ["llm_provider"],
                "settings_path": f"/projects/{project_id}/settings",
            },
        )
    require_capabilities(
        required_llm_capability_ids(plan.llm_provider),
        operation="requirement.enrichment",
    )
    return plan


def _serialize_requirement_enrichment_state(
    session: LlmRequirementReviewSession,
    *,
    coverage: RequirementEnrichmentCoverageReceipt | None,
    coverage_error_code: str = "",
    repository: RequirementEnrichmentRepositoryPort | None = None,
    atomization_scope_read_view: Any | None = None,
) -> dict[str, Any]:
    repository = repository or _requirement_enrichment_repository()
    eligible_direct_node_count = sum(
        item.include_in_view
        and item.semantic_class.value == "REQUIREMENT"
        and item.decision_mode.value == "DIRECT"
        for item in session.reviewed_result.nodes
    )
    expected_lineage = (
        build_requirement_enrichment_lineage(
            session,
            coverage=coverage,
        )
        if coverage is not None and coverage.eligible
        else None
    )
    if expected_lineage is not None and atomization_scope_read_view is None:
        from reqpilotx_server.application.extraction.atomization_scope import (
            build_atomization_scope_state_read_view,
        )

        atomization_scope_read_view = build_atomization_scope_state_read_view(
            repository.get_atomization_scope_state_snapshot(
                project_id=session.project_id,
                document_id=session.document_id,
                session_id=session.session_id,
                review_revision=session.reviewed_result.revision,
                lineage_sha256=expected_lineage.lineage_sha256,
            ),
            lineage_active=_requirement_atomization_is_active(
                expected_lineage.lineage_sha256
            ),
        )
    scope_receipt = (
        atomization_scope_read_view.snapshot.receipt
        if atomization_scope_read_view is not None
        else None
    )
    if scope_receipt is not None:
        run = repository.get_atomization(
            project_id=session.project_id,
            document_id=session.document_id,
            session_id=session.session_id,
            atomization_run_id=scope_receipt.atomization_run_id,
        )
    elif (
        atomization_scope_read_view is not None
        and atomization_scope_read_view.snapshot.attempt is not None
    ):
        run = None
    else:
        # Preserve the pre-W4 implicit atomization path only when no explicit
        # scope attempt exists. Once an attempt exists, its atomic read view is
        # authoritative and an older implicit run must not hide its state.
        run = repository.get_latest_atomization(
            project_id=session.project_id,
            document_id=session.document_id,
            session_id=session.session_id,
        )
    if run is not None and (
        coverage is None or not coverage.eligible or run.lineage != expected_lineage
    ):
        run = None
    latest_scope_attempt = (
        atomization_scope_read_view.attempt_payload
        if atomization_scope_read_view is not None
        else None
    )
    checkpoint_count = (
        atomization_scope_read_view.snapshot.checkpoint_count
        if atomization_scope_read_view is not None and run is None
        else eligible_direct_node_count
        if run is not None
        else 0
    )
    if latest_scope_attempt is not None:
        attempt_state = str(latest_scope_attempt["status"])
        progress_state = (
            "COMPLETED"
            if attempt_state == "SUCCEEDED"
            else "RUNNING"
            if attempt_state == "RUNNING"
            else "PARTIAL"
        )
        progress_completed_count = (
            eligible_direct_node_count
            if attempt_state == "SUCCEEDED"
            else len(latest_scope_attempt["completed_review_node_ids"])
        )
        progress_resumable = bool(latest_scope_attempt["checkpoint_resumable"])
    else:
        progress_state = (
            "COMPLETED"
            if run is not None
            else "RUNNING"
            if expected_lineage is not None
            and _requirement_atomization_is_active(expected_lineage.lineage_sha256)
            else "PARTIAL"
            if checkpoint_count
            else "NOT_STARTED"
        )
        progress_completed_count = checkpoint_count
        progress_resumable = bool(run is None and checkpoint_count)
    atomization_progress = {
        "schema_version": "requirement-atomization-progress/v1",
        "state": progress_state,
        "completed_direct_node_count": min(
            progress_completed_count,
            eligible_direct_node_count,
        ),
        "total_direct_node_count": eligible_direct_node_count,
        "resumable": progress_resumable,
    }
    refinements = (
        repository.list_refinements(
            project_id=session.project_id,
            document_id=session.document_id,
            session_id=session.session_id,
            atomization_run_id=run.atomization_run_id,
        )
        if run is not None
        else ()
    )
    preview = (
        repository.get_latest_publication_preview(
            project_id=session.project_id,
            document_id=session.document_id,
            session_id=session.session_id,
            atomization_run_id=run.atomization_run_id,
        )
        if run is not None
        else None
    )
    preview_stale = False
    if preview is not None and run is not None:
        current_preview = build_requirement_publication_preview(
            session=session,
            run=run,
            coverage=coverage,
            refinements=refinements,
            base_active_requirement_set_id=(preview.base_active_requirement_set_id),
        )
        document = db.get_sor_document(session.document_id)
        preview_stale = bool(
            current_preview.publication_key != preview.publication_key
            or document is None
            or str(document.get("active_requirement_set_id") or "")
            != preview.base_active_requirement_set_id
        )
    publication = (
        repository.get_publication_for_preview(preview_id=preview.preview_id)
        if preview is not None
        else None
    )
    return {
        "schema_version": "requirement-enrichment-state/v1",
        "project_id": session.project_id,
        "document_id": session.document_id,
        "session_id": session.session_id,
        "review_revision": session.reviewed_result.revision,
        "review_nodes": [
            {
                **item.model_dump(mode="json"),
                "text": _reviewed_requirement_node_text(session, item),
                "confirmed": (
                    item.review_node_id
                    in set(session.reviewed_result.confirmed_review_node_ids)
                ),
            }
            for item in session.reviewed_result.nodes
        ],
        "confirmed_review_node_ids": list(
            session.reviewed_result.confirmed_review_node_ids
        ),
        "eligible_direct_node_count": eligible_direct_node_count,
        "atomization_progress": atomization_progress,
        "coverage": (
            {
                **coverage.model_dump(mode="json"),
                "unresolved_unit_count": coverage.unresolved_unit_count,
                "eligible": coverage.eligible,
            }
            if coverage is not None
            else None
        ),
        "coverage_error_code": coverage_error_code,
        "atomization_run": (run.model_dump(mode="json") if run is not None else None),
        "refinements": [item.model_dump(mode="json") for item in refinements],
        "publication_preview": (
            preview.model_dump(mode="json") if preview is not None else None
        ),
        "publication_preview_stale": preview_stale,
        "publication": (
            publication.model_dump(mode="json") if publication is not None else None
        ),
    }


def _reviewed_requirement_node_text(
    session: LlmRequirementReviewSession,
    node: Any,
) -> str:
    prediction_by_unit = {
        item.semantic_unit_id: item for item in session.prediction.nodes
    }
    parts: list[str] = []
    for span in node.source_spans:
        prediction = prediction_by_unit.get(span.semantic_unit_id)
        if prediction is None:
            continue
        text = tuple(prediction.review_text)
        parts.append("".join(text[span.start_offset : span.end_offset]))
    return "".join(parts).strip()


def _requirement_enrichment_http_error(
    *,
    status_code: int,
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> ApplicationError:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    return ApplicationError(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            **(detail or {}),
        },
        headers=_ASSET_RESPONSE_HEADERS,
    )


def _reject_requirement_enrichment_during_structure_optimization(
    project_id: str,
    document_id: str,
) -> None:
    from reqpilotx_server.application.extraction.structure_optimization_jobs import (
        structure_optimization_is_running,
    )

    if structure_optimization_is_running(project_id, document_id):
        raise _requirement_enrichment_http_error(
            status_code=409,
            code="structure_optimization_in_progress",
            message=(
                "Wait for Structure optimization to finish before starting "
                "atomization, refinement, or publication."
            ),
        )
