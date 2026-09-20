from __future__ import annotations

from typing import Any

from app import db
from app.application.extraction.contracts import (
    RequirementPublicationPreviewRequestV2,
    RequirementPublishDraftRequestV2,
)
from app.application.extraction.requirement_enrichment import (
    build_requirement_enrichment_lineage,
    build_requirement_publication_preview,
)
from app.application.extraction.requirement_enrichment_publication_v2 import (
    build_requirement_enrichment_publication_builder_entries_v2,
    build_requirement_enrichment_publication_projection_v2,
    overlay_requirement_enrichment_publication_tree_v2,
)
from app.application.extraction.requirement_tree import (
    build_requirement_tree_snapshot,
)
from app.domain.entry_classification import (
    EntryClassificationSnapshotV1,
    EntryClassificationStatus,
)
from app.domain.extraction.llm_requirement_review import LlmRequirementReviewSession
from app.domain.extraction.requirement_tree import (
    RequirementTreeMode,
    RequirementTreeSnapshotV1,
    RequirementTreeState,
)
from app.domain.requirement_enrichment_publication_v2 import (
    RequirementEnrichmentAtomizationStateV2,
    RequirementEnrichmentPublicationBindingPreviewV2,
    RequirementEnrichmentPublicationBindingV2,
    RequirementEnrichmentPublicationProjectionV2,
    RequirementEnrichmentPublicationStartV2,
    build_requirement_enrichment_publication_binding_preview_v2,
    build_requirement_enrichment_publication_binding_v2,
)
from app.ports.requirement_enrichment_repository import (
    RequirementEnrichmentConflictError,
    RequirementEnrichmentRepositoryPort,
)


def create_requirement_publication_binding_preview_v2(
    *,
    project_id: str,
    document_id: str,
    document: dict[str, Any],
    session: LlmRequirementReviewSession,
    payload: RequirementPublicationPreviewRequestV2,
    repository: RequirementEnrichmentRepositoryPort,
) -> RequirementEnrichmentPublicationBindingPreviewV2:
    projection, _working_tree = _recompute_current_projection_v2(
        project_id=project_id,
        document_id=document_id,
        document=document,
        session=session,
        repository=repository,
        accepted_snapshot_id=(payload.accepted_entry_classification_snapshot_id),
        accepted_revision=payload.accepted_entry_classification_revision,
        accepted_sha256=payload.accepted_entry_classification_sha256,
        atomization_state=RequirementEnrichmentAtomizationStateV2(
            payload.atomization_state
        ),
        atomization_scope_receipt_id=payload.atomization_scope_receipt_id,
        atomization_scope_receipt_sha256=(
            payload.atomization_scope_receipt_sha256
        ),
        atomization_scope_revision=payload.atomization_scope_revision,
        base_active_requirement_set_id=str(
            document.get("active_requirement_set_id") or ""
        ),
    )
    preview = build_requirement_enrichment_publication_binding_preview_v2(
        session_id=session.session_id,
        review_revision=session.reviewed_result.revision,
        base_active_requirement_set_id=str(
            document.get("active_requirement_set_id") or ""
        ),
        projection=projection,
    )
    return repository.create_or_get_publication_binding_preview_v2(preview)


