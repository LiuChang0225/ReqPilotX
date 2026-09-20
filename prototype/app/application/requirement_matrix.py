from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app import db
from app.adapters.persistence.requirement_matrix_states import (
    list_requirement_matrix_node_states,
    save_requirement_matrix_node_decision,
    set_requirement_matrix_node_disposition,
    set_requirement_matrix_node_dispositions,
)
from app.application.errors import ApplicationError
from app.application.extraction.requirement_tree_snapshots import (
    RequirementTreeSnapshotError,
    load_requirement_set_tree_snapshot,
)
from app.application.requirement_matrix_contracts import (
    MatrixDecisionRequestV1,
    MatrixDispositionBatchPatchV1,
    MatrixDispositionPatchV1,
)
from app.domain.extraction.requirement_tree import (
    RequirementTreeNodeV1,
    RequirementTreeSnapshotV1,
    RequirementTreeState,
)
from app.domain.requirement_matrix import (
    MatrixAiAssessmentV1,
    MatrixAnalysisEligibility,
    MatrixAnalysisResultState,
    MatrixAnalysisStatus,
    MatrixCountsV1,
    MatrixLogicalTableV1,
    MatrixManualDecisionV1,
    MatrixNodeDisposition,
    MatrixNodeKind,
    MatrixNodeStateV1,
    MatrixNodeVisibility,
    MatrixSnapshotState,
    RequirementMatrixSnapshotV1,
)

_ANALYZABLE_ENTRY_KINDS = {"REQUIREMENT", "HYBRID"}


def build_requirement_matrix_snapshot(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    *,
    require_current_runtime: bool = False,
) -> RequirementMatrixSnapshotV1:
    """Project one immutable Requirement Set into every downstream consumer.

    Historical snapshots remain available to read.  Callers that are about to
    mutate Matrix state or create a new downstream artifact must opt into the
    current-runtime fence so an immutable legacy Requirement Set cannot be
    silently reinterpreted by newer extraction code.
    """

    _, requirement_set = _require_matrix_scope(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=requirement_set_id,
    )
    try:
        tree = load_requirement_set_tree_snapshot(requirement_set)
    except RequirementTreeSnapshotError:
        return _requires_reextraction_matrix(requirement_set)
    if (
        tree.state is not RequirementTreeState.READY
        or tree.requirement_evidence_bundle is None
    ):
        return _requires_reextraction_matrix(requirement_set)
    if require_current_runtime and not _requirement_set_has_current_runtime_lineage(
        requirement_set
    ):
        return _requires_reextraction_matrix(requirement_set)

    requirements = db.list_requirements(
        project_id,
        document_id,
        requirement_set_id=requirement_set_id,
    )
    requirements_by_id = {
        str(row.get("id") or ""): row
        for row in requirements
        if str(row.get("id") or "")
    }
    bound_requirement_ids = {
        str(node.requirement_entry_id or "")
        for node in tree.nodes
        if str(node.requirement_entry_id or "")
    }
    if bound_requirement_ids != set(requirements_by_id):
        return _requires_reextraction_matrix(requirement_set)

    persisted_states = list_requirement_matrix_node_states(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=requirement_set_id,
    )
    tree_node_ids = {node.id for node in tree.nodes}
    if any(str(row.get("node_id") or "") not in tree_node_ids for row in persisted_states):
        return _requires_reextraction_matrix(requirement_set)
    state_by_node_id = {
        str(row.get("node_id") or ""): row for row in persisted_states
    }

    table_context = _table_context(tree)
    presentation_nodes = _direct_presentation_tree_nodes(tree.nodes)
    nodes = tuple(
        _project_matrix_node(
            tree_node=node,
            requirement=requirements_by_id.get(
                str(node.requirement_entry_id or "")
            ),
            persisted_state=state_by_node_id.get(node.id),
            logical_table_ids=_logical_table_ids_for_node(
                node,
                table_context=table_context,
            ),
            resolved_table_record_id=_resolved_table_record_id(
                node,
                table_context=table_context,
            ),
            table_context=table_context,
        )
        for node in presentation_nodes
    )
    nodes = _restore_historical_table_containers(
        tree=tree,
        nodes=nodes,
    )
    logical_tables = _project_logical_tables(
        tree=tree,
        nodes=nodes,
        table_context=table_context,
    )
    counts = _matrix_counts(nodes=nodes, logical_tables=logical_tables)
    return RequirementMatrixSnapshotV1(
        state=MatrixSnapshotState.READY,
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=requirement_set_id,
        requirement_set_status=str(requirement_set.get("status") or "").upper(),
        tree_sha256=str(requirement_set.get("requirement_tree_sha256") or ""),
        root_node_ids=tree.root_node_ids,
        nodes=nodes,
        logical_tables=logical_tables,
        counts=counts,
    )


