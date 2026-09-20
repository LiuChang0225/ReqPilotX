from __future__ import annotations

from typing import Any

from app.adapters.persistence.table_projection_revisions import (
    append_table_projection_revision,
    decode_projection_revision,
    get_max_persisted_projection_revision,
    get_table_projection_revision_by_id,
    get_table_projection_revision_by_projection_id,
    get_table_projection_revision_head,
)
from app.application.errors import ApplicationError
from app.application.extraction.contracts import (
    TableRecordProjectionRevisionRequest,
)
from app.application.extraction.table_evidence_projection import (
    build_table_evidence_bindings,
)
from app.application.extraction.table_projection_overrides import (
    apply_table_projection_overrides,
)
from app.application.transport import ResponseSink
from app.domain.extraction.table_evidence import TableEvidenceBindingV1
from app.domain.extraction.table_records import (
    TableRecordKind,
    TableRecordProjectionMode,
    TableRecordProjectionV1,
)


def get_document_table_record_projection_row_groups(
    project_id: str,
    document_id: str,
    projection_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    """Describe a side-effect-free, source-bound manual row-group draft."""

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
    from app.application.extraction.table_record_projection import (
        build_manual_row_group_projection,
        propose_manual_row_groups,
    )
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    response.headers.update(_ASSET_RESPONSE_HEADERS)
    document = _require_project_document(project_id, document_id)
    _job, result, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=True,
    )
    if not _llm_extraction_result_matches_runtime(result):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "table_projection_runtime_binding_mismatch",
                "message": "当前表格投影需要重新整理后才能修正。",
            },
        )
    source_sha256 = str(document.get("source_sha256") or "")
    automatic_projections = tuple(
        TableRecordProjectionV1.model_validate(item)
        for item in result.get("table_record_projections") or []
        if isinstance(item, dict)
    )
    structure_snapshot_ids = {
        projection.table_fact.structure_snapshot_id
        for projection in automatic_projections
    }
    if len(structure_snapshot_ids) != 1:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "table_projection_structure_binding_mismatch",
                "message": "当前表格投影没有绑定唯一的结构结果。",
            },
        )
    structure_snapshot_id = next(iter(structure_snapshot_ids))
    requested_projection, _revision_row = _resolve_projection_alias(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        structure_snapshot_id=structure_snapshot_id,
        projection_id=projection_id,
        automatic_projections=automatic_projections,
    )
    if requested_projection is None:
        raise _projection_not_found()
    effective_result = apply_table_projection_overrides(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        payload=result,
    )
    projections = tuple(
        TableRecordProjectionV1.model_validate(item)
        for item in effective_result.get("table_record_projections") or []
        if isinstance(item, dict)
    )
    current = next(
        (
            projection
            for projection in projections
            if projection.table_fact.logical_table_id
            == requested_projection.table_fact.logical_table_id
        ),
        None,
    )
    if current is None:
        raise _projection_not_found()

    automatic = next(
        (
            projection
            for projection in automatic_projections
            if projection.table_fact.logical_table_id
            == current.table_fact.logical_table_id
        ),
        None,
    )
    effective_identity = (
        agent.get("effective_structure_identity")
        if isinstance(agent.get("effective_structure_identity"), dict)
        else {}
    )
    agent_structure_snapshot_id = str(
        effective_identity.get("snapshot_id") or ""
    )
    if (
        automatic is not None
        and automatic.table_fact.source_sha256 == source_sha256
        and agent_structure_snapshot_id == structure_snapshot_id
        and current.table_fact.fact_binding_sha256
        == automatic.table_fact.fact_binding_sha256
    ):
        from app.application.extraction.table_row_group_review_index import (
            TABLE_ROW_GROUP_REVIEW_INDEX_KEY,
            validated_table_row_group_review_entry,
        )

        review_entry = validated_table_row_group_review_entry(
            result.get(TABLE_ROW_GROUP_REVIEW_INDEX_KEY),
            automatic_projection=automatic,
        )
        if review_entry is not None:
            suggested_groups = _review_index_groups(
                review_entry["suggested_row_groups"]
            )
            matrix_projection = (
                current.mode is TableRecordProjectionMode.RECORDS
                and any(
                    record.kind is not TableRecordKind.ROW
                    for record in current.records
                )
            )
            return _row_group_review_response(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                structure_snapshot_id=structure_snapshot_id,
                current=current,
                suggested_groups=(
                    () if matrix_projection else suggested_groups
                ),
                current_groups=(
                    () if matrix_projection else _current_row_groups(current)
                ),
                source_ir=None,
                eligible_rows=(
                    []
                    if matrix_projection
                    else [dict(item) for item in review_entry["eligible_rows"]]
                ),
                editable=(
                    False
                    if matrix_projection
                    else bool(review_entry["editable"])
                ),
                read_only_reason=(
                    "当前为矩阵交点投影，需要行列表头编辑器，不能按物理行改组。"
                    if matrix_projection
                    else review_entry["read_only_reason"]
                ),
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
        current.table_fact.structure_snapshot_id
        != effective_structure.effective_snapshot_id
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "table_projection_structure_binding_mismatch",
                "message": "结构树已变化，请重新读取行分组。",
            },
        )

    if current.mode is TableRecordProjectionMode.RECORDS and any(
        record.kind is not TableRecordKind.ROW for record in current.records
    ):
        return _row_group_review_response(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            structure_snapshot_id=effective_structure.effective_snapshot_id,
            current=current,
            suggested_groups=(),
            current_groups=(),
            source_ir=source_ir,
            eligible_rows=None,
            editable=False,
            read_only_reason=(
                "当前为矩阵交点投影，需要行列表头编辑器，不能按物理行改组。"
            ),
        )

    suggested_groups = propose_manual_row_groups(
        source_ir,
        effective_structure,
        table_structure_node_id=current.table_fact.logical_table_id,
    )
    if suggested_groups:
        try:
            build_manual_row_group_projection(
                source_ir,
                effective_structure,
                table_structure_node_id=current.table_fact.logical_table_id,
                row_groups=suggested_groups,
                projection_revision=current.projection_revision,
            )
        except ValueError as exc:
            raise ApplicationError(
                status_code=422,
                detail={
                    "code": "table_projection_row_group_review_unavailable",
                    "message": "当前表格无法形成安全、完整的人工行分组。",
                    "detail": str(exc),
                },
            ) from exc
    current_groups = _current_row_groups(current)
    return _row_group_review_response(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        structure_snapshot_id=effective_structure.effective_snapshot_id,
        current=current,
        suggested_groups=tuple(
            group.row_coordinates for group in suggested_groups
        ),
        current_groups=current_groups,
        source_ir=source_ir,
        eligible_rows=None,
        editable=bool(suggested_groups),
        read_only_reason=(
            "当前表格没有可分组的业务行。" if not suggested_groups else None
        ),
    )


