from __future__ import annotations

from typing import Protocol, runtime_checkable

from reqpilotx.domain.extraction.llm_requirement_review import (
    LlmRequirementModelPrediction,
    LlmRequirementReviewDelta,
    LlmRequirementReviewedResult,
    LlmRequirementReviewSession,
)


class LlmRequirementReviewRepositoryError(RuntimeError):
    pass


class LlmRequirementReviewConflictError(LlmRequirementReviewRepositoryError):
    pass


class LlmRequirementReviewIncompatibleError(LlmRequirementReviewRepositoryError):
    def __init__(
        self,
        *,
        stored_schema_version: str,
        current_schema_version: str,
    ) -> None:
        self.stored_schema_version = stored_schema_version
        self.current_schema_version = current_schema_version
        super().__init__(
            "stored LLM review schema "
            f"{stored_schema_version or '<missing>'} is incompatible with "
            f"{current_schema_version}"
        )


@runtime_checkable
class LlmRequirementReviewRepositoryPort(Protocol):
    def create_or_get(
        self,
        *,
        project_id: str,
        document_id: str,
        prediction: LlmRequirementModelPrediction,
        reviewed_result: LlmRequirementReviewedResult,
        delta: LlmRequirementReviewDelta,
    ) -> LlmRequirementReviewSession: ...

    def get(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
    ) -> LlmRequirementReviewSession | None: ...

    def get_latest(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
    ) -> LlmRequirementReviewSession | None: ...

    def get_for_prediction(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        prediction_id: str,
    ) -> LlmRequirementReviewSession | None: ...

    def update(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        expected_revision: int,
        reviewed_result: LlmRequirementReviewedResult,
        delta: LlmRequirementReviewDelta,
    ) -> LlmRequirementReviewSession: ...

    def update_confirmation(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        expected_revision: int,
        review_node_id: str,
        confirmed: bool,
        reviewed_result: LlmRequirementReviewedResult,
        delta: LlmRequirementReviewDelta,
    ) -> LlmRequirementReviewSession: ...

    def purge_document(
        self,
        *,
        project_id: str,
        document_id: str,
    ) -> int: ...


__all__ = [
    "LlmRequirementReviewConflictError",
    "LlmRequirementReviewIncompatibleError",
    "LlmRequirementReviewRepositoryError",
    "LlmRequirementReviewRepositoryPort",
]