def _requirement_set_has_current_runtime_lineage(
    requirement_set: dict[str, Any],
) -> bool:
    """Prove that a frozen set came from a current-runtime formal result.

    Requirement Set rows intentionally remain immutable and therefore do not
    get rewritten when extraction runtime versions advance.  The proof is
    reconstructed from the current or latest successful immutable extraction
    result and must match the set's exact source and Effective Structure
    identity.  Missing history, partial jobs, stale runtimes, and ambiguous
    identities all fail closed.
    """

    from app.application.extraction.document_jobs import (
        _get_latest_successful_llm_extraction_job,
        _get_llm_extraction_job,
        _llm_extraction_result_matches_runtime,
        _llm_job_visual_strategy,
        _restored_llm_extraction_job,
    )

    project_id = str(requirement_set.get("project_id") or "").strip()
    document_id = str(requirement_set.get("sor_document_id") or "").strip()
    source_sha256 = str(requirement_set.get("source_hash") or "").strip().lower()
    effective_snapshot_id = str(
        requirement_set.get("structure_snapshot_id") or ""
    ).strip()
    effective_tree_sha256 = str(
        requirement_set.get("structure_tree_sha256") or ""
    ).strip().lower()
    if "structure_revision" not in requirement_set:
        return False
    try:
        structure_revision = int(requirement_set.get("structure_revision") or 0)
    except (TypeError, ValueError):
        return False
    if (
        not project_id
        or not document_id
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or not effective_snapshot_id
        or re.fullmatch(r"[0-9a-f]{64}", effective_tree_sha256) is None
        or structure_revision < 0
    ):
        return False

    try:
        current = _get_llm_extraction_job(
            project_id,
            document_id,
            source_sha256=source_sha256,
        )
        latest_successful = _get_latest_successful_llm_extraction_job(
            project_id,
            document_id,
            source_sha256=source_sha256,
        )
        restored = (
            _restored_llm_extraction_job(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
            )
            if current is None and latest_successful is None
            else None
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return False

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for candidate in (current, latest_successful, restored):
        if (
            not isinstance(candidate, dict)
            or str(candidate.get("status") or "").strip().upper() != "COMPLETED"
            or not isinstance(candidate.get("result"), dict)
        ):
            continue
        identity = (
            str(candidate.get("job_id") or ""),
            id(candidate.get("result")),
        )
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(candidate)

    for candidate in candidates:
        result = candidate["result"]
        try:
            runtime_matches = _llm_extraction_result_matches_runtime(
                result,
                expected_visual_strategy=_llm_job_visual_strategy(candidate),
            )
        except (TypeError, ValueError):
            runtime_matches = False
        if not runtime_matches:
            continue
        agent = result.get("structure_agent")
        base_identity = (
            agent.get("base_structure_identity")
            if isinstance(agent, dict)
            and isinstance(agent.get("base_structure_identity"), dict)
            else {}
        )
        effective_identity = (
            agent.get("effective_structure_identity")
            if isinstance(agent, dict)
            and isinstance(agent.get("effective_structure_identity"), dict)
            else {}
        )
        if "revision_number" not in effective_identity:
            continue
        try:
            result_revision = int(
                effective_identity.get("revision_number") or 0
            )
        except (TypeError, ValueError):
            continue
        if (
            str(result.get("project_id") or "") == project_id
            and str(result.get("document_id") or "") == document_id
            and str(result.get("source_sha256") or "").lower() == source_sha256
            and str(base_identity.get("source_sha256") or "").lower()
            == source_sha256
            and str(effective_identity.get("snapshot_id") or "")
            == effective_snapshot_id
            and str(effective_identity.get("tree_sha256") or "").lower()
            == effective_tree_sha256
            and result_revision == structure_revision
        ):
            return True
    return False


def _direct_presentation_tree_nodes(
    nodes: tuple[RequirementTreeNodeV1, ...],
) -> tuple[RequirementTreeNodeV1, ...]:
    """Remove descendant roll-up evidence from each human-facing node.

    Frozen Requirement Trees intentionally preserve complete lineage and older
    snapshots often repeat every descendant block, table and image on each
    ancestor.  That is useful evidence, but it is not a readable Matrix row.
    This derived projection keeps only evidence owned directly by the node;
    the immutable snapshot and its hash remain untouched.
    """

    node_by_id = {node.id: node for node in nodes}
    descendants: dict[str, dict[str, set[str]]] = {}
    projected: dict[str, RequirementTreeNodeV1] = {}
    evidence_fields = ("content_blocks", "table_refs", "figure_refs", "source_refs")

    for node in sorted(nodes, key=lambda item: (item.depth, item.source_order), reverse=True):
        child_evidence = {field: set() for field in evidence_fields}
        for child_id in node.child_ids:
            child = node_by_id.get(child_id)
            if child is None:
                continue
            child_rollup = descendants.get(child_id, {})
            for field in evidence_fields:
                child_evidence[field].update(child_rollup.get(field, set()))

        own_signatures: dict[str, set[str]] = {}
        updates: dict[str, Any] = {}
        for field in evidence_fields:
            values = tuple(getattr(node, field))
            signatures = {_matrix_evidence_signature(value) for value in values}
            own_signatures[field] = signatures
            updates[field] = tuple(
                value
                for value in values
                if _matrix_evidence_signature(value) not in child_evidence[field]
            )

        descendants[node.id] = {
            field: own_signatures[field] | child_evidence[field]
            for field in evidence_fields
        }
        direct_text = any(
            str(block.get("text") or block.get("raw_text") or "").strip()
            for block in updates["content_blocks"]
            if isinstance(block, dict)
            and str(block.get("kind") or block.get("type") or "").lower()
            in {"text", "paragraph"}
        )
        if node.child_ids and not direct_text:
            updates["summary"] = node.label
        projected[node.id] = node.model_copy(update=updates)

    return tuple(projected[node.id] for node in nodes)


def _matrix_evidence_signature(value: dict[str, Any]) -> str:
    for key in (
        "source_id",
        "table_id",
        "cell_id",
        "asset_id",
        "binding_id",
        "content_sha256",
        "id",
        "node_id",
    ):
        normalized = str(value.get(key) or "").strip()
        if normalized:
            return f"{key}:{normalized}"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _restore_historical_table_containers(
    *,
    tree: RequirementTreeSnapshotV1,
    nodes: tuple[MatrixNodeStateV1, ...],
) -> tuple[MatrixNodeStateV1, ...]:
    """Restore provable RECORDS table parents in legacy frozen snapshots.

    Older snapshots placed table records directly under a chapter.  The
    immutable records and evidence remain authoritative; this function only
    restores the missing read-model container when record identity, evidence,
    common parent, contiguity and order all agree exactly.
    """

    node_by_id = {node.node_id: node for node in nodes}
    occupied_ids = set(node_by_id)
    plans: list[dict[str, Any]] = []
    planned_record_ids: set[str] = set()
    for projection in tree.table_record_projections:
        if str(getattr(projection.mode, "value", projection.mode)).upper() != "RECORDS":
            continue
        logical_id = str(projection.table_fact.logical_table_id or "")
        if not logical_id or any(
            node.display_role is MatrixNodeKind.TABLE
            and logical_id in node.logical_table_ids
            for node in nodes
        ):
            continue
        projection_record_ids = tuple(
            str(record.record_id or "") for record in projection.records
        )
        if not projection_record_ids or any(not value for value in projection_record_ids):
            continue
        record_nodes: list[MatrixNodeStateV1] = []
        valid = True
        for record_id in projection_record_ids:
            matches = [
                node
                for node in nodes
                if node.display_role is MatrixNodeKind.TABLE_RECORD
                and node.table_record_id == record_id
                and logical_id in node.logical_table_ids
            ]
            if len(matches) != 1:
                valid = False
                break
            record_nodes.append(matches[0])
        if not valid or any(node.node_id in planned_record_ids for node in record_nodes):
            continue
        parent_ids = {node.parent_id for node in record_nodes}
        if len(parent_ids) != 1 or None in parent_ids:
            continue
        parent_id = next(iter(parent_ids))
        parent = node_by_id.get(str(parent_id))
        if parent is None:
            continue
        try:
            indices = [parent.child_ids.index(node.node_id) for node in record_nodes]
        except ValueError:
            continue
        if indices != list(range(indices[0], indices[0] + len(indices))):
            continue
        bindings = [
            binding
            for binding in tree.table_evidence_bindings
            if binding.logical_table_id == logical_id
        ]
        bound_record_ids = {
            str(binding.record_id)
            for binding in bindings
            if binding.record_id is not None
        }
        if bound_record_ids != set(projection_record_ids) or not any(
            binding.record_id is None for binding in bindings
        ):
            continue
        canonical = json.dumps(
            {
                "source_ir_id": projection.table_fact.source_ir_id,
                "logical_table_id": logical_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        container_id = f"whole-table-{hashlib.sha256(canonical).hexdigest()[:32]}"
        if container_id in occupied_ids:
            continue
        occupied_ids.add(container_id)
        plans.append(
            {
                "logical_id": logical_id,
                "container_id": container_id,
                "parent": parent,
                "records": tuple(record_nodes),
                "first_index": indices[0],
                "projection": projection,
            }
        )
        planned_record_ids.update(node.node_id for node in record_nodes)

    if not plans:
        return nodes

    updated = dict(node_by_id)
    insert_before: dict[str, MatrixNodeStateV1] = {}
    for plan in plans:
        parent = updated[plan["parent"].node_id]
        record_nodes = plan["records"]
        record_node_ids = tuple(node.node_id for node in record_nodes)
        first_record_id = record_node_ids[0]
        container_id = plan["container_id"]
        logical_id = plan["logical_id"]
        replacement: list[str] = []
        for child_id in parent.child_ids:
            if child_id == first_record_id:
                replacement.append(container_id)
            if child_id not in record_node_ids:
                replacement.append(child_id)
        updated[parent.node_id] = parent.model_copy(
            update={"child_ids": tuple(replacement)}
        )

        descendant_ids: set[str] = set()
        pending = list(record_node_ids)
        while pending:
            current_id = pending.pop()
            if current_id in descendant_ids:
                continue
            descendant_ids.add(current_id)
            current = updated[current_id]
            pending.extend(current.child_ids)
        for node_id in descendant_ids:
            current = updated[node_id]
            changes: dict[str, Any] = {"depth": current.depth + 1}
            if node_id in record_node_ids:
                changes["parent_id"] = container_id
            updated[node_id] = current.model_copy(update=changes)

        projection = plan["projection"]
        whole_source_refs = tuple(
            ref.model_dump(mode="json")
            for ref in projection.table_fact.whole_table_source_refs
        )
        page_values = [
            page
            for node in record_nodes
            for page in (node.page_start, node.page_end)
            if page is not None
        ]
        record_count = len(record_nodes)
        container = MatrixNodeStateV1(
            node_id=container_id,
            parent_id=parent.node_id,
            child_ids=record_node_ids,
            depth=parent.depth + 1,
            source_order=min(node.source_order for node in record_nodes),
            display_role=MatrixNodeKind.TABLE,
            source_kind="TABLE",
            display_title="表格",
            display_text=f"{record_count} 条表格条目",
            label="表格",
            summary=f"{record_count} 条表格条目",
            source_refs=whole_source_refs,
            page_start=min(page_values) if page_values else parent.page_start,
            page_end=max(page_values) if page_values else parent.page_end,
            structure_node_id=str(
                projection.table_fact.owner_structure_node_id or ""
            )
            or None,
            logical_table_ids=(logical_id,),
            matrix_visibility=MatrixNodeVisibility.VISIBLE,
            disposition=MatrixNodeDisposition.RETAIN,
            analysis_eligibility=MatrixAnalysisEligibility.NOT_ELIGIBLE,
            analysis_status=MatrixAnalysisStatus.NOT_APPLICABLE,
        )
        updated[container_id] = container
        insert_before[first_record_id] = container

    output: list[MatrixNodeStateV1] = []
    for original in nodes:
        container = insert_before.get(original.node_id)
        if container is not None:
            output.append(container)
        output.append(updated[original.node_id])
    return tuple(output)


def get_requirement_matrix(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> dict[str, Any]:
    matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requirement_set_id,
    )
    payload = matrix.model_dump(mode="json")
    # The treegrid endpoint is deliberately a lightweight read model.  Full
    # clause/table evidence is version-bound and loaded only for the selected
    # node/table through the detail endpoints below.
    payload["nodes"] = [
        _matrix_node_summary_payload(node) for node in matrix.nodes
    ]
    payload["logical_tables"] = [
        _matrix_table_summary_payload(table) for table in matrix.logical_tables
    ]
    return payload


def select_matrix_analysis_requirements(
    matrix: RequirementMatrixSnapshotV1,
    requirements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the one frozen Matrix target consumed by the AI worker.

    ``entry_kind`` remains provenance about how an entry was published.  It is
    deliberately not used as a second eligibility system: the locked Matrix is
    the sole authority for visibility, human disposition, and intrinsic
    analysis eligibility.
    """

    _require_ready_matrix(matrix)
    requirement_by_id = {
        str(row.get("id") or ""): row
        for row in requirements
        if str(row.get("id") or "")
    }
    selected: list[dict[str, Any]] = []
    seen_requirement_ids: set[str] = set()
    node_by_id = {node.node_id: node for node in matrix.nodes}
    table_by_id = {
        table.logical_table_id: table for table in matrix.logical_tables
    }
    for node in _matrix_nodes_in_source_order(matrix):
        if (
            node.matrix_visibility is not MatrixNodeVisibility.VISIBLE
            or node.disposition is not MatrixNodeDisposition.ANALYZE
            or node.analysis_eligibility is not MatrixAnalysisEligibility.ELIGIBLE
        ):
            continue
        requirement_id = str(node.requirement_id or "")
        requirement = requirement_by_id.get(requirement_id)
        if not requirement_id or requirement is None:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "matrix_analysis_target_binding_invalid",
                    "message": "需求矩阵中的分析项无法绑定到冻结需求版本。",
                },
            )
        if requirement_id in seen_requirement_ids:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "matrix_analysis_target_not_unique",
                    "message": "同一需求在矩阵分析范围中出现了多次。",
                },
            )
        seen_requirement_ids.add(requirement_id)
        frozen_context = _matrix_analysis_context(
            node,
            node_by_id=node_by_id,
            table_by_id=table_by_id,
        )
        # Text consumed by AI must come from the immutable Requirement Tree,
        # never from a later-mutated convenience row in the database.
        frozen_text = str(
            node.requirement_text or node.summary or node.label or ""
        ).strip()
        selected.append(
            {
                **requirement,
                "raw_text": frozen_text,
                "display_text": frozen_text,
                "requirement_text": frozen_text,
                **frozen_context,
                "matrix_node_id": node.node_id,
                "matrix_display_role": node.display_role.value,
                "matrix_disposition": node.disposition.value,
                "matrix_analysis_eligibility": node.analysis_eligibility.value,
                "matrix_source_refs": list(node.source_refs),
                "matrix_table_record_id": node.table_record_id,
                "matrix_logical_table_ids": list(node.logical_table_ids),
            }
        )
    return selected


def _matrix_analysis_context(
    node: MatrixNodeStateV1,
    *,
    node_by_id: dict[str, MatrixNodeStateV1],
    table_by_id: dict[str, MatrixLogicalTableV1],
) -> dict[str, Any]:
    ancestors: list[MatrixNodeStateV1] = []
    visited: set[str] = set()
    parent_id = node.parent_id
    while parent_id and parent_id not in visited:
        visited.add(parent_id)
        parent = node_by_id.get(parent_id)
        if parent is None:
            break
        ancestors.append(parent)
        parent_id = parent.parent_id
    ancestors.reverse()
    direct_parent = ancestors[-1] if ancestors else None
    parent_text = _matrix_node_context_text(direct_parent) if direct_parent else ""
    parent_label = str(direct_parent.label or "") if direct_parent else ""
    sibling_items: list[dict[str, Any]] = []
    if direct_parent is not None:
        siblings = [
            node_by_id[child_id]
            for child_id in direct_parent.child_ids
            if child_id in node_by_id
        ]
        siblings.sort(key=lambda item: (item.source_order, item.node_id))
        selected_index = next(
            (index for index, item in enumerate(siblings) if item.node_id == node.node_id),
            -1,
        )
        if selected_index >= 0:
            for sibling in siblings[
                max(0, selected_index - 1) : selected_index + 2
            ]:
                if sibling.node_id != node.node_id:
                    sibling_items.append(_matrix_context_item(sibling))

    frozen_node_table_refs = [dict(item) for item in node.table_refs]
    # A record-bound atom must never inherit a whole-table or sibling slice.
    table_refs = [] if node.table_record_id else list(frozen_node_table_refs)
    for logical_table_id in node.logical_table_ids:
        table = table_by_id.get(logical_table_id)
        if table is None:
            continue
        projection = dict(table.projection)
        matching_record = next(
            (
                dict(item)
                for item in projection.get("records") or []
                if isinstance(item, dict)
                and str(item.get("record_id") or item.get("id") or "")
                == str(node.table_record_id or "")
            ),
            {},
        )
        if node.table_record_id and not matching_record:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "matrix_table_record_binding_invalid",
                    "message": "需求矩阵中的表格记录证据无法解析，请重新提取。",
                },
            )
        record_slice: dict[str, Any] = {}
        slice_rows: list[list[str]] = []
        ordered_headers: list[dict[str, Any]] = []
        if node.table_record_id:
            record_slice = _record_local_table_slice(
                matching_record,
                table_refs=frozen_node_table_refs,
                logical_table_id=logical_table_id,
                physical_table_ids=table.physical_table_ids,
            )
            if not record_slice.get("cells"):
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": "matrix_table_record_content_unresolved",
                        "message": (
                            "表格记录缺少可验证的原始单元格内容，请重新提取。"
                        ),
                    },
                )
            ordered_headers = _ordered_record_header_bindings(
                matching_record,
                record_slice["cells"],
            )
            slice_rows = _record_ai_rows(
                matching_record,
                record_slice["cells"],
            )
        table_refs.append(
            {
                "logical_table_id": logical_table_id,
                "table_id": table.physical_table_ids[0],
                "title": str(projection.get("title") or node.label or ""),
                "slice_ai_projection": (
                    {
                        **matching_record,
                        "header_bindings": ordered_headers,
                        "cells": record_slice.get("cells", []),
                        "source_tables": record_slice.get("source_tables", []),
                    }
                    if node.table_record_id
                    else projection
                ),
                "slice_rows": (
                    slice_rows
                    if node.table_record_id
                    else matching_record.get("row_bindings") or []
                ),
                "headers": (
                    ordered_headers
                    if node.table_record_id
                    else matching_record.get("header_bindings") or []
                ),
                "merged_cells": (
                    matching_record.get("shared_merged_values") or []
                ),
                "notes": matching_record.get("notes") or projection.get("notes") or [],
            }
        )
    return {
        "parent_section_label": parent_label,
        "parent_section_title": parent_label,
        "full_parent_clause": parent_text,
        "ancestor_path": [_matrix_context_item(item) for item in ancestors],
        "ancestor_path_status": "FULL" if ancestors else "MISSING",
        "direct_parent": (
            _matrix_context_item(direct_parent) if direct_parent else None
        ),
        "direct_children": [
            _matrix_context_item(node_by_id[child_id])
            for child_id in node.child_ids
            if child_id in node_by_id
        ],
        "adjacent_context": sibling_items,
        "content_blocks": [dict(item) for item in node.content_blocks],
        "table_refs": table_refs,
        "source_refs": [dict(item) for item in node.source_refs],
    }


def _record_local_table_slice(
    record: dict[str, Any],
    *,
    table_refs: list[dict[str, Any]],
    logical_table_id: str,
    physical_table_ids: tuple[str, ...],
) -> dict[str, Any]:
    wanted_cell_ids: set[str] = set()
    for key in (
        "row_bindings",
        "column_bindings",
        "header_bindings",
    ):
        for binding in record.get(key) or []:
            if isinstance(binding, dict):
                wanted_cell_ids.update(
                    str(value)
                    for value in binding.get("cell_ids") or []
                    if str(value)
                )
    for shared in record.get("shared_merged_values") or []:
        if isinstance(shared, dict) and str(shared.get("cell_id") or ""):
            wanted_cell_ids.add(str(shared["cell_id"]))

    source_tables: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    seen_cell_ids: set[str] = set()
    physical_ids = set(physical_table_ids)
    for table_ref in table_refs:
        table_id = str(
            table_ref.get("table_id")
            or table_ref.get("id")
            or table_ref.get("source_id")
            or ""
        )
        ref_logical_id = str(table_ref.get("logical_table_id") or "")
        if (
            table_id not in physical_ids
            and ref_logical_id != logical_table_id
        ):
            continue
        selected_cells = []
        for cell in table_ref.get("cells") or []:
            if not isinstance(cell, dict):
                continue
            cell_id = str(cell.get("id") or cell.get("cell_id") or "")
            if cell_id not in wanted_cell_ids or cell_id in seen_cell_ids:
                continue
            selected = dict(cell)
            selected_cells.append(selected)
            cells.append(selected)
            seen_cell_ids.add(cell_id)
        if selected_cells:
            source_tables.append(
                {
                    "table_id": table_id,
                    "logical_table_id": ref_logical_id or logical_table_id,
                    "title": str(
                        table_ref.get("title")
                        or table_ref.get("label")
                        or ""
                    ),
                    "page_number": table_ref.get("page_number")
                    or table_ref.get("page"),
                    "cells": selected_cells,
                }
            )
    return {"cells": cells, "source_tables": source_tables}


def _record_ai_rows(
    record: dict[str, Any],
    cells: list[dict[str, Any]],
) -> list[list[str]]:
    row_cell_ids = {
        str(cell_id)
        for binding in record.get("row_bindings") or []
        if isinstance(binding, dict)
        for cell_id in binding.get("cell_ids") or []
        if str(cell_id)
    }
    headers = [
        str(binding.get("text") or "").strip()
        for binding in _ordered_record_header_bindings(record, cells)
        if isinstance(binding, dict) and str(binding.get("text") or "").strip()
    ]
    values = [
        str(cell.get("text") or cell.get("value") or "").strip()
        for cell in cells
        if (
            not row_cell_ids
            or str(cell.get("id") or cell.get("cell_id") or "")
            in row_cell_ids
        )
        if str(cell.get("text") or cell.get("value") or "").strip()
    ]
    if not values:
        return []
    return [[" / ".join(headers) or "记录", " | ".join(values)]]


def _ordered_record_header_bindings(
    record: dict[str, Any],
    cells: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Restore header path order from immutable native-cell geometry.

    Frozen TableRecord headers are identity-stable but older snapshots sorted
    same-level bindings by cell ID.  Matrix consumers must present and send the
    visual table order without mutating that frozen evidence.
    """

    positions = {
        str(cell.get("id") or cell.get("cell_id") or ""): index
        for index, cell in enumerate(cells)
        if isinstance(cell, dict)
        and str(cell.get("id") or cell.get("cell_id") or "")
    }
    indexed = [
        (index, dict(binding))
        for index, binding in enumerate(record.get("header_bindings") or [])
        if isinstance(binding, dict)
    ]

    def binding_order(item: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        original_index, binding = item
        cell_positions = [
            positions[str(cell_id)]
            for cell_id in binding.get("cell_ids") or []
            if str(cell_id) in positions
        ]
        return (
            min(cell_positions) if cell_positions else len(positions) + original_index,
            original_index,
        )

    return [binding for _, binding in sorted(indexed, key=binding_order)]


def _matrix_node_context_text(node: MatrixNodeStateV1 | None) -> str:
    if node is None:
        return ""
    paragraphs = [
        str(block.get("text") or block.get("raw_text") or "").strip()
        for block in node.content_blocks
        if isinstance(block, dict)
        and str(block.get("text") or block.get("raw_text") or "").strip()
    ]
    return "\n\n".join(paragraphs) or str(
        node.requirement_text or node.summary or node.label or ""
    ).strip()


def _matrix_context_item(node: MatrixNodeStateV1) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "label": node.label,
        "summary": node.summary,
        "display_role": node.display_role.value,
        "page_start": node.page_start,
        "page_end": node.page_end,
    }


def _matrix_nodes_in_source_order(
    matrix: RequirementMatrixSnapshotV1,
) -> tuple[MatrixNodeStateV1, ...]:
    node_by_id = {node.node_id: node for node in matrix.nodes}
    ordered: list[MatrixNodeStateV1] = []
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        node = node_by_id[node_id]
        ordered.append(node)
        # ``child_ids`` is part of the frozen Requirement Tree contract and is
        # therefore the authoritative sibling order.  Several table records
        # intentionally share one document-level source anchor; sorting those
        # IDs again would fall back to their hashes and corrupt row order.
        for child_id in node.child_ids:
            visit(child_id)

    for root_id in matrix.root_node_ids:
        visit(root_id)
    return tuple(ordered)


def get_requirement_matrix_node_detail(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    node_id: str,
) -> dict[str, Any]:
    matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requirement_set_id,
    )
    _require_ready_matrix(matrix)
    matrix_node = next((node for node in matrix.nodes if node.node_id == node_id), None)
    if matrix_node is None:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "matrix_node_not_found",
                "message": "该节点不属于当前需求版本。",
            },
        )
    requirement_set = db.get_requirement_set(requirement_set_id) or {}
    tree = load_requirement_set_tree_snapshot(requirement_set)
    tree_node = next((node for node in tree.nodes if node.id == node_id), None)
    requirement = (
        db.get_requirement(matrix_node.requirement_id)
        if matrix_node.requirement_id
        else None
    )
    if requirement is not None:
        _assert_requirement_binding(
            requirement,
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requirement_set_id,
        )
    assessment = (
        db.get_assessment(matrix_node.requirement_id)
        if matrix_node.requirement_id
        else None
    )
    node_by_id = {node.node_id: node for node in matrix.nodes}
    path = _matrix_node_path(matrix_node, node_by_id=node_by_id)
    parent_clause = None
    if matrix_node.display_role not in {
        MatrixNodeKind.STRUCTURE,
        MatrixNodeKind.CLAUSE,
    }:
        parent_clause = next(
            (
                item
                for item in reversed(path[:-1])
                if item.matrix_visibility is MatrixNodeVisibility.VISIBLE
                and item.display_role
                in {MatrixNodeKind.CLAUSE, MatrixNodeKind.STRUCTURE}
                and (
                    item.content_blocks
                    or item.display_text
                    or item.requirement_text
                    or item.summary
                )
            ),
            None,
        )
    node_payload = _matrix_node_detail_payload(matrix_node)
    if matrix_node.display_role is MatrixNodeKind.TABLE_RECORD:
        # The native logical table is loaded once through the table endpoint.
        # Historical snapshots duplicated the complete table into every row;
        # never send that quadratic payload to the selected-record surface.
        node_payload["content_blocks"] = [
            dict(block)
            for block in matrix_node.content_blocks
            if str(block.get("kind") or "").lower() == "table_record"
        ]
        node_payload["table_refs"] = []
    parent_clause_payload: dict[str, Any] | None = None
    if parent_clause is not None:
        parent_clause_payload = (
            _matrix_node_summary_payload(parent_clause)
            if matrix_node.display_role is MatrixNodeKind.TABLE_RECORD
            else _matrix_node_detail_payload(parent_clause)
        )
    return {
        "schema_version": "requirement-matrix-node-detail/v1",
        "project_id": project_id,
        "document_id": document_id,
        "requirement_set_id": requirement_set_id,
        "node": node_payload,
        "path": [
            _matrix_node_summary_payload(item) for item in path
        ],
        "parent_clause": parent_clause_payload,
        "tree_node": (
            {
                "id": tree_node.id,
                "parent_id": tree_node.parent_id,
                "kind": tree_node.kind,
                "depth": tree_node.depth,
                "source_order": tree_node.source_order,
                "projection_role": tree_node.projection_role,
                "derived_read_model": False,
            }
            if tree_node is not None
            else {
                "id": matrix_node.node_id,
                "parent_id": matrix_node.parent_id,
                "kind": matrix_node.source_kind or matrix_node.display_role.value,
                "depth": matrix_node.depth,
                "source_order": matrix_node.source_order,
                "projection_role": "DERIVED_TABLE_CONTAINER",
                "derived_read_model": True,
            }
        ),
        "requirement": (
            {
                "id": requirement.get("id"),
                "req_code": requirement.get("req_code"),
                "entry_kind": requirement.get("entry_kind"),
            }
            if requirement is not None
            else None
        ),
        "assessment": assessment,
        "logical_tables": [
            _matrix_table_summary_payload(table)
            for table in matrix.logical_tables
            if table.logical_table_id in matrix_node.logical_table_ids
        ],
    }


def get_requirement_matrix_logical_table(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    logical_table_id: str,
) -> dict[str, Any]:
    matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requirement_set_id,
    )
    _require_ready_matrix(matrix)
    logical_table = next(
        (
            table
            for table in matrix.logical_tables
            if table.logical_table_id == logical_table_id
        ),
        None,
    )
    if logical_table is None:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "matrix_logical_table_not_found",
                "message": "该逻辑表不属于当前需求版本。",
            },
        )
    node_ids = set(logical_table.node_ids)
    logical_table_payload = logical_table.model_dump(mode="json")
    logical_table_payload["structured_segments"] = _logical_table_segments(
        logical_table,
        nodes=matrix.nodes,
    )
    return {
        "schema_version": "requirement-matrix-logical-table-detail/v1",
        "project_id": project_id,
        "document_id": document_id,
        "requirement_set_id": requirement_set_id,
        "logical_table": logical_table_payload,
        "nodes": [
            _matrix_node_summary_payload(node)
            for node in matrix.nodes
            if node.node_id in node_ids
        ],
    }