def revise_document_table_record_projection(
    project_id: str,
    document_id: str,
    projection_id: str,
    payload: TableRecordProjectionRevisionRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    """Append a source-bound table split/merge correction."""

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
    from app.application.extraction.table_record_projection import (
        ManualRowGroup,
        build_manual_row_group_projection,
        propose_manual_row_groups,
    )
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    response.headers.update(_ASSET_RESPONSE_HEADERS)
    document = _require_project_document(project_id, document_id)
    _job, result, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=True,
    )
    if not _llm_extraction_result_matches_runtime(result):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "table_projection_runtime_binding_mismatch",
                "message": "当前表格投影需要重新整理后才能修正。",
            },
        )
    source_sha256 = str(document.get("source_sha256") or "")
    automatic_projections = tuple(
        TableRecordProjectionV1.model_validate(item)
        for item in result.get("table_record_projections") or []
        if isinstance(item, dict)
    )
    structure_snapshot_ids = {
        projection.table_fact.structure_snapshot_id
        for projection in automatic_projections
    }
    if len(structure_snapshot_ids) != 1:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "table_projection_structure_binding_mismatch",
                "message": "当前表格投影没有绑定唯一的结构结果。",
            },
        )
    structure_snapshot_id = next(iter(structure_snapshot_ids))
    requested_projection, requested_revision_row = _resolve_projection_alias(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        structure_snapshot_id=structure_snapshot_id,
        projection_id=projection_id,
        automatic_projections=automatic_projections,
    )
    if requested_projection is None:
        raise _projection_not_found()

    effective_result = apply_table_projection_overrides(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        payload=result,
    )
    projections = tuple(
        TableRecordProjectionV1.model_validate(item)
        for item in effective_result.get("table_record_projections") or []
        if isinstance(item, dict)
    )
    current = next(
        (
            projection
            for projection in projections
            if projection.table_fact.logical_table_id
            == requested_projection.table_fact.logical_table_id
        ),
        None,
    )
    if current is None:
        raise _projection_not_found()
    automatic_for_table = next(
        (
            projection
            for projection in automatic_projections
            if projection.table_fact.logical_table_id
            == requested_projection.table_fact.logical_table_id
        ),
        None,
    )
    if (
        automatic_for_table is None
        or requested_projection.table_fact != automatic_for_table.table_fact
    ):
        raise _projection_not_found()

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
        current.table_fact.structure_snapshot_id
        != effective_structure.effective_snapshot_id
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "table_projection_structure_binding_mismatch",
                "message": "结构树已变化，请先同步当前结果。",
            },
        )
    if (
        payload.expected_source_sha256 is not None
        and payload.expected_source_sha256 != source_sha256
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "table_projection_source_binding_mismatch",
                "message": "原文版本已变化，请重新读取行分组。",
            },
        )
    if (
        payload.expected_structure_snapshot_id is not None
        and payload.expected_structure_snapshot_id
        != effective_structure.effective_snapshot_id
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "table_projection_structure_binding_mismatch",
                "message": "结构树已变化，请重新读取行分组。",
            },
        )

    head = get_table_projection_revision_head(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        structure_snapshot_id=effective_structure.effective_snapshot_id,
        logical_table_id=current.table_fact.logical_table_id,
    )
    stored_revision = int((head or {}).get("revision") or 0)
    if (
        requested_projection.projection_revision
        != payload.expected_projection_revision
    ):
        raise _projection_revision_conflict(
            expected=payload.expected_projection_revision,
            current=current.projection_revision,
        )
    if current.projection_revision != payload.expected_projection_revision:
        replay = _replay_completed_command(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            structure_snapshot_id=effective_structure.effective_snapshot_id,
            payload=payload,
            requested_projection=requested_projection,
            requested_revision_row=requested_revision_row,
            head=head,
            source_ir=source_ir,
            effective_structure=effective_structure,
            tables=effective_result.get("tables") or [],
            manual_row_group_type=ManualRowGroup,
            build_projection=build_manual_row_group_projection,
            propose_row_groups=propose_manual_row_groups,
        )
        if replay is not None:
            return replay
        raise _projection_revision_conflict(
            expected=payload.expected_projection_revision,
            current=current.projection_revision,
        )

    max_persisted_projection_revision = (
        get_max_persisted_projection_revision(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            structure_snapshot_id=effective_structure.effective_snapshot_id,
            logical_table_id=current.table_fact.logical_table_id,
        )
    )
    next_projection_revision = (
        max(
            current.projection_revision,
            max_persisted_projection_revision,
        )
        + 1
    )
    action = payload.action
    revised = _build_requested_revision(
        source_ir=source_ir,
        effective_structure=effective_structure,
        current=current,
        action=action,
        record_id=str(payload.record_id or ""),
        explicit_row_groups=payload.row_groups,
        projection_revision=next_projection_revision,
        manual_row_group_type=ManualRowGroup,
        build_projection=build_manual_row_group_projection,
        propose_row_groups=propose_manual_row_groups,
    )

    evidence = (
        build_table_evidence_bindings(
            table_record_projections=(revised,),
            tables=effective_result.get("tables") or [],
        )
        if revised is not None
        else ()
    )
    try:
        stored, created = append_table_projection_revision(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            structure_snapshot_id=effective_structure.effective_snapshot_id,
            logical_table_id=current.table_fact.logical_table_id,
            expected_revision=stored_revision,
            action=action,
            projection=(
                revised.model_dump(mode="json")
                if revised is not None
                else None
            ),
            evidence_bindings=[
                binding.model_dump(mode="json") for binding in evidence
            ],
        )
    except ValueError as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "table_projection_revision_conflict",
                "message": "表格拆分状态已变化，请同步后再操作。",
                "detail": str(exc),
            },
        ) from exc

    return _revision_response(
        project_id=project_id,
        document_id=document_id,
        stored=stored,
        created=created,
        logical_table_id=current.table_fact.logical_table_id,
        projection=revised,
    )


