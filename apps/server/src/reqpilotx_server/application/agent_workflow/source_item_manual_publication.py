"""Host-built semantic contexts for atomic user-opinion carry/staleness.

No Agent-provided manual decisions are accepted. Database publication invokes
its repository hook against the latest user record under the common source lock.
"""

from __future__ import annotations


class _PinnedContext(dict):
    """Semantic value plus host-only CAS metadata; metadata never enters its hash."""


def _semantic_head(container, project_id, document_id, source_sha):
    from reqpilotx_server.adapters.persistence.postgres_jobs import source_item_semantic_head_job_id

    with container.jobs.database.connection() as connection:
        return source_item_semantic_head_job_id(
            connection, project_id, document_id, source_sha
        )


def manual_publication_enabled(container):
    database = getattr(getattr(container, "jobs", None), "database", None)
    if database is None:
        return False
    from reqpilotx_server.adapters.persistence.source_item_manual_decisions import (
        manual_storage_available,
    )

    with database.connection() as connection:
        return manual_storage_available(connection)


def invalidate_manual_for_unbound_map_in_transaction(
    connection, *, project_id, document_id, source_sha256, publication_id
):
    """A legacy Map-only head has no verified successor SourceItem context.

    Retain the last known baseline identity for audit, mark its current opinions
    stale, and require the actual later SourceItem publication to bind new units.
    This empty context is deliberately not a claim that source extraction ran.
    """
    from reqpilotx_server.adapters.persistence.source_item_manual_decisions import (
        current_manual_decisions_in_transaction,
        rebind_manual_decisions_in_transaction,
    )
    from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
        canonical_json_sha256,
    )

    previous = current_manual_decisions_in_transaction(
        connection, project_id, document_id
    )
    if previous is None:
        return {"status": "NO_MANUAL_DECISIONS"}
    body = {
        "schema_version": "source-item-manual-context/v1",
        "source_sha256": source_sha256,
        "baseline_sha256": previous["baseline_sha256"],
        "units": {},
    }
    result = rebind_manual_decisions_in_transaction(
        connection,
        project_id=project_id,
        document_id=document_id,
        previous_context=None,
        current_context={**body, "context_sha256": canonical_json_sha256(body)},
        publication_id=publication_id,
    )
    return {**result, "reason": "SOURCE_BASELINE_REQUIRES_REBIND"}


def current_manual_publication_context(container, project_id, document_id):
    if not manual_publication_enabled(container):
        return None
    from reqpilotx_server.application.agent_workflow.source_item_manual_decision_api import (
        resolve_current_manual_context,
    )

    # Check the resolved semantic snapshot against durable publication ordering.
    # A same-baseline image/decomposition update must not escape the CAS fence.
    context = resolve_current_manual_context(project_id, document_id)
    head = _semantic_head(container, project_id, document_id, context["source_sha256"])
    verified = resolve_current_manual_context(project_id, document_id)
    if verified != context or head != _semantic_head(
        container, project_id, document_id, context["source_sha256"]
    ):
        raise ValueError(
            "SourceItem semantics changed while pinning manual response context"
        )
    pinned = _PinnedContext(context)
    pinned.semantic_head_job_id = head
    return pinned


def attach_manual_publication_contexts(
    result,
    *,
    container,
    project_id,
    document_id,
    previous_context,
    document_map,
    baseline,
    table_previews,
    decomposition=None,
    annotations=None,
):
    if not manual_publication_enabled(container):
        return result
    from reqpilotx_server.application.agent_workflow.source_item_manual_decisions import (
        build_manual_decision_context,
    )

    current = build_manual_decision_context(
        document_map,
        baseline,
        table_previews,
        decomposition=decomposition,
        annotations=annotations,
    )
    result["_manual_decision_contexts"] = {
        "previous": previous_context,
        "current": current,
    }
    result.setdefault(
        "document_map_artifact_sha256",
        document_map.artifact_sha256
        if hasattr(document_map, "artifact_sha256")
        else document_map["artifact_sha256"],
    )
    if previous_context is not None:
        result.setdefault(
            "expected_live_baseline_sha256", previous_context["baseline_sha256"]
        )
        if hasattr(previous_context, "semantic_head_job_id"):
            result["expected_source_item_semantic_head_job_id"] = (
                previous_context.semantic_head_job_id
            )
    repository = getattr(container, "agent_structure_loop_repository", None)
    if repository is not None:
        head = repository.get_working_revision_head(
            project_id=project_id,
            document_id=document_id,
            source_sha256=current["source_sha256"],
        )
        result.setdefault("expected_structure_head_verified", True)
        result.setdefault(
            "expected_structure_head_revision_sha256",
            head.revision_sha256 if head else None,
        )
    return result


def attach_rebound_baseline_manual_contexts(
    result,
    *,
    container,
    project_id,
    document_id,
    previous_bundle,
    document_map,
    baseline,
    annotations=None,
    decomposition=None,
):
    if not manual_publication_enabled(container):
        return result
    from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import _source_context
    from reqpilotx_server.application.agent_workflow.source_item_delivery_bridge import _reasoning
    from reqpilotx_server.application.agent_workflow.source_item_decomposition import (
        rebind_source_item_decomposition,
    )
    from reqpilotx_server.application.agent_workflow.source_item_visual import (
        rebind_source_item_annotations,
    )

    previous = previous_bundle["baseline"]
    before = current_manual_publication_context(container, project_id, document_id)
    source_context, current_bundle, source_path, tables = _source_context(
        project_id, document_id, tuple(r.source_record_id for r in previous.records)
    )
    if current_bundle["baseline"].artifact_sha256 != previous.artifact_sha256:
        raise ValueError(
            "Manual response predecessor changed before baseline publication"
        )
    from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import (
        materialize_native_structure_table_preview,
        _verified_projection_operations,
    )

    for scope in {
        record.logical_table_scope_id
        for record in baseline.records
        if record.logical_table_scope_id
    } - set(tables):
        tables[scope] = materialize_native_structure_table_preview(
            bundle=source_context["bundle"],
            source_sha256=baseline.source_sha256,
            scope_id=scope,
            projection_operations=_verified_projection_operations(
                source_context["handoff_attempts"]
            ),
            source_path=source_path,
        )
    previous_decomposition = _reasoning(project_id, document_id, previous)[
        "decomposition"
    ]
    if decomposition is None and previous_decomposition is not None:
        decomposition = rebind_source_item_decomposition(
            previous, baseline, previous_decomposition, table_previews=tables
        )["artifact"]
    if annotations is None and current_bundle.get("image_annotations") is not None:
        annotations = rebind_source_item_annotations(
            previous_bundle["document_map"],
            document_map,
            previous,
            baseline,
            current_bundle["image_annotations"],
            table_previews=tables,
            previous_table_previews=tables,
        )["artifact"]
    return attach_manual_publication_contexts(
        result,
        container=container,
        project_id=project_id,
        document_id=document_id,
        previous_context=before,
        document_map=document_map,
        baseline=baseline,
        table_previews=tables,
        decomposition=decomposition,
        annotations=annotations,
    )