def _logical_table_segments(
    logical_table: MatrixLogicalTableV1,
    *,
    nodes: tuple[MatrixNodeStateV1, ...],
) -> list[dict[str, Any]]:
    physical_ids = set(logical_table.physical_table_ids)
    by_identity: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if logical_table.logical_table_id not in node.logical_table_ids:
            continue
        for block in (*node.content_blocks, *node.table_refs):
            kind = str(block.get("kind") or block.get("type") or "").lower()
            if kind != "table" and not isinstance(block.get("cells"), list):
                continue
            table_id = str(
                block.get("table_id") or block.get("id") or block.get("source_id") or ""
            )
            block_logical_id = str(block.get("logical_table_id") or "")
            if (
                table_id not in physical_ids
                and block_logical_id != logical_table.logical_table_id
            ):
                continue
            identity = table_id or str(block.get("content_sha256") or "")
            if not identity:
                continue
            current = by_identity.get(identity)
            if current is None or len(block.get("cells") or []) > len(
                current.get("cells") or []
            ):
                by_identity[identity] = dict(block)
    return sorted(
        by_identity.values(),
        key=lambda block: (
            int(block.get("page_start") or block.get("page_number") or 0),
            int(block.get("source_order") or 0),
            str(block.get("table_id") or block.get("id") or ""),
        ),
    )