def _resolve_projection_alias(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    projection_id: str,
    automatic_projections: tuple[TableRecordProjectionV1, ...],
) -> tuple[TableRecordProjectionV1 | None, dict[str, Any] | None]:
    automatic = next(
        (
            projection
            for projection in automatic_projections
            if projection.projection_id == projection_id
        ),
        None,
    )
    if automatic is not None:
        return automatic, None
    revision_row = get_table_projection_revision_by_projection_id(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        structure_snapshot_id=structure_snapshot_id,
        projection_id=projection_id,
    )
    if revision_row is None:
        return None, None
    projection_payload, _evidence = decode_projection_revision(revision_row)
    if projection_payload is None:
        return None, None
    return (
        TableRecordProjectionV1.model_validate(projection_payload),
        revision_row,
    )


def _replay_completed_command(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    payload: TableRecordProjectionRevisionRequest,
    requested_projection: TableRecordProjectionV1,
    requested_revision_row: dict[str, Any] | None,
    head: dict[str, Any] | None,
    source_ir: Any,
    effective_structure: Any,
    tables: list[dict[str, Any]],
    manual_row_group_type: Any,
    build_projection: Any,
    propose_row_groups: Any,
) -> dict[str, Any] | None:
    """Return the already committed result only for its direct HTTP replay."""

    if head is None or str(head.get("action") or "") != payload.action:
        return None
    if not _head_directly_supersedes_requested_projection(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        structure_snapshot_id=structure_snapshot_id,
        head=head,
        requested_revision_row=requested_revision_row,
    ):
        return None
    stored_payload, stored_evidence_payload = decode_projection_revision(head)
    if payload.action == "RESTORE_AUTO":
        if stored_payload is not None or stored_evidence_payload:
            return None
        return _revision_response(
            project_id=project_id,
            document_id=document_id,
            stored=head,
            created=False,
            logical_table_id=requested_projection.table_fact.logical_table_id,
            projection=None,
        )
    if stored_payload is None:
        return None
    try:
        stored_projection = TableRecordProjectionV1.model_validate(
            stored_payload
        )
        replayed_projection = _build_requested_revision(
            source_ir=source_ir,
            effective_structure=effective_structure,
            current=requested_projection,
            action=payload.action,
            record_id=str(payload.record_id or ""),
            explicit_row_groups=payload.row_groups,
            projection_revision=stored_projection.projection_revision,
            manual_row_group_type=manual_row_group_type,
            build_projection=build_projection,
            propose_row_groups=propose_row_groups,
        )
        if replayed_projection != stored_projection:
            return None
        replayed_evidence = build_table_evidence_bindings(
            table_record_projections=(replayed_projection,),
            tables=tables,
        )
        stored_evidence = tuple(
            TableEvidenceBindingV1.model_validate(item)
            for item in stored_evidence_payload
        )
    except (ApplicationError, TypeError, ValueError):
        return None
    if replayed_evidence != stored_evidence:
        return None
    return _revision_response(
        project_id=project_id,
        document_id=document_id,
        stored=head,
        created=False,
        logical_table_id=requested_projection.table_fact.logical_table_id,
        projection=stored_projection,
    )


