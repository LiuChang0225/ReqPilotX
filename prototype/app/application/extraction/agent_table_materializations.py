from __future__ import annotations

from typing import Any

from app.application.errors import ApplicationError
from app.application.extraction.agent_table_materialization_contracts import (
    AgentTableMaterializationCommitRequestV1,
)
from app.application.extraction.agent_logical_table_scope import (
    agent_logical_table_scope_to_parent_materializer_input,
)
from app.application.extraction.table_evidence_projection import (
    build_table_evidence_bindings,
)
from app.application.extraction.table_projection_overrides import (
    apply_table_projection_overrides,
)
from app.application.transport import ResponseSink
from app.domain.extraction.table_records import TableRecordProjectionV1
from app.ports.agent_table_materialization_repository import (
    AgentTableMaterializationConflict,
)


def commit_document_agent_table_materialization(
    project_id: str,
    document_id: str,
    projection_id: str,
    payload: AgentTableMaterializationCommitRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    """Commit a validated Agent artifact without inferring any boundaries."""

    from app.application.extraction.document_jobs import (
        _llm_extraction_result_matches_runtime,
    )
    from app.application.extraction.review_support import (
        _require_project_document,
    )
    from app.application.extraction.revise_structure import (
        validate_structure_revision_chain,
    )
    from app.application.extraction.structure_workspace import (
        _rebuild_structure_revision_context,
        _structure_agent_job_payload,
    )
    from app.application.runtime import (
        _ASSET_RESPONSE_HEADERS,
        _agent_table_materializations,
    )

    response.headers.update(_ASSET_RESPONSE_HEADERS)
    document = _require_project_document(project_id, document_id)
    _job, result, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=True,
    )
    if not _llm_extraction_result_matches_runtime(result):
        raise _conflict(
            "agent_table_materialization_runtime_binding_mismatch",
            "当前表格来源运行已变化，请重新生成 Agent 表格边界。",
        )
    source_sha256 = str(document.get("source_sha256") or "")
    if source_sha256 != payload.expected_source_sha256:
        raise _conflict(
            "agent_table_materialization_source_binding_mismatch",
            "原文版本已变化，请重新生成 Agent 表格边界。",
        )

    source_ir, base_tree, revisions = _rebuild_structure_revision_context(
        document=document,
        agent=agent,
    )
    effective_structure = validate_structure_revision_chain(
        source_ir,
        base_tree,
        revisions,
    )
    if (
        effective_structure.effective_snapshot_id
        != payload.expected_effective_structure_snapshot_id
    ):
        raise _conflict(
            "agent_table_materialization_structure_binding_mismatch",
            "有效结构已变化，请重新生成 Agent 表格边界。",
        )

    effective_result = apply_table_projection_overrides(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        payload=result,
    )
    current = next(
        (
            TableRecordProjectionV1.model_validate(item)
            for item in effective_result.get("table_record_projections") or []
            if isinstance(item, dict)
            and str(item.get("projection_id") or "") == projection_id
        ),
        None,
    )
    if current is None:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "agent_table_materialization_base_projection_not_found",
                "message": "当前结果中没有找到该基础表格投影。",
            },
        )
    if (
        current.table_fact.source_sha256 != source_sha256
        or current.table_fact.structure_snapshot_id
        != effective_structure.effective_snapshot_id
    ):
        raise _conflict(
            "agent_table_materialization_fact_binding_mismatch",
            "当前表格事实不再绑定原文和有效结构。",
        )
    if (
        current.projection_id != payload.expected_base_projection_id
        or current.projection_revision
        != payload.expected_base_projection_revision
        or current.projection_sha256
        != payload.expected_base_projection_sha256
    ):
        raise _conflict(
            "agent_table_materialization_base_projection_conflict",
            "基础表格投影已变化，请同步后重新提交。",
        )

    materialization = payload.materialization
    if (
        materialization.materializer_logical_table_id
        != current.table_fact.logical_table_id
        or materialization.table_fact_binding_sha256
        != current.table_fact.fact_binding_sha256
        or materialization.projection.table_fact != current.table_fact
    ):
        raise _conflict(
            "agent_table_materialization_table_fact_conflict",
            "Agent 物化工件不再绑定当前不可变表格事实。",
        )
    try:
        materializer_input = agent_logical_table_scope_to_parent_materializer_input(
            source_ir,
            payload.logical_table_scope,
            expected_d1_table_map_artifact=(
                payload.logical_table_scope.d1_table_map_artifact
            ),
            base_projection=current,
        )
    except ValueError as exc:
        raise _conflict(
            "agent_table_materialization_logical_scope_conflict",
            "Agent 逻辑表 scope 或 D1 表格映射已不再绑定当前来源事实。",
            detail=str(exc),
        ) from exc
    if (
        materializer_input.expected_agent_logical_table_id
        != materialization.agent_logical_table_id
        or materializer_input.agent_logical_table_scope_sha256
        != materialization.agent_logical_table_scope_sha256
        or materializer_input.d1_table_map_artifact_id
        != materialization.d1_table_map_artifact_id
        or materializer_input.d1_table_map_artifact_sha256
        != materialization.d1_table_map_artifact_sha256
    ):
        raise _conflict(
            "agent_table_materialization_logical_scope_conflict",
            "Agent 逻辑表 scope 与父边界物化工件不一致。",
        )
    try:
        evidence_bindings = build_table_evidence_bindings(
            table_record_projections=(materialization.projection,),
            tables=effective_result.get("tables") or [],
        )
    except ValueError as exc:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "agent_table_materialization_evidence_unavailable",
                "message": "无法为 Agent 表格物化结果建立完整来源证据。",
                "detail": str(exc),
            },
        ) from exc
    try:
        repository = _agent_table_materializations()
    except RuntimeError as exc:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "agent_table_materialization_repository_unavailable",
                "message": "Agent 表格物化仅在正式 PostgreSQL 持久化运行时可用。",
            },
        ) from exc
    try:
        record, created = repository.commit(
            project_id=project_id,
            document_id=document_id,
            command_id=payload.command_id,
            base_projection=current,
            logical_table_scope=materializer_input.scope,
            materialization=materialization,
            evidence_bindings=evidence_bindings,
        )
    except AgentTableMaterializationConflict as exc:
        raise _conflict(
            "agent_table_materialization_commit_conflict",
            "表格投影或 Agent 物化状态已变化，请同步后重试。",
            detail=str(exc),
        ) from exc
    return {
        "schema_version": "agent-table-materialization-commit-response/v1",
        "created": created,
        "record": record.model_dump(mode="json"),
    }