def _matrix_node_summary_payload(node: MatrixNodeStateV1) -> dict[str, Any]:
    payload = node.model_dump(mode="json")
    for field in ("content_blocks", "table_refs", "figure_refs", "source_refs"):
        payload[field] = []
    # The treegrid only needs a readable row preview.  Full, unmodified text is
    # version-bound and remains available from the node detail endpoint.  This
    # prevents document-root summaries and long table clauses from turning the
    # virtualized list into a multi-megabyte response.
    payload["label"] = _matrix_list_preview(payload.get("label"), limit=320)
    payload["summary"] = _matrix_list_preview(payload.get("summary"), limit=600)
    payload["requirement_text"] = _matrix_list_preview(
        payload.get("requirement_text"),
        limit=600,
    )
    return payload


def _matrix_node_detail_payload(node: MatrixNodeStateV1) -> dict[str, Any]:
    payload = node.model_dump(mode="json")
    if node.child_ids:
        # The immutable tree may retain a whole-descendant summary for lineage.
        # A Matrix detail is a direct-content view, so never ship that roll-up
        # as a plausible user paragraph or a legacy requirement fallback.
        direct_text = str(node.display_text or "").strip()
        payload["summary"] = direct_text
        payload["requirement_text"] = direct_text
    return payload


def _matrix_list_preview(value: Any, *, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}…"