def _head_directly_supersedes_requested_projection(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    head: dict[str, Any],
    requested_revision_row: dict[str, Any] | None,
) -> bool:
    supersedes_id = str(head.get("supersedes_id") or "")
    if requested_revision_row is not None:
        return supersedes_id == str(requested_revision_row.get("id") or "")
    if not supersedes_id:
        return True
    predecessor = get_table_projection_revision_by_id(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        structure_snapshot_id=structure_snapshot_id,
        revision_id=supersedes_id,
    )
    return (
        predecessor is not None
        and str(predecessor.get("action") or "") == "RESTORE_AUTO"
    )


def _build_requested_revision(
    *,
    source_ir: Any,
    effective_structure: Any,
    current: TableRecordProjectionV1,
    action: str,
    record_id: str,
    explicit_row_groups: Any,
    projection_revision: int,
    manual_row_group_type: Any,
    build_projection: Any,
    propose_row_groups: Any,
) -> TableRecordProjectionV1 | None:
    if action == "RESTORE_AUTO":
        return None
    if action == "KEEP_WHOLE":
        return build_projection(
            source_ir,
            effective_structure,
            table_structure_node_id=current.table_fact.logical_table_id,
            row_groups=(),
            projection_revision=projection_revision,
        )
    if action == "SPLIT_ROWS":
        groups = (
            tuple(
                manual_row_group_type(
                    row_coordinates=tuple(
                        (row.table_id, row.row_index)
                        for row in group.rows
                    )
                )
                for group in explicit_row_groups
            )
            if explicit_row_groups is not None
            else propose_row_groups(
                source_ir,
                effective_structure,
                table_structure_node_id=current.table_fact.logical_table_id,
            )
        )
        if explicit_row_groups is None and len(groups) < 2:
            raise ApplicationError(
                status_code=422,
                detail={
                    "code": "table_projection_has_no_separable_rows",
                    "message": (
                        "当前原表没有形成两个可验证的业务行。"
                        "原表保持不变，可继续作为完整条款核对。"
                    ),
                },
            )
        try:
            return build_projection(
                source_ir,
                effective_structure,
                table_structure_node_id=current.table_fact.logical_table_id,
                row_groups=groups,
                projection_revision=projection_revision,
            )
        except ValueError as exc:
            if explicit_row_groups is None:
                raise
            raise ApplicationError(
                status_code=422,
                detail={
                    "code": "invalid_table_projection_row_groups",
                    "message": (
                        "行分组必须按原表顺序连续排列，并完整覆盖所有业务行。"
                    ),
                    "detail": str(exc),
                },
            ) from exc
    return _merge_adjacent_records(
        source_ir=source_ir,
        effective_structure=effective_structure,
        current=current,
        record_id=record_id,
        merge_next=action == "MERGE_NEXT",
        projection_revision=projection_revision,
        manual_row_group_type=manual_row_group_type,
        build_projection=build_projection,
    )