def publish_requirement_enrichment_draft_v2(
    *,
    project_id: str,
    document_id: str,
    document: dict[str, Any],
    session: LlmRequirementReviewSession,
    payload: RequirementPublishDraftRequestV2,
    repository: RequirementEnrichmentRepositoryPort,
) -> tuple[
    RequirementEnrichmentPublicationBindingV2,
    RequirementTreeSnapshotV1,
    bool,
]:
    preview = repository.get_publication_binding_preview_v2(
        project_id=project_id,
        document_id=document_id,
        session_id=session.session_id,
        binding_preview_id=payload.binding_preview_id,
    )
    if preview is None:
        raise LookupError("publication binding preview was not found")
    if preview.payload_sha256 != payload.binding_preview_sha256:
        raise RequirementEnrichmentConflictError(
            "publish request does not match its persisted binding preview"
        )
    if preview.review_revision != session.reviewed_result.revision:
        raise RequirementEnrichmentConflictError(
            "review changed after publication binding preview"
        )
    ready_binding = repository.get_publication_binding_for_preview_v2(
        binding_preview_id=preview.binding_preview_id,
    )
    if ready_binding is not None:
        from app.application.extraction.requirement_tree_snapshots import (
            load_requirement_set_tree_snapshot,
        )

        requirement_set = db.get_requirement_set(ready_binding.requirement_set_id)
        if (
            requirement_set is None
            or str(
                requirement_set.get("enrichment_publication_binding_id") or ""
            )
            != ready_binding.binding_id
            or str(
                requirement_set.get("enrichment_publication_binding_sha256") or ""
            )
            != ready_binding.payload_sha256
        ):
            raise RequirementEnrichmentConflictError(
                "READY publication binding no longer matches its Requirement Set"
            )
        return (
            ready_binding,
            load_requirement_set_tree_snapshot(requirement_set),
            False,
        )

    current_projection, accepted_working_tree = _recompute_projection_from_preview_v2(
        project_id=project_id,
        document_id=document_id,
        document=document,
        session=session,
        repository=repository,
        preview=preview,
    )
    if current_projection != preview.projection:
        raise RequirementEnrichmentConflictError(
            "publication binding preview is stale"
        )
    started = repository.begin_publication_binding_v2(
        preview=preview,
        current_projection=current_projection,
    )
    try:
        requirement_entry_ids = {
            item.requirement_tree_node_id: item.requirement_entry_id
            for item in started.row_bindings
        }
        tree = _build_and_persist_publication_tree_v2(
            project_id=project_id,
            document_id=document_id,
            accepted_working_tree=accepted_working_tree,
            projection=current_projection,
            requirement_set_id=started.requirement_set_id,
            requirement_entry_ids=requirement_entry_ids,
            materialized_entries=(
                build_requirement_enrichment_publication_builder_entries_v2(
                    current_projection,
                    requirement_entry_ids=requirement_entry_ids,
                )
            ),
        )

        from app.application.extraction.enrichment_workspace import (
            _require_requirement_enrichment_session,
        )

        latest_document, latest_session = _require_requirement_enrichment_session(
            project_id=project_id,
            document_id=document_id,
            session_id=session.session_id,
            expected_review_revision=preview.review_revision,
        )
        latest_projection, _latest_tree = _recompute_projection_from_preview_v2(
            project_id=project_id,
            document_id=document_id,
            document=latest_document,
            session=latest_session,
            repository=repository,
            preview=preview,
        )
        if latest_projection != current_projection:
            raise RequirementEnrichmentConflictError(
                "publication inputs changed while the tree was being persisted"
            )
        requirement_set = db.get_requirement_set(started.requirement_set_id)
        if requirement_set is None:
            raise RequirementEnrichmentConflictError(
                "publication draft disappeared before finalization"
            )
        binding = build_requirement_enrichment_publication_binding_v2(
            started=started,
            requirement_tree_object_key=str(
                requirement_set.get("requirement_tree_object_key") or ""
            ),
            requirement_tree_sha256=tree_sha256(tree),
            requirement_tree_schema_version=tree.schema_version,
        )
        binding = repository.finalize_publication_binding_v2(binding)
    except BaseException:
        _discard_failed_publishing_draft_v2(
            project_id=project_id,
            document_id=document_id,
            started=started,
            repository=repository,
        )
        raise
    return binding, tree, started.created


def tree_sha256(tree: RequirementTreeSnapshotV1) -> str:
    from app.domain.extraction.requirement_tree import (
        canonical_requirement_tree_sha256,
    )

    return canonical_requirement_tree_sha256(tree)