def _matrix_table_summary_payload(table: MatrixLogicalTableV1) -> dict[str, Any]:
    payload = table.model_dump(mode="json")
    payload.pop("projection", None)
    payload.pop("evidence_bindings", None)
    return payload


def _validate_matrix_disposition(
    node: MatrixNodeStateV1,
    disposition: MatrixNodeDisposition,
) -> None:
    if node.matrix_visibility is MatrixNodeVisibility.SYSTEM_NON_BUSINESS:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "matrix_system_node_immutable",
                "message": "系统非业务节点不能人工改变处置。",
            },
        )
    if node.child_ids:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "matrix_container_disposition_requires_leaf_selection",
                "message": "该项是内容分组，处理方式由下级条目汇总，请选择具体条目。",
            },
        )
    if (
        disposition is MatrixNodeDisposition.ANALYZE
        and node.analysis_eligibility is not MatrixAnalysisEligibility.ELIGIBLE
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "matrix_node_not_analysis_candidate",
                "message": (
                    "该节点不是可直接判定的候选项；请先在需求提取中完成"
                    "必要的条目拆分或结构修订。"
                ),
            },
        )


def patch_requirement_matrix_node_disposition(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    node_id: str,
    payload: MatrixDispositionPatchV1,
) -> dict[str, Any]:
    matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requirement_set_id,
        require_current_runtime=True,
    )
    _require_ready_matrix(matrix)
    node = next((item for item in matrix.nodes if item.node_id == node_id), None)
    if node is None:
        raise ApplicationError(
            status_code=404,
            detail={"code": "matrix_node_not_found", "message": "节点不存在。"},
        )
    _validate_matrix_disposition(node, payload.disposition)
    try:
        persisted = set_requirement_matrix_node_disposition(
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requirement_set_id,
            node_id=node_id,
            disposition=payload.disposition.value,
            reason=payload.reason,
            expected_revision=payload.expected_revision,
        )
    except RuntimeError as exc:
        if str(exc) != "matrix_state_revision_conflict":
            raise
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "matrix_state_revision_conflict",
                "message": "节点处置已变化，请刷新后重试。",
            },
        ) from exc
    except PermissionError as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": str(exc),
                "message": "只有当前未锁定的需求版本可以修改处置。",
            },
        ) from exc
    except LookupError as exc:
        raise ApplicationError(
            status_code=409,
            detail={"code": str(exc), "message": "需求版本绑定已变化。"},
        ) from exc
    from app.application.requirement_set_impact_service import (
        reconcile_requirement_set_impact_for_draft,
    )

    impact_receipt = reconcile_requirement_set_impact_for_draft(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=requirement_set_id,
    )
    refreshed = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requirement_set_id,
        require_current_runtime=True,
    )
    refreshed_node = next(item for item in refreshed.nodes if item.node_id == node_id)
    return {
        "schema_version": "requirement-matrix-disposition-response/v1",
        "project_id": project_id,
        "document_id": document_id,
        "requirement_set_id": requirement_set_id,
        "save_revision": int(persisted.get("revision") or 0),
        "node": refreshed_node.model_dump(mode="json"),
        "counts": refreshed.counts.model_dump(mode="json"),
        "impact_receipt": impact_receipt,
    }


def patch_requirement_matrix_dispositions(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    payload: MatrixDispositionBatchPatchV1,
) -> dict[str, Any]:
    matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requirement_set_id,
        require_current_runtime=True,
    )
    _require_ready_matrix(matrix)
    if payload.expected_tree_sha256 != matrix.tree_sha256:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "matrix_tree_revision_conflict",
                "message": "需求树版本已变化，请刷新后重新确认批量范围。",
            },
        )
    node_by_id = {node.node_id: node for node in matrix.nodes}
    updates: list[dict[str, Any]] = []
    for item in payload.items:
        node = node_by_id.get(item.node_id)
        if node is None:
            raise ApplicationError(
                status_code=404,
                detail={
                    "code": "matrix_node_not_found",
                    "message": "批量范围包含不属于当前版本的节点。",
                },
            )
        _validate_matrix_disposition(node, item.disposition)
        updates.append(
            {
                "node_id": item.node_id,
                "disposition": item.disposition.value,
                "reason": item.reason,
                "expected_revision": item.expected_revision,
            }
        )
    try:
        persisted = set_requirement_matrix_node_dispositions(
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requirement_set_id,
            updates=updates,
        )
    except RuntimeError as exc:
        if str(exc) != "matrix_state_revision_conflict":
            raise
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "matrix_state_revision_conflict",
                "message": "批量范围中已有节点发生变化，请刷新后重试。",
            },
        ) from exc
    except PermissionError as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": str(exc),
                "message": "只有当前未锁定的需求版本可以批量修改处置。",
            },
        ) from exc
    except LookupError as exc:
        raise ApplicationError(
            status_code=409,
            detail={"code": str(exc), "message": "需求版本绑定已变化。"},
        ) from exc

    from app.application.requirement_set_impact_service import (
        reconcile_requirement_set_impact_for_draft,
    )

    impact_receipt = reconcile_requirement_set_impact_for_draft(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=requirement_set_id,
    )

    refreshed = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requirement_set_id,
        require_current_runtime=True,
    )
    changed_ids = {item.node_id for item in payload.items}
    return {
        "schema_version": "requirement-matrix-disposition-batch-response/v1",
        "project_id": project_id,
        "document_id": document_id,
        "requirement_set_id": requirement_set_id,
        "tree_sha256": refreshed.tree_sha256,
        "changed_count": len(persisted),
        "nodes": [
            node.model_dump(mode="json")
            for node in refreshed.nodes
            if node.node_id in changed_ids
        ],
        "counts": refreshed.counts.model_dump(mode="json"),
        "impact_receipt": impact_receipt,
    }


def post_requirement_matrix_node_decision(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    node_id: str,
    payload: MatrixDecisionRequestV1,
) -> dict[str, Any]:
    matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requirement_set_id,
        require_current_runtime=True,
    )
    _require_ready_matrix(matrix)
    node = next((item for item in matrix.nodes if item.node_id == node_id), None)
    if node is None:
        raise ApplicationError(
            status_code=404,
            detail={"code": "matrix_node_not_found", "message": "节点不存在。"},
        )
    if (
        node.analysis_eligibility is not MatrixAnalysisEligibility.ELIGIBLE
        or node.matrix_visibility is not MatrixNodeVisibility.VISIBLE
        or node.disposition is not MatrixNodeDisposition.ANALYZE
        or not node.requirement_id
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "matrix_node_not_analysis_eligible",
                "message": "只有当前已纳入分析的可判定条目可以记录人工结论。",
            },
        )
    document, requirement_set = _require_matrix_scope(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=requirement_set_id,
    )
    if (
        str(requirement_set.get("status") or "").upper() != "LOCKED"
        or str(document.get("active_requirement_set_id") or "")
        != requirement_set_id
        or not bool(int(requirement_set.get("is_active") or 0))
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "matrix_decision_requires_current_locked_set",
                "message": "人工结论只能写入当前已锁定的需求版本。",
            },
        )
    try:
        from app.application.requirement_set_impact_service import (
            REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION,
            requirement_analysis_input_sha256,
        )

        analysis_rows = select_matrix_analysis_requirements(
            matrix,
            db.list_requirements(
                project_id,
                document_id,
                requirement_set_id=requirement_set_id,
            ),
        )
        analysis_row = next(
            (
                row
                for row in analysis_rows
                if str(row.get("id") or "") == node.requirement_id
            ),
            None,
        )
        if analysis_row is None:
            raise ValueError("matrix_decision_analysis_input_not_found")
        persisted = save_requirement_matrix_node_decision(
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requirement_set_id,
            node_id=node_id,
            requirement_id=node.requirement_id,
            disposition=node.disposition.value,
            final_decision=payload.final_decision,
            engineer_reason=payload.engineer_reason.strip(),
            expected_revision=payload.expected_revision,
            analysis_input_schema_version=(
                REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
            ),
            analysis_input_sha256=requirement_analysis_input_sha256(
                analysis_row
            ),
        )
    except RuntimeError as exc:
        if str(exc) != "matrix_state_revision_conflict":
            raise
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "matrix_state_revision_conflict",
                "message": "节点状态已变化，请刷新后重试。",
            },
        ) from exc
    except (LookupError, PermissionError, ValueError) as exc:
        raise ApplicationError(
            status_code=409,
            detail={"code": "matrix_decision_rejected", "message": str(exc)},
        ) from exc
    refreshed = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requirement_set_id,
        require_current_runtime=True,
    )
    refreshed_node = next(item for item in refreshed.nodes if item.node_id == node_id)
    return {
        "schema_version": "requirement-matrix-decision-response/v1",
        "project_id": project_id,
        "document_id": document_id,
        "requirement_set_id": requirement_set_id,
        "save_revision": int(persisted.get("revision") or 0),
        "node": refreshed_node.model_dump(mode="json"),
        "assessment": db.get_assessment(node.requirement_id),
    }