def _projection_not_found() -> ApplicationError:
    return ApplicationError(
        status_code=404,
        detail={
            "code": "table_projection_not_found",
            "message": (
                "当前需求树中没有找到该表格投影，请同步后重试。"
            ),
        },
    )


def _projection_revision_conflict(
    *,
    expected: int,
    current: int,
) -> ApplicationError:
    return ApplicationError(
        status_code=409,
        detail={
            "code": "table_projection_revision_conflict",
            "message": "表格拆分状态已变化，请同步后再操作。",
            "expected_projection_revision": expected,
            "current_projection_revision": current,
        },
    )


def _revision_response(
    *,
    project_id: str,
    document_id: str,
    stored: dict[str, Any],
    created: bool,
    logical_table_id: str,
    projection: TableRecordProjectionV1 | None,
) -> dict[str, Any]:
    return {
        "schema_version": "table-record-projection-revision-response/v1",
        "project_id": project_id,
        "document_id": document_id,
        "created": created,
        "revision": {
            "id": str(stored.get("id") or ""),
            "revision": int(stored.get("revision") or 0),
            "action": str(stored.get("action") or ""),
            "logical_table_id": logical_table_id,
        },
        "projection": (
            projection.model_dump(mode="json")
            if projection is not None
            else None
        ),
    }