def _recompute_projection_from_preview_v2(
    *,
    project_id: str,
    document_id: str,
    document: dict[str, Any],
    session: LlmRequirementReviewSession,
    repository: RequirementEnrichmentRepositoryPort,
    preview: RequirementEnrichmentPublicationBindingPreviewV2,
) -> tuple[RequirementEnrichmentPublicationProjectionV2, RequirementTreeSnapshotV1]:
    projection = preview.projection
    return _recompute_current_projection_v2(
        project_id=project_id,
        document_id=document_id,
        document=document,
        session=session,
        repository=repository,
        accepted_snapshot_id=projection.entry_classification_snapshot_id,
        accepted_revision=projection.entry_classification_revision,
        accepted_sha256=projection.entry_classification_sha256,
        atomization_state=projection.atomization_state,
        atomization_scope_receipt_id=projection.atomization_scope_receipt_id,
        atomization_scope_receipt_sha256=projection.atomization_scope_receipt_sha256,
        atomization_scope_revision=projection.atomization_scope_revision,
        base_active_requirement_set_id=preview.base_active_requirement_set_id,
    )


def _recompute_current_projection_v2(
    *,
    project_id: str,
    document_id: str,
    document: dict[str, Any],
    session: LlmRequirementReviewSession,
    repository: RequirementEnrichmentRepositoryPort,
    accepted_snapshot_id: str,
    accepted_revision: int,
    accepted_sha256: str,
    atomization_state: RequirementEnrichmentAtomizationStateV2,
    atomization_scope_receipt_id: str,
    atomization_scope_receipt_sha256: str,
    atomization_scope_revision: int,
    base_active_requirement_set_id: str,
) -> tuple[RequirementEnrichmentPublicationProjectionV2, RequirementTreeSnapshotV1]:
    from app.application.extraction.atomization_scope import (
        load_current_entry_classification_context,
    )

    if str(document.get("active_requirement_set_id") or "") != (
        base_active_requirement_set_id
    ):
        raise RequirementEnrichmentConflictError(
            "active Requirement Set changed after publication preview"
        )
    working_tree, classification = load_current_entry_classification_context(
        project_id,
        document_id,
    )
    _require_exact_accepted_classification(
        classification,
        snapshot_id=accepted_snapshot_id,
        revision=accepted_revision,
        payload_sha256=accepted_sha256,
    )
    if atomization_state is RequirementEnrichmentAtomizationStateV2.NOT_RUN:
        return (
            build_requirement_enrichment_publication_projection_v2(
                accepted_working_tree=working_tree,
                accepted_entry_classification=classification,
                atomization_state=atomization_state,
            ),
            working_tree,
        )

    from app.application.extraction.enrichment_workspace import (
        _require_current_requirement_enrichment_coverage,
    )

    coverage = _require_current_requirement_enrichment_coverage(session)
    lineage = build_requirement_enrichment_lineage(session, coverage=coverage)
    receipt = repository.get_atomization_scope_receipt_revision(
        project_id=project_id,
        document_id=document_id,
        session_id=session.session_id,
        lineage_sha256=lineage.lineage_sha256,
        scope_revision=atomization_scope_revision,
    )
    latest_receipt = repository.get_latest_atomization_scope_receipt(
        project_id=project_id,
        document_id=document_id,
        session_id=session.session_id,
        lineage_sha256=lineage.lineage_sha256,
    )
    if (
        receipt is None
        or latest_receipt is None
        or receipt != latest_receipt
        or receipt.receipt_id != atomization_scope_receipt_id
        or receipt.payload_sha256 != atomization_scope_receipt_sha256
        or receipt.active_revision < 1
    ):
        raise RequirementEnrichmentConflictError(
            "APPLIED publication requires the exact current active scope receipt"
        )
    run = repository.get_atomization(
        project_id=project_id,
        document_id=document_id,
        session_id=session.session_id,
        atomization_run_id=receipt.atomization_run_id,
    )
    if run is None or run.revision != receipt.atomization_run_revision:
        raise RequirementEnrichmentConflictError(
            "APPLIED publication atomization run is missing or stale"
        )
    refinements = repository.list_refinements(
        project_id=project_id,
        document_id=document_id,
        session_id=session.session_id,
        atomization_run_id=run.atomization_run_id,
    )
    core_preview = build_requirement_publication_preview(
        session=session,
        run=run,
        coverage=coverage,
        refinements=refinements,
        base_active_requirement_set_id=base_active_requirement_set_id,
        now=session.updated_at_utc,
    )
    core_preview = repository.create_or_get_publication_preview(core_preview)
    return (
        build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=atomization_state,
            atomization_scope_receipt=receipt,
            atomization_run=run,
            publication_preview=core_preview,
        ),
        working_tree,
    )