def _require_matrix_scope(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.application.extraction.review_support import _require_project_document

    document = _require_project_document(project_id, document_id)
    requirement_set = db.get_requirement_set(requirement_set_id)
    if (
        requirement_set is None
        or str(requirement_set.get("project_id") or "") != project_id
        or str(requirement_set.get("sor_document_id") or "") != document_id
    ):
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "matrix_requirement_set_not_found",
                "message": "该需求版本不属于当前项目和文档。",
            },
        )
    return document, requirement_set


def _requires_reextraction_matrix(
    requirement_set: dict[str, Any],
) -> RequirementMatrixSnapshotV1:
    return RequirementMatrixSnapshotV1(
        state=MatrixSnapshotState.REQUIRES_REEXTRACTION,
        project_id=str(requirement_set.get("project_id") or ""),
        document_id=str(requirement_set.get("sor_document_id") or ""),
        requirement_set_id=str(requirement_set.get("id") or ""),
        requirement_set_status=str(requirement_set.get("status") or "").upper(),
        tree_sha256=str(requirement_set.get("requirement_tree_sha256") or ""),
    )


def _require_ready_matrix(matrix: RequirementMatrixSnapshotV1) -> None:
    if matrix.state is MatrixSnapshotState.READY:
        return
    raise ApplicationError(
        status_code=409,
        detail={
            "code": "matrix_requires_reextraction",
            "state": MatrixSnapshotState.REQUIRES_REEXTRACTION.value,
            "message": "该需求版本没有可信的冻结结构树，请重新提取生成新版本。",
        },
    )


_SOURCE_NUMBER_ONLY = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+)*"
    r"|[A-Za-z]"
    r"|[一二三四五六七八九十百千]+"
    r")(?:[、.)）])?$"
)
_SOURCE_NUMBER_PREFIX = re.compile(
    r"^(?P<number>"
    r"(?:\d+(?:\.\d+)+)"
    r"|(?:\d+)(?=\s)"
    r"|(?:\d+|[A-Za-z]|[一二三四五六七八九十百千]+)[、.)）]"
    r"|[（(](?:\d+|[A-Za-z]|[一二三四五六七八九十百千]+)[)）]"
    r")\s*(?P<title>.+)$"
)
_TABLE_SEQUENCE_HEADER = re.compile(
    r"^(?:序号|编号|序列|项次|no\.?|number)$",
    re.IGNORECASE,
)


def _compact_human_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _split_source_heading(value: object) -> tuple[str, str]:
    text = _compact_human_text(value)
    if not text:
        return "", ""
    if _SOURCE_NUMBER_ONLY.fullmatch(text):
        return text, ""
    matched = _SOURCE_NUMBER_PREFIX.match(text)
    if matched is None:
        return "", text
    return matched.group("number").strip(), matched.group("title").strip()


def _matrix_human_presentation(
    tree_node: RequirementTreeNodeV1,
    *,
    table_context: dict[str, Any],
    resolved_table_record_id: str | None,
) -> dict[str, str]:
    """Project frozen evidence into a human-readable Matrix row.

    Machine-oriented summaries and ``REQxxxx`` identities remain available for
    search, lineage and AI input, but they are not the customer-facing display
    contract.  In particular, table rows must never expose the pipe-joined
    serialization used by the extraction pipeline.
    """

    kind = str(tree_node.kind or "").upper()
    label = _compact_human_text(tree_node.label)
    summary_lines = [
        _compact_human_text(line)
        for line in str(tree_node.summary or "").splitlines()
        if _compact_human_text(line)
    ]

    if kind == "TABLE_RECORD":
        label_parts = [part.strip() for part in label.split(" · ", 1)]
        first = label_parts[0] if label_parts else "表格记录"
        second = label_parts[1] if len(label_parts) > 1 else ""
        record = table_context.get("records_by_id", {}).get(
            resolved_table_record_id or ""
        )
        headers = [
            _compact_human_text(binding.text)
            for binding in getattr(record, "header_bindings", ())
        ]
        has_sequence_header = any(
            _TABLE_SEQUENCE_HEADER.fullmatch(header) for header in headers
        )
        source_number = (
            first
            if has_sequence_header and _SOURCE_NUMBER_ONLY.fullmatch(first)
            else ""
        )
        display_title = second if source_number and second else first
        # Historical table records keep one pipe-joined string solely as a
        # compact machine projection.  Convert it into human lines here; the
        # UI and Excel must never expose that serialization verbatim.
        values = [
            _compact_human_text(value)
            for value in str(tree_node.summary or "").split(" | ")
            if _compact_human_text(value)
        ]
        if source_number and values and values[0] == source_number:
            values.pop(0)
        if values and values[0] == display_title:
            values.pop(0)
        # Equal display values can still come from different source columns.
        # Keep their positional multiplicity here; source-identity de-duplication
        # belongs in the direct-evidence projection, not in presentation text.
        display_text = "\n".join(values)
        if not display_text and not source_number:
            display_text = second
        return {
            "source_number": source_number,
            "display_title": display_title or "表格记录",
            "display_text": display_text,
        }

    source_number, title_from_label = _split_source_heading(label)
    first_summary = summary_lines[0] if summary_lines else ""
    summary_number, summary_title = _split_source_heading(first_summary)
    if not source_number and summary_number:
        source_number = summary_number
    display_title = title_from_label
    if not display_title or display_title == source_number:
        candidates = summary_lines[1:] if summary_number else summary_lines
        display_title = next(
            (
                candidate
                for candidate in candidates
                if candidate not in {source_number, label}
                and " | " not in candidate
            ),
            "",
        )
    if not display_title:
        display_title = summary_title or label or "未命名内容"

    paragraphs: list[str] = []
    for block in tree_node.content_blocks:
        block_kind = str(block.get("kind") or block.get("type") or "").lower()
        if block_kind not in {"text", "paragraph"}:
            continue
        text = str(block.get("text") or block.get("raw_text") or "").strip()
        if not text:
            continue
        lines = [
            _compact_human_text(line)
            for line in text.splitlines()
            if _compact_human_text(line)
        ]
        while lines and lines[0] in {source_number, label, display_title}:
            lines.pop(0)
        if lines and source_number and lines[0].startswith(f"{source_number} "):
            lines[0] = lines[0][len(source_number) :].strip()
        if lines and lines[0] == display_title:
            lines.pop(0)
        paragraph = "\n".join(lines).strip()
        if paragraph and paragraph not in paragraphs:
            paragraphs.append(paragraph)

    if paragraphs:
        display_text = "\n\n".join(paragraphs)
    elif tree_node.child_ids:
        # Container summaries in historical snapshots intentionally roll up
        # descendant evidence. They are useful lineage, but not direct text.
        # When no direct paragraph survives, the child rows carry the content.
        display_text = ""
    elif kind not in {"TABLE", "TABLE_RECORD"}:
        remaining = [
            line
            for line in summary_lines
            if line not in {source_number, label, display_title}
            and " | " not in line
        ]
        display_text = "\n".join(remaining)
    else:
        display_text = ""
    return {
        "source_number": source_number,
        "display_title": display_title,
        "display_text": display_text,
    }


