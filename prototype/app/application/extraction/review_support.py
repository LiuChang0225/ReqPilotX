from __future__ import annotations

import logging
from typing import Any

from app import db
from app.application.errors import ApplicationError
from app.application.extraction import (
    build_llm_requirement_review_reference_candidate,
    evaluate_llm_requirement_review,
    validate_llm_requirement_review,
)
from app.application.runtime import RUNTIME_STATE
from app.domain.extraction.llm_requirement_review import (
    LlmRequirementModelPrediction,
    LlmRequirementReviewDelta,
    LlmRequirementReviewedResult,
    LlmRequirementReviewSession,
)
from app.ports.llm_requirement_review_repository import (
    LlmRequirementReviewIncompatibleError,
    LlmRequirementReviewRepositoryError,
    LlmRequirementReviewRepositoryPort,
)

logger = logging.getLogger(__name__)

def _bind_llm_requirement_review_session(
    *,
    project_id: str,
    document_id: str,
    shadow: dict[str, Any],
) -> dict[str, Any]:
    from app.application.extraction.document_jobs import _LLM_EXTRACTION_JOB_LOCK
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    review_payload = shadow.get("llm_review")
    if not isinstance(review_payload, dict):
        return shadow
    with _LLM_EXTRACTION_JOB_LOCK:
        try:
            prediction = LlmRequirementModelPrediction.model_validate(
                review_payload.get("model_prediction")
            )
            reviewed_result = LlmRequirementReviewedResult.model_validate(
                review_payload.get("reviewed_result")
            )
            delta = LlmRequirementReviewDelta.model_validate(
                review_payload.get("delta")
            )
            document = db.get_sor_document(document_id)
            if (
                not document
                or str(document.get("project_id") or "") != project_id
                or str(document.get("source_sha256") or "") != prediction.source_sha256
            ):
                raise ValueError(
                    "SOR document identity changed before review persistence"
                )
            validate_llm_requirement_review(prediction, reviewed_result)
            expected_delta = evaluate_llm_requirement_review(
                prediction,
                reviewed_result,
            )
            if delta != expected_delta:
                raise ValueError("LLM review delta does not match the reviewed result")
            session = _llm_requirement_review_repository().create_or_get(
                project_id=project_id,
                document_id=document_id,
                prediction=prediction,
                reviewed_result=reviewed_result,
                delta=delta,
            )
        except (ValueError, LlmRequirementReviewRepositoryError) as exc:
            raise ApplicationError(
                status_code=500,
                detail={
                    "code": "llm_requirement_review_persistence_failed",
                    "message": (
                        "The LLM result could not be opened for structured review."
                    ),
                },
                headers=_ASSET_RESPONSE_HEADERS,
            ) from exc
    bound = dict(shadow)
    bound["llm_review"] = _serialize_llm_requirement_review_session(session)
    return bound

def _llm_requirement_review_repository() -> LlmRequirementReviewRepositoryPort:
    if RUNTIME_STATE.container is not None:
        return RUNTIME_STATE.container.llm_review_repository
    factory = RUNTIME_STATE.test_llm_review_repository_factory
    if factory is not None:
        return factory()
    raise RuntimeError("LLM review persistence is not initialized.")

def _llm_requirement_review_incompatible_http_error(
    error: LlmRequirementReviewIncompatibleError,
) -> ApplicationError:
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    return ApplicationError(
        status_code=409,
        detail={
            "code": "llm_requirement_review_schema_incompatible",
            "message": (
                "This review was created by an older source-binding contract. "
                "Regenerate the document draft; stored offsets are not migrated."
            ),
            "stored_schema_version": error.stored_schema_version,
            "current_schema_version": error.current_schema_version,
        },
        headers=_ASSET_RESPONSE_HEADERS,
    )

def _require_project_document(project_id: str, document_id: str) -> dict[str, Any]:
    from app.application.projects import _require_project

    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document.get("project_id") != project_id:
        raise ApplicationError(
            status_code=404,
            detail="SOR document not found for this project.",
        )
    return document

def _serialize_llm_requirement_review_session(
    session: LlmRequirementReviewSession,
) -> dict[str, Any]:
    reference_candidate = build_llm_requirement_review_reference_candidate(session)
    return {
        "schema_version": session.schema_version,
        "persistence": {
            "status": "PERSISTED",
            "session_id": session.session_id,
            "revision": session.reviewed_result.revision,
            "created_at_utc": session.created_at_utc.isoformat(),
            "updated_at_utc": session.updated_at_utc.isoformat(),
        },
        "model_prediction": session.prediction.model_dump(mode="json"),
        "reviewed_result": session.reviewed_result.model_dump(mode="json"),
        "delta": session.delta.model_dump(mode="json"),
        "reference_candidate": reference_candidate.model_dump(mode="json"),
    }

def _llm_requirement_review_response(
    *,
    project_id: str,
    document_id: str,
    session: LlmRequirementReviewSession,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "document_id": document_id,
        "serving_impact": "NONE",
        "requirement_set_id": None,
        "requirement_set_created": False,
        "requirement_set_activated": False,
        "llm_review": _serialize_llm_requirement_review_session(session),
    }

def _llm_requirement_confirmation_response(
    *,
    project_id: str,
    document_id: str,
    session: LlmRequirementReviewSession,
    review_node_id: str,
    confirmed: bool,
    changed: bool,
) -> dict[str, Any]:
    """Return only the state needed to acknowledge one confirmation command."""

    return {
        "schema_version": "llm-requirement-confirmation-response/v1",
        "project_id": project_id,
        "document_id": document_id,
        "serving_impact": "NONE",
        "confirmation": {
            "session_id": session.session_id,
            "prediction_id": session.prediction.prediction_id,
            "revision": session.reviewed_result.revision,
            "status": session.reviewed_result.status.value,
            "review_node_id": review_node_id,
            "confirmed": confirmed,
            "changed": changed,
            "updated_at_utc": session.updated_at_utc.isoformat(),
        },
    }