def _require_exact_accepted_classification(
    classification: EntryClassificationSnapshotV1,
    *,
    snapshot_id: str,
    revision: int,
    payload_sha256: str,
) -> None:
    if (
        classification.status is not EntryClassificationStatus.ACCEPTED
        or classification.snapshot_id != snapshot_id
        or classification.revision != revision
        or classification.payload_sha256 != payload_sha256
    ):
        raise RequirementEnrichmentConflictError(
            "publication requires the exact current ACCEPTED entry classification"
        )


def _build_and_persist_publication_tree_v2(
    *,
    project_id: str,
    document_id: str,
    accepted_working_tree: RequirementTreeSnapshotV1,
    projection: RequirementEnrichmentPublicationProjectionV2,
    requirement_set_id: str,
    requirement_entry_ids: dict[str, str],
    materialized_entries: tuple[dict[str, Any], ...],
) -> RequirementTreeSnapshotV1:
    from app.application.extraction.document_jobs import (
        _llm_extraction_result_matches_runtime,
    )
    from app.application.extraction.requirement_tree_snapshots import (
        RequirementTreeSnapshotError,
        persist_requirement_tree_snapshot,
    )
    from app.application.extraction.review_support import _require_project_document
    from app.application.extraction.structure_workspace import (
        _rebuild_structure_revision_context,
        _serialize_structure_workbench_for_api,
        _structure_agent_job_payload,
    )

    document = _require_project_document(project_id, document_id)
    if str(document.get("source_sha256") or "") != projection.source_sha256:
        raise RequirementEnrichmentConflictError(
            "document source changed before publication tree build"
        )
    job, payload, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=True,
    )
    if not _llm_extraction_result_matches_runtime(payload):
        raise RequirementTreeSnapshotError(
            "the effective Structure must be regenerated for this runtime"
        )
    source_ir, base_tree, revisions = _rebuild_structure_revision_context(
        document=document,
        agent=agent,
    )
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
    )
    builder_entries = build_requirement_enrichment_publication_builder_entries_v2(
        projection,
        requirement_entry_ids=requirement_entry_ids,
    )
    builder_tree = build_requirement_tree_snapshot(
        workbench=workbench,
        payload=payload,
        mode=RequirementTreeMode.REQUIREMENT_SET,
        requirement_set_id=requirement_set_id,
        requirement_entries=builder_entries,
        forced_state=RequirementTreeState.READY,
    )
    tree = overlay_requirement_enrichment_publication_tree_v2(
        accepted_working_tree=accepted_working_tree,
        builder_tree=builder_tree,
        projection=projection,
        materialized_entries=materialized_entries,
    )
    persist_requirement_tree_snapshot(
        requirement_set_id=requirement_set_id,
        snapshot=tree,
    )
    return tree


def _discard_failed_publishing_draft_v2(
    *,
    project_id: str,
    document_id: str,
    started: RequirementEnrichmentPublicationStartV2,
    repository: RequirementEnrichmentRepositoryPort,
) -> None:
    from app.application.extraction.requirement_tree_snapshots import (
        delete_unowned_requirement_set_tree_object,
    )

    if started.created and repository.abort_publication_binding_v2(started):
        delete_unowned_requirement_set_tree_object(
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=started.requirement_set_id,
        )


__all__ = [
    "create_requirement_publication_binding_preview_v2",
    "publish_requirement_enrichment_draft_v2",
]