def _project_matrix_node(
    *,
    tree_node: RequirementTreeNodeV1,
    requirement: dict[str, Any] | None,
    persisted_state: dict[str, Any] | None,
    logical_table_ids: tuple[str, ...],
    resolved_table_record_id: str | None = None,
    table_context: dict[str, Any] | None = None,
) -> MatrixNodeStateV1:
    requirement_id = str((requirement or {}).get("id") or "") or None
    entry_kind = str((requirement or {}).get("entry_kind") or "").upper()
    analysis_candidate = _is_analysis_candidate(
        tree_node=tree_node,
        requirement=requirement,
    )
    disposition = _default_disposition(
        entry_kind=entry_kind,
        projection_role=str(tree_node.projection_role or "").upper(),
        analysis_candidate=analysis_candidate,
        persisted_state=persisted_state,
    )
    if _is_system_non_business(tree_node):
        matrix_visibility = MatrixNodeVisibility.SYSTEM_NON_BUSINESS
        disposition = MatrixNodeDisposition.RETAIN
    elif disposition is MatrixNodeDisposition.EXCLUDE:
        matrix_visibility = MatrixNodeVisibility.HUMAN_EXCLUDED
    else:
        matrix_visibility = MatrixNodeVisibility.VISIBLE
    analysis_eligibility = (
        MatrixAnalysisEligibility.ELIGIBLE
        if requirement_id and analysis_candidate
        else MatrixAnalysisEligibility.NOT_ELIGIBLE
    )
    projected_ai_assessment = _matrix_ai_assessment(requirement)
    projected_manual_decision = _matrix_manual_decision(requirement)
    stale_reference = _matrix_assessment_reference_status(requirement) == (
        "STALE_REFERENCE"
    )
    if stale_reference:
        ai_assessment = None
        manual_decision = None
        stale_ai_assessment = projected_ai_assessment
        stale_manual_decision = projected_manual_decision
        analysis_result_state = MatrixAnalysisResultState.STALE_REFERENCE
        compliance_status = ""
    else:
        ai_assessment = projected_ai_assessment
        manual_decision = projected_manual_decision
        stale_ai_assessment = None
        stale_manual_decision = None
        compliance_status = str(
            (requirement or {}).get("compliance_status") or ""
        )
        analysis_result_state = (
            MatrixAnalysisResultState.CURRENT
            if ai_assessment is not None
            or manual_decision is not None
            or compliance_status.strip()
            else MatrixAnalysisResultState.NONE
        )
    if (
        analysis_eligibility is MatrixAnalysisEligibility.NOT_ELIGIBLE
        or matrix_visibility is not MatrixNodeVisibility.VISIBLE
        or disposition is not MatrixNodeDisposition.ANALYZE
    ):
        analysis_status = MatrixAnalysisStatus.NOT_APPLICABLE
    elif manual_decision is not None:
        analysis_status = MatrixAnalysisStatus.DECIDED
    elif ai_assessment is not None:
        analysis_status = MatrixAnalysisStatus.ASSESSED
    else:
        analysis_status = MatrixAnalysisStatus.NOT_STARTED
    human = _matrix_human_presentation(
        tree_node,
        table_context=table_context or {},
        resolved_table_record_id=(
            resolved_table_record_id or tree_node.table_record_id
        ),
    )
    return MatrixNodeStateV1(
        node_id=tree_node.id,
        parent_id=tree_node.parent_id,
        child_ids=tree_node.child_ids,
        depth=tree_node.depth,
        source_order=tree_node.source_order,
        display_role=_matrix_node_kind(tree_node),
        source_kind=str(tree_node.kind or "").upper(),
        source_number=human["source_number"],
        display_title=human["display_title"],
        display_text=human["display_text"],
        label=tree_node.label,
        summary=tree_node.summary,
        content_blocks=tree_node.content_blocks,
        table_refs=tree_node.table_refs,
        figure_refs=tree_node.figure_refs,
        source_refs=tree_node.source_refs,
        page_start=tree_node.page_start,
        page_end=tree_node.page_end,
        structure_node_id=tree_node.structure_node_id,
        requirement_entry_id=(
            str(tree_node.requirement_entry_id)
            if tree_node.requirement_entry_id
            else None
        ),
        requirement_id=requirement_id,
        requirement_code=str((requirement or {}).get("req_code") or ""),
        requirement_text=str(
            human["display_text"]
            or (
                (tree_node.summary or tree_node.label or "")
                if not tree_node.child_ids
                else ""
            )
        ),
        # Requirement atoms deliberately do not duplicate table-record identity
        # on the tree node.  The immutable evidence bundle is the authority for
        # that lineage, so Matrix carries the resolved record for every
        # downstream consumer (AI, UI and Excel).
        table_record_id=resolved_table_record_id or tree_node.table_record_id,
        logical_table_ids=logical_table_ids,
        matrix_visibility=matrix_visibility,
        disposition=disposition,
        disposition_reason=str((persisted_state or {}).get("reason") or ""),
        analysis_eligibility=analysis_eligibility,
        analysis_status=analysis_status,
        analysis_result_state=analysis_result_state,
        ai_assessment=ai_assessment,
        manual_decision=manual_decision,
        stale_ai_assessment=stale_ai_assessment,
        stale_manual_decision=stale_manual_decision,
        compliance_status=compliance_status,
        save_revision=int((persisted_state or {}).get("revision") or 0),
        updated_at=_latest_timestamp(
            str((persisted_state or {}).get("updated_at") or ""),
            str((requirement or {}).get("assessment_updated_at") or ""),
            str((requirement or {}).get("updated_at") or ""),
        ),
    )


def _default_disposition(
    *,
    entry_kind: str,
    projection_role: str,
    analysis_candidate: bool,
    persisted_state: dict[str, Any] | None,
) -> MatrixNodeDisposition:
    persisted = str((persisted_state or {}).get("disposition") or "").upper()
    if persisted in {item.value for item in MatrixNodeDisposition}:
        return MatrixNodeDisposition(persisted)
    if (
        projection_role == "REQUIREMENT"
        and entry_kind in _ANALYZABLE_ENTRY_KINDS
        and analysis_candidate
    ):
        return MatrixNodeDisposition.ANALYZE
    if entry_kind == "EXCLUDED":
        return MatrixNodeDisposition.EXCLUDE
    return MatrixNodeDisposition.RETAIN


def _is_analysis_candidate(
    *,
    tree_node: RequirementTreeNodeV1,
    requirement: dict[str, Any] | None,
) -> bool:
    """Return whether the frozen shape can carry one direct human decision.

    Eligibility describes the node's intrinsic shape.  It deliberately does not
    depend on the current disposition: a retained structural leaf can be opted
    into analysis later, while roll-ups and table containers must first be split
    into a directly decidable child.
    """

    if _is_system_non_business(tree_node):
        return False
    role = str(tree_node.projection_role or "").strip().upper()
    if role not in {"REQUIREMENT", "DIRECT_CONTENT", "STRUCTURE_PROXY"}:
        return False
    if tree_node.child_ids:
        return False
    decision_reference = (requirement or {}).get("decision_reference")
    if isinstance(decision_reference, dict):
        decision_mode = str(
            decision_reference.get("decision_mode") or ""
        ).strip().upper()
        if decision_mode == "ROLLUP":
            return False
    return True


def _is_system_non_business(node: RequirementTreeNodeV1) -> bool:
    values = {
        str(node.kind or "").strip().upper(),
        str(node.projection_role or "").strip().upper(),
        str(node.classification or "").strip().upper(),
    }
    proven_non_business = {
        "NOISE",
        "COVER",
        "TABLE_OF_CONTENTS",
        "TOC",
        "HEADER",
        "FOOTER",
        "PAGE_HEADER",
        "PAGE_FOOTER",
        "WATERMARK",
        "SYSTEM_NON_BUSINESS",
    }
    if values & proven_non_business:
        return True

    # DOCUMENT is the immutable hierarchy wrapper, not a user-facing Matrix
    # row.  Keep it in the frozen graph for lineage while excluding it from
    # analysis and export.  Some native PDFs expose a TOC as an ordinary GROUP
    # without a semantic role, so use a deliberately small exact-title fallback
    # instead of content heuristics that could hide a real business clause.
    if str(node.kind or "").strip().upper() == "DOCUMENT":
        return True
    compact_label = "".join(str(node.label or "").split()).strip(":：")
    return compact_label.casefold() in {
        "目录",
        "目录页",
        "目次",
        "contents",
        "tableofcontents",
    }


def _matrix_ai_assessment(
    requirement: dict[str, Any] | None,
) -> MatrixAiAssessmentV1 | None:
    row = requirement or {}
    mode = str(row.get("assessment_mode") or "").strip()
    if not mode or mode.lower() == "manual":
        return None
    if not any(
        str(row.get(field) or "").strip()
        for field in (
            "ai_decision",
            "compliance_status",
            "ai_reason",
            "analysis_basis",
            "clarification_question",
        )
    ):
        return None
    confidence: float | None
    try:
        raw_confidence = row.get("ai_confidence")
        confidence = None if raw_confidence in {None, ""} else float(raw_confidence)
    except (TypeError, ValueError):
        confidence = None
    return MatrixAiAssessmentV1(
        decision=str(row.get("ai_decision") or ""),
        compliance_status=str(row.get("compliance_status") or ""),
        reason=str(row.get("ai_reason") or ""),
        analysis_basis=str(row.get("analysis_basis") or ""),
        clarification_question=str(row.get("clarification_question") or ""),
        confidence=confidence,
        assessment_mode=mode,
    )


def _matrix_assessment_reference_status(
    requirement: dict[str, Any] | None,
) -> str:
    row = requirement or {}
    raw_status = row.get("assessment_reference_status")
    if raw_status in {None, ""}:
        raw_status = row.get("reference_status")
    return str(getattr(raw_status, "value", raw_status) or "").strip().upper()


def _matrix_manual_decision(
    requirement: dict[str, Any] | None,
) -> MatrixManualDecisionV1 | None:
    row = requirement or {}
    decision = str(row.get("final_decision") or "").strip()
    if not decision:
        return None
    return MatrixManualDecisionV1(
        decision=decision,
        reason=str(row.get("engineer_reason") or ""),
        decided_at=str(row.get("assessment_updated_at") or ""),
    )


