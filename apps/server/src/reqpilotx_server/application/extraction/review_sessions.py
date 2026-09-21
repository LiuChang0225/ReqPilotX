from __future__ import annotations

import logging
from typing import Any

from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.extraction import (
    evaluate_llm_requirement_review,
    validate_llm_requirement_review,
)
from reqpilotx_server.application.extraction.contracts import (
    LlmRequirementConfirmationRequest,
    LlmRequirementReviewUpdateRequest,
)
from reqpilotx_server.application.transport import (
    ResponseSink,
)
from reqpilotx_server.domain.extraction.llm_requirement_review import (
    LlmRequirementReviewedResult,
    LlmRequirementReviewStatus,
)
from reqpilotx_server.ports.llm_requirement_review_repository import (
    LlmRequirementReviewConflictError,
    LlmRequirementReviewIncompatibleError,
    LlmRequirementReviewRepositoryError,
)

logger = logging.getLogger(__name__)

def get_llm_requirement_review_session(
    project_id: str,
    document_id: str,
    session_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    """Load an editable shadow review without touching formal serving state."""
    from reqpilotx_server.application.extraction.review_support import (
        _llm_requirement_review_incompatible_http_error,
        _llm_requirement_review_repository,
        _llm_requirement_review_response,
        _require_project_document,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS


    _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    try:
        session = _llm_requirement_review_repository().get(
            project_id=project_id,
            document_id=document_id,
            session_id=session_id,
        )
    except LlmRequirementReviewIncompatibleError as exc:
        raise _llm_requirement_review_incompatible_http_error(exc) from exc
    except ValueError as exc:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "llm_requirement_review_session_id_invalid",
                "message": str(exc),
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc
    if session is None:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "llm_requirement_review_not_found",
                "message": "LLM requirement review session was not found.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    return _llm_requirement_review_response(
        project_id=project_id,
        document_id=document_id,
        session=session,
    )

def update_llm_requirement_review_session(
    project_id: str,
    document_id: str,
    session_id: str,
    payload: LlmRequirementReviewUpdateRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    """Validate and persist one structured human-review revision."""
    from reqpilotx_server.application.extraction.document_jobs import (
        _reject_review_mutation_for_stale_structure,
        _reject_review_mutation_while_extraction_runs,
    )
    from reqpilotx_server.application.extraction.review_support import (
        _llm_requirement_review_incompatible_http_error,
        _llm_requirement_review_repository,
        _llm_requirement_review_response,
        _require_project_document,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS


    document = _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    _reject_review_mutation_while_extraction_runs(
        project_id=project_id,
        document_id=document_id,
        source_sha256=str(document.get("source_sha256") or ""),
    )
    repository = _llm_requirement_review_repository()
    try:
        current = repository.get(
            project_id=project_id,
            document_id=document_id,
            session_id=session_id,
        )
    except LlmRequirementReviewIncompatibleError as exc:
        raise _llm_requirement_review_incompatible_http_error(exc) from exc
    except ValueError as exc:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "llm_requirement_review_session_id_invalid",
                "message": str(exc),
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc
    if current is None:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "llm_requirement_review_not_found",
                "message": "LLM requirement review session was not found.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    if current.prediction.source_sha256 != str(document.get("source_sha256") or ""):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "llm_requirement_review_source_changed",
                "message": "The source identity no longer matches this review session.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    _reject_review_mutation_for_stale_structure(
        project_id=project_id,
        document_id=document_id,
        source_sha256=current.prediction.source_sha256,
        prediction_structure_snapshot_id=(current.prediction.structure_snapshot_id),
    )

    try:
        reviewed_result = LlmRequirementReviewedResult(
            prediction_id=current.prediction.prediction_id,
            revision=payload.expected_revision + 1,
            status=LlmRequirementReviewStatus(payload.status),
            reviewer_label=payload.reviewer_label.strip(),
            nodes=payload.nodes,
            confirmed_review_node_ids=(
                payload.confirmed_review_node_ids
                if payload.confirmed_review_node_ids is not None
                else current.reviewed_result.confirmed_review_node_ids
            ),
        )
        validate_llm_requirement_review(current.prediction, reviewed_result)
        delta = evaluate_llm_requirement_review(
            current.prediction,
            reviewed_result,
        )
        updated = repository.update(
            project_id=project_id,
            document_id=document_id,
            session_id=session_id,
            expected_revision=payload.expected_revision,
            reviewed_result=reviewed_result,
            delta=delta,
        )
    except LlmRequirementReviewConflictError as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "llm_requirement_review_revision_conflict",
                "message": str(exc),
                "current_revision": current.reviewed_result.revision,
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc
    except (ValueError, LlmRequirementReviewRepositoryError) as exc:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "llm_requirement_review_invalid",
                "message": str(exc),
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc

    return _llm_requirement_review_response(
        project_id=project_id,
        document_id=document_id,
        session=updated,
    )

def update_llm_requirement_review_confirmation(
    project_id: str,
    document_id: str,
    session_id: str,
    payload: LlmRequirementConfirmationRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    """Persist one confirm/undo command without retransmitting all review nodes."""
    from reqpilotx_server.application.extraction.document_jobs import (
        _reject_review_mutation_for_stale_structure,
        _reject_review_mutation_while_extraction_runs,
    )
    from reqpilotx_server.application.extraction.review_support import (
        _llm_requirement_confirmation_response,
        _llm_requirement_review_incompatible_http_error,
        _llm_requirement_review_repository,
        _require_project_document,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS


    document = _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    _reject_review_mutation_while_extraction_runs(
        project_id=project_id,
        document_id=document_id,
        source_sha256=str(document.get("source_sha256") or ""),
    )
    repository = _llm_requirement_review_repository()
    try:
        current = repository.get(
            project_id=project_id,
            document_id=document_id,
            session_id=session_id,
        )
    except LlmRequirementReviewIncompatibleError as exc:
        raise _llm_requirement_review_incompatible_http_error(exc) from exc
    except ValueError as exc:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "llm_requirement_review_session_id_invalid",
                "message": str(exc),
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc
    if current is None:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "llm_requirement_review_not_found",
                "message": "LLM requirement review session was not found.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    if current.prediction.source_sha256 != str(document.get("source_sha256") or ""):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "llm_requirement_review_source_changed",
                "message": "The source identity no longer matches this review session.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    _reject_review_mutation_for_stale_structure(
        project_id=project_id,
        document_id=document_id,
        source_sha256=current.prediction.source_sha256,
        prediction_structure_snapshot_id=(current.prediction.structure_snapshot_id),
    )
    if payload.expected_revision != current.reviewed_result.revision:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "llm_requirement_review_revision_conflict",
                "message": "review session revision has changed",
                "current_revision": current.reviewed_result.revision,
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    node_ids = tuple(node.review_node_id for node in current.reviewed_result.nodes)
    if payload.review_node_id not in set(node_ids):
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "llm_requirement_review_node_not_found",
                "message": "The review node does not belong to this session.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    confirmed_ids = set(current.reviewed_result.confirmed_review_node_ids)
    if payload.confirmed:
        confirmed_ids.add(payload.review_node_id)
    else:
        confirmed_ids.discard(payload.review_node_id)
    ordered_confirmed_ids = tuple(
        node_id for node_id in node_ids if node_id in confirmed_ids
    )
    if (
        payload.expected_revision == current.reviewed_result.revision
        and ordered_confirmed_ids == current.reviewed_result.confirmed_review_node_ids
    ):
        return _llm_requirement_confirmation_response(
            project_id=project_id,
            document_id=document_id,
            session=current,
            review_node_id=payload.review_node_id,
            confirmed=payload.confirmed,
            changed=False,
        )
    try:
        reviewed_result = LlmRequirementReviewedResult(
            prediction_id=current.prediction.prediction_id,
            revision=payload.expected_revision + 1,
            status=LlmRequirementReviewStatus.DRAFT,
            reviewer_label=current.reviewed_result.reviewer_label,
            nodes=current.reviewed_result.nodes,
            confirmed_review_node_ids=ordered_confirmed_ids,
        )
        validate_llm_requirement_review(current.prediction, reviewed_result)
        delta = evaluate_llm_requirement_review(
            current.prediction,
            reviewed_result,
        )
        updated = repository.update_confirmation(
            project_id=project_id,
            document_id=document_id,
            session_id=session_id,
            expected_revision=payload.expected_revision,
            review_node_id=payload.review_node_id,
            confirmed=payload.confirmed,
            reviewed_result=reviewed_result,
            delta=delta,
        )
    except LlmRequirementReviewConflictError as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "llm_requirement_review_revision_conflict",
                "message": str(exc),
                "current_revision": current.reviewed_result.revision,
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc
    except (ValueError, LlmRequirementReviewRepositoryError) as exc:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "llm_requirement_review_invalid",
                "message": str(exc),
            },
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc

    return _llm_requirement_confirmation_response(
        project_id=project_id,
        document_id=document_id,
        session=updated,
        review_node_id=payload.review_node_id,
        confirmed=payload.confirmed,
        changed=True,
    )
