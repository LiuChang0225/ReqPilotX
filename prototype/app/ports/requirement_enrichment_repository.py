from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.domain.extraction.requirement_enrichment import (
    RequirementAtomizationRun,
    RequirementPublicationPreview,
    RequirementPublicationResult,
    RequirementRefinementSuggestion,
)
from app.domain.atomization_scope import (
    AtomizationScopeAttemptStatus,
    AtomizationScopeAttemptV1,
    AtomizationScopeReceiptV1,
)
from app.domain.requirement_enrichment_publication_v2 import (
    RequirementEnrichmentPublicationBindingPreviewV2,
    RequirementEnrichmentPublicationBindingV2,
    RequirementEnrichmentPublicationProjectionV2,
    RequirementEnrichmentPublicationStartV2,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderRequest,
    RequirementAtomizationProviderResult,
)


class RequirementEnrichmentRepositoryError(RuntimeError):
    pass


class RequirementEnrichmentConflictError(RequirementEnrichmentRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class AtomizationScopeStateSnapshot:
    """One atomic read of the scope head, latest attempt, and valid checkpoints."""

    receipt: AtomizationScopeReceiptV1 | None
    attempt: AtomizationScopeAttemptV1 | None
    checkpoint_count: int
    valid_checkpoint_review_node_ids: tuple[str, ...] = ()


@runtime_checkable
class RequirementEnrichmentRepositoryPort(Protocol):
    def create_or_get_publication_binding_preview_v2(
        self,
        preview: RequirementEnrichmentPublicationBindingPreviewV2,
    ) -> RequirementEnrichmentPublicationBindingPreviewV2: ...

    def get_publication_binding_preview_v2(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        binding_preview_id: str,
    ) -> RequirementEnrichmentPublicationBindingPreviewV2 | None: ...

    def begin_publication_binding_v2(
        self,
        *,
        preview: RequirementEnrichmentPublicationBindingPreviewV2,
        current_projection: RequirementEnrichmentPublicationProjectionV2,
    ) -> RequirementEnrichmentPublicationStartV2: ...

    def finalize_publication_binding_v2(
        self,
        binding: RequirementEnrichmentPublicationBindingV2,
    ) -> RequirementEnrichmentPublicationBindingV2: ...

    def abort_publication_binding_v2(
        self,
        started: RequirementEnrichmentPublicationStartV2,
    ) -> bool: ...

    def get_publication_binding_v2(
        self,
        *,
        requirement_set_id: str,
        binding_id: str,
    ) -> RequirementEnrichmentPublicationBindingV2 | None: ...

    def get_publication_binding_for_preview_v2(
        self,
        *,
        binding_preview_id: str,
    ) -> RequirementEnrichmentPublicationBindingV2 | None: ...

    def get_atomization_checkpoint(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult | None: ...

    def create_or_get_atomization_checkpoint(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
        request: RequirementAtomizationProviderRequest,
        result: RequirementAtomizationProviderResult,
        scope_attempt_id: str | None = None,
    ) -> RequirementAtomizationProviderResult: ...

    def count_atomization_checkpoints(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
    ) -> int: ...

    def delete_atomization_checkpoint(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
        request: RequirementAtomizationProviderRequest,
        scope_attempt_id: str | None = None,
    ) -> None: ...

    def create_or_get_atomization(
        self,
        run: RequirementAtomizationRun,
    ) -> RequirementAtomizationRun: ...

    def get_atomization(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        atomization_run_id: str,
    ) -> RequirementAtomizationRun | None: ...

    def get_latest_atomization(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        atomization_revision: int | None = None,
    ) -> RequirementAtomizationRun | None: ...

    def get_latest_atomization_scope_receipt(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        lineage_sha256: str,
    ) -> AtomizationScopeReceiptV1 | None: ...

    def get_latest_atomization_scope_attempt(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        lineage_sha256: str,
    ) -> AtomizationScopeAttemptV1 | None: ...

    def get_atomization_scope_state_snapshot(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
    ) -> AtomizationScopeStateSnapshot: ...

    def begin_atomization_scope_attempt(
        self,
        *,
        attempt: AtomizationScopeAttemptV1,
        expected_scope_revision: int,
    ) -> AtomizationScopeAttemptV1: ...

    def heartbeat_atomization_scope_attempt(
        self,
        *,
        attempt_id: str,
        lineage_sha256: str,
        expected_scope_revision: int,
    ) -> AtomizationScopeAttemptV1: ...

    def update_atomization_scope_attempt(
        self,
        *,
        attempt: AtomizationScopeAttemptV1,
        expected_status: AtomizationScopeAttemptStatus,
    ) -> AtomizationScopeAttemptV1: ...

    def get_atomization_scope_receipt_revision(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        lineage_sha256: str,
        scope_revision: int,
    ) -> AtomizationScopeReceiptV1 | None: ...

    def get_atomization_scope_command_result(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        lineage_sha256: str,
        command_id: str,
        command_sha256: str,
    ) -> tuple[AtomizationScopeReceiptV1, RequirementAtomizationRun] | None: ...

    def create_atomization_scope_revision(
        self,
        *,
        run: RequirementAtomizationRun,
        receipt: AtomizationScopeReceiptV1,
        attempt: AtomizationScopeAttemptV1,
        expected_scope_revision: int,
    ) -> tuple[RequirementAtomizationRun, AtomizationScopeReceiptV1, bool]: ...

    def create_or_get_refinement(
        self,
        suggestion: RequirementRefinementSuggestion,
    ) -> RequirementRefinementSuggestion: ...

    def get_refinement(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        suggestion_id: str,
    ) -> RequirementRefinementSuggestion | None: ...

    def list_refinements(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        atomization_run_id: str,
    ) -> tuple[RequirementRefinementSuggestion, ...]: ...

    def update_refinement(
        self,
        *,
        suggestion: RequirementRefinementSuggestion,
        expected_revision: int,
    ) -> RequirementRefinementSuggestion: ...

    def create_or_get_publication_preview(
        self,
        preview: RequirementPublicationPreview,
    ) -> RequirementPublicationPreview: ...

    def get_publication_preview(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        preview_id: str,
    ) -> RequirementPublicationPreview | None: ...

    def get_latest_publication_preview(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        atomization_run_id: str,
    ) -> RequirementPublicationPreview | None: ...

    def get_publication_for_preview(
        self,
        *,
        preview_id: str,
    ) -> RequirementPublicationResult | None: ...

    def publish_draft(
        self,
        preview: RequirementPublicationPreview,
    ) -> RequirementPublicationResult: ...


__all__ = [
    "RequirementEnrichmentConflictError",
    "RequirementEnrichmentRepositoryError",
    "RequirementEnrichmentRepositoryPort",
]