def _latest_timestamp(*values: str) -> str:
    return max((value for value in values if value), default="")


def _matrix_node_kind(node: RequirementTreeNodeV1) -> MatrixNodeKind:
    role = str(node.projection_role or "").upper()
    kind = str(node.kind or "").upper()
    if role == "REQUIREMENT" or kind == "REQUIREMENT":
        return MatrixNodeKind.ATOMIC_REQUIREMENT
    if role == "TABLE_RECORD" or kind == "TABLE_RECORD":
        return MatrixNodeKind.TABLE_RECORD
    if kind == "TABLE":
        return MatrixNodeKind.TABLE
    if kind in {"CLAUSE", "LIST_ITEM", "SUBCLAUSE"}:
        return MatrixNodeKind.CLAUSE
    if kind in {"DOCUMENT", "SECTION", "APPENDIX", "ANNEX", "LIST_GROUP"}:
        return MatrixNodeKind.STRUCTURE
    return MatrixNodeKind.CONTEXT


def _table_context(tree: RequirementTreeSnapshotV1) -> dict[str, Any]:
    projections_by_logical_id = {
        projection.table_fact.logical_table_id: projection
        for projection in tree.table_record_projections
    }
    evidence_by_id = {
        binding.binding_id: binding for binding in tree.table_evidence_bindings
    }
    requirement_evidence_by_id = {
        binding.binding_id: binding
        for binding in (
            tree.requirement_evidence_bundle.requirement_bindings
            if tree.requirement_evidence_bundle is not None
            else ()
        )
    }
    logical_id_by_record_id = {
        record.record_id: projection.table_fact.logical_table_id
        for projection in tree.table_record_projections
        for record in projection.records
    }
    records_by_id = {
        record.record_id: record
        for projection in tree.table_record_projections
        for record in projection.records
    }
    logical_ids_by_owner: dict[str, list[str]] = {}
    logical_id_by_physical_table_id: dict[str, str] = {}
    for logical_id, projection in projections_by_logical_id.items():
        logical_ids_by_owner.setdefault(
            projection.table_fact.owner_structure_node_id, []
        ).append(logical_id)
        for part in projection.table_fact.table_parts:
            logical_id_by_physical_table_id[part.table_id] = logical_id
    return {
        "projections_by_logical_id": projections_by_logical_id,
        "evidence_by_id": evidence_by_id,
        "requirement_evidence_by_id": requirement_evidence_by_id,
        "logical_id_by_record_id": logical_id_by_record_id,
        "records_by_id": records_by_id,
        "logical_ids_by_owner": logical_ids_by_owner,
        "logical_id_by_physical_table_id": logical_id_by_physical_table_id,
    }


def _logical_table_ids_for_node(
    node: RequirementTreeNodeV1,
    *,
    table_context: dict[str, Any],
) -> tuple[str, ...]:
    logical_ids: list[str] = []

    def add(value: object) -> None:
        normalized = str(value or "").strip()
        if normalized and normalized not in logical_ids:
            logical_ids.append(normalized)

    evidence = _resolved_table_evidence(node, table_context=table_context)
    if evidence is not None:
        add(evidence.logical_table_id)
    add(table_context["logical_id_by_record_id"].get(node.table_record_id or ""))
    for logical_id in table_context["logical_ids_by_owner"].get(
        node.structure_node_id or "", []
    ):
        add(logical_id)
    for table_ref in node.table_refs:
        add(table_ref.get("logical_table_id"))
        physical_id = str(
            table_ref.get("table_id") or table_ref.get("id") or ""
        )
        add(table_context["logical_id_by_physical_table_id"].get(physical_id))
    known = table_context["projections_by_logical_id"]
    return tuple(value for value in logical_ids if value in known)


def _resolved_table_evidence(
    node: RequirementTreeNodeV1,
    *,
    table_context: dict[str, Any],
) -> Any | None:
    """Resolve direct or requirement-owned immutable table evidence.

    Table-record structure nodes point at table evidence directly.  Atomic
    requirements point at their own requirement evidence, which in turn points
    at the table evidence.  Keeping this traversal in one place prevents AI,
    UI and export from silently falling back to a whole long table.
    """

    table_binding_id = str(node.table_evidence_binding_id or "")
    if not table_binding_id and node.requirement_evidence_binding_id:
        requirement_evidence = table_context[
            "requirement_evidence_by_id"
        ].get(node.requirement_evidence_binding_id)
        if requirement_evidence is not None:
            table_binding_id = str(
                requirement_evidence.table_evidence_binding_id or ""
            )
    if not table_binding_id:
        return None
    return table_context["evidence_by_id"].get(table_binding_id)


def _resolved_table_record_id(
    node: RequirementTreeNodeV1,
    *,
    table_context: dict[str, Any],
) -> str | None:
    evidence = _resolved_table_evidence(node, table_context=table_context)
    return str(evidence.record_id) if evidence is not None and evidence.record_id else None


def _project_logical_tables(
    *,
    tree: RequirementTreeSnapshotV1,
    nodes: tuple[MatrixNodeStateV1, ...],
    table_context: dict[str, Any],
) -> tuple[MatrixLogicalTableV1, ...]:
    output: list[MatrixLogicalTableV1] = []
    for projection in tree.table_record_projections:
        logical_id = projection.table_fact.logical_table_id
        bindings = tuple(
            binding
            for binding in tree.table_evidence_bindings
            if binding.logical_table_id == logical_id
        )
        output.append(
            MatrixLogicalTableV1(
                logical_table_id=logical_id,
                projection_id=projection.projection_id,
                projection_mode=projection.mode.value,
                record_count=len(projection.records),
                physical_table_ids=tuple(
                    part.table_id for part in projection.table_fact.table_parts
                ),
                node_ids=tuple(
                    node.node_id
                    for node in nodes
                    if logical_id in node.logical_table_ids
                ),
                evidence_binding_ids=tuple(
                    binding.binding_id for binding in bindings
                ),
                projection=projection.model_dump(mode="json"),
                evidence_bindings=tuple(
                    binding.model_dump(mode="json") for binding in bindings
                ),
            )
        )
    return tuple(output)


def _matrix_counts(
    *,
    nodes: tuple[MatrixNodeStateV1, ...],
    logical_tables: tuple[MatrixLogicalTableV1, ...],
) -> MatrixCountsV1:
    return MatrixCountsV1(
        total_nodes=len(nodes),
        visible_nodes=sum(
            node.matrix_visibility is MatrixNodeVisibility.VISIBLE for node in nodes
        ),
        system_non_business_nodes=sum(
            node.matrix_visibility is MatrixNodeVisibility.SYSTEM_NON_BUSINESS
            for node in nodes
        ),
        human_excluded_nodes=sum(
            node.matrix_visibility is MatrixNodeVisibility.HUMAN_EXCLUDED
            for node in nodes
        ),
        analyze_nodes=sum(
            node.disposition is MatrixNodeDisposition.ANALYZE for node in nodes
        ),
        retained_nodes=sum(
            node.disposition is MatrixNodeDisposition.RETAIN for node in nodes
        ),
        excluded_nodes=sum(
            node.disposition is MatrixNodeDisposition.EXCLUDE for node in nodes
        ),
        analysis_eligible_nodes=sum(
            node.analysis_eligibility is MatrixAnalysisEligibility.ELIGIBLE
            for node in nodes
        ),
        assessed_nodes=sum(
            node.analysis_status
            in {MatrixAnalysisStatus.ASSESSED, MatrixAnalysisStatus.DECIDED}
            for node in nodes
        ),
        decided_nodes=sum(
            node.analysis_status is MatrixAnalysisStatus.DECIDED for node in nodes
        ),
        stale_result_nodes=sum(
            node.analysis_result_state
            is MatrixAnalysisResultState.STALE_REFERENCE
            for node in nodes
        ),
        logical_tables=len(logical_tables),
    )


def _matrix_node_path(
    node: MatrixNodeStateV1,
    *,
    node_by_id: dict[str, MatrixNodeStateV1],
) -> tuple[MatrixNodeStateV1, ...]:
    path: list[MatrixNodeStateV1] = [node]
    current = node
    while current.parent_id:
        parent = node_by_id.get(current.parent_id)
        if parent is None:
            break
        path.append(parent)
        current = parent
    path.reverse()
    return tuple(path)


def _assert_requirement_binding(
    requirement: dict[str, Any],
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> None:
    if (
        str(requirement.get("project_id") or "") != project_id
        or str(requirement.get("sor_document_id") or "") != document_id
        or str(requirement.get("requirement_set_id") or "")
        != requirement_set_id
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "matrix_requirement_binding_mismatch",
                "message": "需求条目与当前版本的绑定不一致。",
            },
        )


__all__ = [
    "build_requirement_matrix_snapshot",
    "get_requirement_matrix",
    "get_requirement_matrix_logical_table",
    "get_requirement_matrix_node_detail",
    "patch_requirement_matrix_dispositions",
    "patch_requirement_matrix_node_disposition",
    "post_requirement_matrix_node_decision",
]