def get_document_agent_table_materialization(
    project_id: str,
    document_id: str,
    record_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    from app.application.extraction.review_support import (
        _require_project_document,
    )
    from app.application.runtime import (
        _ASSET_RESPONSE_HEADERS,
        _agent_table_materializations,
    )

    response.headers.update(_ASSET_RESPONSE_HEADERS)
    _require_project_document(project_id, document_id)
    try:
        repository = _agent_table_materializations()
    except RuntimeError as exc:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "agent_table_materialization_repository_unavailable",
                "message": "Agent 表格物化仅在正式 PostgreSQL 持久化运行时可用。",
            },
        ) from exc
    record = repository.get(
        project_id=project_id,
        document_id=document_id,
        record_id=record_id,
    )
    if record is None:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "agent_table_materialization_not_found",
                "message": "没有找到该 Agent 表格物化记录。",
            },
        )
    return {
        "schema_version": "agent-table-materialization-read-response/v1",
        "record": record.model_dump(mode="json"),
    }


def _conflict(code: str, message: str, *, detail: str = "") -> ApplicationError:
    payload: dict[str, Any] = {"code": code, "message": message}
    if detail:
        payload["detail"] = detail
    return ApplicationError(status_code=409, detail=payload)


__all__ = [
    "commit_document_agent_table_materialization",
    "get_document_agent_table_materialization",
]