def _row_group_review_response(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    structure_snapshot_id: str,
    current: TableRecordProjectionV1,
    suggested_groups: tuple[tuple[tuple[str, int], ...], ...],
    current_groups: tuple[tuple[tuple[str, int], ...], ...],
    source_ir: Any | None,
    eligible_rows: list[dict[str, Any]] | None,
    editable: bool,
    read_only_reason: str | None,
) -> dict[str, Any]:
    eligible_coordinates = tuple(
        coordinate
        for group in suggested_groups
        for coordinate in group
    )

    def serialize_coordinate(coordinate: tuple[str, int]) -> dict[str, Any]:
        table_id, row_index = coordinate
        table_item = table_catalog.get(table_id)
        values: list[str] = []
        page_number: int | None = None
        if table_item is not None:
            table, source_node = table_item
            page_number = source_node.native_locator.page_number
            for cell in sorted(
                (
                    cell
                    for cell in table.cells
                    if cell.row <= row_index < cell.row + cell.row_span
                ),
                key=lambda cell: (cell.column, cell.row, cell.cell_id),
            ):
                value = (
                    cell.display_value
                    or cell.raw_text
                    or cell.formula
                    or ""
                ).strip()
                if value and value not in values:
                    values.append(value)
        display_text = " · ".join(values)
        if len(display_text) > 240:
            display_text = display_text[:237].rstrip() + "..."
        return {
            "table_id": table_id,
            "row_index": row_index,
            "page_number": page_number,
            "display_text": display_text,
        }

    def serialize_groups(
        groups: tuple[tuple[tuple[str, int], ...], ...],
    ) -> list[dict[str, Any]]:
        return [
            {
                "rows": [
                    {"table_id": table_id, "row_index": row_index}
                    for table_id, row_index in group
                ]
            }
            for group in groups
        ]

    if eligible_rows is None:
        if source_ir is None:
            raise ValueError(
                "row-group response requires SourceIR or durable eligible rows"
            )
        table_catalog = {
            node.table_payload.table_id: (node.table_payload, node)
            for node in source_ir.nodes
            if node.table_payload is not None
        }
        serialized_eligible_rows = [
            serialize_coordinate(coordinate)
            for coordinate in eligible_coordinates
        ]
    else:
        serialized_eligible_rows = eligible_rows

    return {
        "schema_version": "table-record-projection-row-group-review/v1",
        "project_id": project_id,
        "document_id": document_id,
        "projection_id": current.projection_id,
        "logical_table_id": current.table_fact.logical_table_id,
        "expected_projection_revision": current.projection_revision,
        "expected_source_sha256": source_sha256,
        "expected_structure_snapshot_id": structure_snapshot_id,
        "editable": editable,
        "read_only_reason": read_only_reason,
        "eligible_rows": serialized_eligible_rows,
        "current_row_groups": serialize_groups(current_groups),
        "suggested_row_groups": serialize_groups(suggested_groups),
    }


def _current_row_groups(
    current: TableRecordProjectionV1,
) -> tuple[tuple[tuple[str, int], ...], ...]:
    if current.mode is not TableRecordProjectionMode.RECORDS:
        return ()
    return tuple(
        tuple(
            (binding.table_id, binding.row_index)
            for binding in record.row_bindings
        )
        for record in current.records
    )


def _review_index_groups(
    value: list[dict[str, Any]],
) -> tuple[tuple[tuple[str, int], ...], ...]:
    return tuple(
        tuple(
            (str(row["table_id"]), int(row["row_index"]))
            for row in group["rows"]
        )
        for group in value
    )


def _merge_adjacent_records(
    *,
    source_ir: Any,
    effective_structure: Any,
    current: TableRecordProjectionV1,
    record_id: str,
    merge_next: bool,
    projection_revision: int,
    manual_row_group_type: Any,
    build_projection: Any,
) -> TableRecordProjectionV1:
    if current.mode is not TableRecordProjectionMode.RECORDS:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "table_projection_not_split",
                "message": "当前表格尚未拆分为业务行。",
            },
        )
    records = list(current.records)
    index = next(
        (
            candidate_index
            for candidate_index, record in enumerate(records)
            if record.record_id == record_id
        ),
        -1,
    )
    if index < 0:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "table_record_not_found",
                "message": (
                    "没有找到要合并的表格记录，请同步后重试。"
                ),
            },
        )
    neighbor_index = index + 1 if merge_next else index - 1
    if neighbor_index < 0 or neighbor_index >= len(records):
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "table_record_has_no_adjacent_record",
                "message": "当前记录在该方向没有可合并的相邻记录。",
            },
        )
    if (
        records[index].kind is not TableRecordKind.ROW
        or records[neighbor_index].kind is not TableRecordKind.ROW
    ):
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "matrix_record_merge_requires_axis_editor",
                "message": (
                    "矩阵交点需要在行列表头编辑器中调整，不能直接合并。"
                ),
            },
        )
    left = min(index, neighbor_index)
    right = max(index, neighbor_index)
    groups: list[Any] = []
    for record_index, record in enumerate(records):
        coordinates = tuple(
            (binding.table_id, binding.row_index)
            for binding in record.row_bindings
        )
        if record_index == left:
            coordinates = (
                *coordinates,
                *tuple(
                    (binding.table_id, binding.row_index)
                    for binding in records[right].row_bindings
                ),
            )
        if record_index == right:
            continue
        groups.append(manual_row_group_type(row_coordinates=coordinates))
    return build_projection(
        source_ir,
        effective_structure,
        table_structure_node_id=current.table_fact.logical_table_id,
        row_groups=tuple(groups),
        projection_revision=projection_revision,
    )


__all__ = [
    "get_document_table_record_projection_row_groups",
    "revise_document_table_record_projection",
]
