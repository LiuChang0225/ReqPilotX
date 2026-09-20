"""Canonical assessment scope projection for W6-A and W6-B.

This module is intentionally pure.  It projects an already verified Matrix,
serializes the immutable scope claim, and selects worker rows.  It never loads
an AI profile, synchronizes knowledge, calls a provider, or writes storage.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Iterable, Mapping

from app.domain.requirement_matrix import (
    MatrixAnalysisEligibility,
    MatrixNodeDisposition,
    MatrixNodeStateV1,
    MatrixNodeVisibility,
    MatrixSnapshotState,
    RequirementMatrixSnapshotV1,
)

ASSESSMENT_SCOPE_SCHEMA_VERSION_V1 = "requirement-analysis-scope/v1"
ASSESSMENT_SCOPE_SCHEMA_VERSION_V2 = "requirement-analysis-scope/v2"
ASSESSMENT_SCOPE_SCHEMA_VERSION = ASSESSMENT_SCOPE_SCHEMA_VERSION_V1
ASSESSMENT_SCOPE_PREVIEW_SCHEMA_VERSION_V1 = "requirement-analysis-scope-preview/v1"
ASSESSMENT_SCOPE_PREVIEW_SCHEMA_VERSION_V2 = "requirement-analysis-scope-preview/v2"
ASSESSMENT_SCOPE_PREVIEW_SCHEMA_VERSION = ASSESSMENT_SCOPE_PREVIEW_SCHEMA_VERSION_V1
ASSESSMENT_COMMAND_SCHEMA_VERSION = "requirement-analysis-command/v1"
ATOMIC_BATCH_PUBLICATION_MODE = "ATOMIC_BATCH"
PER_NODE_RECOVERABLE_PUBLICATION_MODE = "PER_NODE_RECOVERABLE"


class AssessmentScopeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_assessment_scope(
    matrix: RequirementMatrixSnapshotV1,
    *,
    requirement_set_version: int,
    scope: str,
    requested_node_ids: Iterable[str],
    schema_version: str = ASSESSMENT_SCOPE_SCHEMA_VERSION,
    publication_mode: str | None = None,
    source_job_id: str = "",
    retry_node_ids: Iterable[str] = (),
    retry_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one canonical source-ordered scope envelope."""

    if matrix.state is not MatrixSnapshotState.READY:
        raise AssessmentScopeError(
            "matrix_requires_reextraction",
            "该需求版本没有可信的冻结结构树，请重新提取生成新版本。",
        )
    normalized_schema = str(schema_version or "").strip()
    if normalized_schema not in {
        ASSESSMENT_SCOPE_SCHEMA_VERSION_V1,
        ASSESSMENT_SCOPE_SCHEMA_VERSION_V2,
    }:
        raise AssessmentScopeError(
            "analysis_scope_schema_invalid", "不支持的分析范围快照版本。"
        )
    is_recoverable = normalized_schema == ASSESSMENT_SCOPE_SCHEMA_VERSION_V2
    expected_publication_mode = (
        PER_NODE_RECOVERABLE_PUBLICATION_MODE
        if is_recoverable
        else ATOMIC_BATCH_PUBLICATION_MODE
    )
    normalized_publication_mode = str(
        publication_mode or expected_publication_mode
    ).strip()
    if normalized_publication_mode != expected_publication_mode:
        raise AssessmentScopeError(
            "analysis_scope_publication_mode_invalid",
            "分析范围版本与发布模式不匹配。",
        )

    normalized_scope = str(scope or "").strip().upper()
    allowed_scopes = (
        {"CURRENT", "SELECTED", "ALL", "FAILED_ONLY"}
        if is_recoverable
        else {"CURRENT", "SELECTED", "ALL"}
    )
    if normalized_scope not in allowed_scopes:
        raise AssessmentScopeError(
            "analysis_scope_invalid", "分析范围必须是当前项、所选项或全部分析项。"
        )
    raw_ids = tuple(str(item or "").strip() for item in requested_node_ids)
    if any(not item for item in raw_ids) or len(raw_ids) != len(set(raw_ids)):
        raise AssessmentScopeError(
            "analysis_scope_node_ids_invalid", "分析范围中的节点 ID 不能为空或重复。"
        )
    normalized_source_job_id = str(source_job_id or "").strip()
    raw_retry_ids = tuple(str(item or "").strip() for item in retry_node_ids)
    if any(not item for item in raw_retry_ids) or len(raw_retry_ids) != len(
        set(raw_retry_ids)
    ):
        raise AssessmentScopeError(
            "analysis_scope_retry_node_ids_invalid",
            "失败项重试范围中的节点 ID 不能为空或重复。",
        )
    if normalized_scope == "FAILED_ONLY":
        if raw_ids:
            raise AssessmentScopeError(
                "analysis_scope_failed_only_rejects_nodes",
                "失败项重试由服务器冻结，不能由客户端提交节点 ID。",
            )
        if not normalized_source_job_id:
            raise AssessmentScopeError(
                "analysis_scope_failed_only_requires_source_job",
                "失败项重试必须绑定来源分析任务。",
            )
        if retry_metadata is None:
            raise AssessmentScopeError(
                "analysis_scope_retry_metadata_missing",
                "失败项重试缺少服务器冻结的失败快照。",
            )
    elif normalized_source_job_id or raw_retry_ids or retry_metadata is not None:
        raise AssessmentScopeError(
            "analysis_scope_retry_metadata_unexpected",
            "只有失败项重试范围可以携带来源任务与失败快照。",
        )
    if normalized_scope == "CURRENT" and len(raw_ids) != 1:
        raise AssessmentScopeError(
            "analysis_scope_current_requires_one",
            "当前项分析必须且只能选择一个节点。",
        )
    if normalized_scope == "SELECTED" and not raw_ids:
        raise AssessmentScopeError(
            "analysis_scope_selected_requires_nodes",
            "所选项分析至少需要选择一个节点。",
        )
    if normalized_scope == "ALL" and raw_ids:
        raise AssessmentScopeError(
            "analysis_scope_all_rejects_nodes",
            "全部分析项由服务器展开，不能同时提交节点 ID。",
        )

    ordered_nodes = _nodes_in_source_order(matrix)
    node_by_id = {node.node_id: node for node in ordered_nodes}
    unknown_ids = [node_id for node_id in raw_ids if node_id not in node_by_id]
    if unknown_ids:
        raise AssessmentScopeError(
            "analysis_scope_node_not_in_requirement_set",
            "分析范围包含不属于当前冻结需求版本的节点。",
        )
    unknown_retry_ids = [
        node_id for node_id in raw_retry_ids if node_id not in node_by_id
    ]
    if unknown_retry_ids:
        raise AssessmentScopeError(
            "analysis_scope_retry_node_not_in_requirement_set",
            "失败项重试包含不属于当前冻结需求版本的节点。",
        )

    if normalized_scope == "ALL":
        # "全部" means every business row the user can reason about, not only
        # the provider-call subset.  Keeping non-target business rows in the
        # frozen envelope makes the preview's skipped count honest while
        # system furniture (cover/TOC/header/footer/watermark) stays outside
        # the user-facing analysis range.
        selected_nodes = tuple(
            node
            for node in ordered_nodes
            if node.matrix_visibility is not MatrixNodeVisibility.SYSTEM_NON_BUSINESS
        )
    elif normalized_scope == "FAILED_ONLY":
        retry_id_set = set(raw_retry_ids)
        selected_nodes = tuple(
            node for node in ordered_nodes if node.node_id in retry_id_set
        )
        if tuple(node.node_id for node in selected_nodes) != raw_retry_ids:
            raise AssessmentScopeError(
                "analysis_scope_retry_order_invalid",
                "失败项重试目标必须按来源顺序冻结。",
            )
        if any(not _is_target(node) for node in selected_nodes):
            raise AssessmentScopeError(
                "analysis_scope_retry_target_invalid",
                "失败项重试只能包含当前仍可分析的需求。",
            )
    else:
        selected_id_set = set(raw_ids)
        selected_nodes = tuple(
            node for node in ordered_nodes if node.node_id in selected_id_set
        )

    target_nodes = tuple(node for node in selected_nodes if _is_target(node))
    skipped_nodes = tuple(node for node in selected_nodes if not _is_target(node))
    analysis_universe_nodes = tuple(node for node in ordered_nodes if _is_target(node))
    canonical_requested_ids = tuple(node.node_id for node in selected_nodes)
    target_pairs = tuple(
        {
            "node_id": node.node_id,
            "requirement_id": str(node.requirement_id or ""),
        }
        for node in target_nodes
    )
    skipped = tuple(
        {
            "node_id": node.node_id,
            "reason_code": _skip_reason(node),
        }
        for node in skipped_nodes
    )
    analysis_universe_pairs = tuple(
        {
            "node_id": node.node_id,
            "requirement_id": str(node.requirement_id or ""),
        }
        for node in analysis_universe_nodes
    )
    skipped_reason_counts = dict(
        sorted(Counter(item["reason_code"] for item in skipped).items())
    )
    payload: dict[str, Any] = {
        "schema_version": normalized_schema,
        "requirement_set_id": matrix.requirement_set_id,
        "requirement_set_version": max(int(requirement_set_version or 0), 0),
        "tree_sha256": matrix.tree_sha256,
        "scope": normalized_scope,
        "requested_node_ids": list(canonical_requested_ids),
        "target_pairs": list(target_pairs),
        "analysis_universe_pairs": list(analysis_universe_pairs),
        "skipped": list(skipped),
        "target_examples": [_node_example(node) for node in target_nodes[:8]],
        "skipped_examples": [
            {
                **_node_example(node),
                "reason": _skip_reason(node),
                "reason_code": _skip_reason(node),
            }
            for node in skipped_nodes[:8]
        ],
        "publication_mode": normalized_publication_mode,
        "skipped_reason_counts": skipped_reason_counts,
    }
    if is_recoverable:
        retry_snapshot_sha256 = ""
        retry_summary = {
            "source_failed_count": 0,
            "target": 0,
            "already_current": 0,
            "skipped": 0,
        }
        if normalized_scope == "FAILED_ONLY":
            metadata = dict(retry_metadata or {})
            retry_snapshot_sha256 = str(
                metadata.get("failed_snapshot_sha256") or ""
            ).strip()
            if not _is_sha256(retry_snapshot_sha256):
                raise AssessmentScopeError(
                    "analysis_scope_failed_snapshot_invalid",
                    "失败项重试快照哈希无效。",
                )
            source_failed_count = _non_negative_int(
                metadata.get("source_failed_count"),
                code="analysis_scope_retry_summary_invalid",
            )
            already_current = _non_negative_int(
                metadata.get("already_current", 0),
                code="analysis_scope_retry_summary_invalid",
            )
            retry_skipped = _non_negative_int(
                metadata.get("skipped", 0),
                code="analysis_scope_retry_summary_invalid",
            )
            retry_summary = {
                "source_failed_count": source_failed_count,
                "target": len(target_pairs),
                "already_current": already_current,
                "skipped": retry_skipped,
            }
            if source_failed_count != sum(
                retry_summary[key] for key in ("target", "already_current", "skipped")
            ):
                raise AssessmentScopeError(
                    "analysis_scope_retry_summary_invalid",
                    "失败项重试计数与来源失败快照不一致。",
                )
        payload.update(
            {
                "source_job_id": normalized_source_job_id,
                "failed_snapshot_sha256": retry_snapshot_sha256,
                "retry_summary": retry_summary,
            }
        )
    payload_json = canonical_json(payload)
    return {
        **payload,
        "scope_payload_json": payload_json,
        "scope_sha256": sha256_text(payload_json),
    }


def assessment_scope_preview(
    scope_envelope: dict[str, Any], *, command_id: str
) -> dict[str, Any]:
    target_pairs = scope_target_pairs(scope_envelope)
    requested_ids = _string_list(scope_envelope.get("requested_node_ids"))
    skipped = _dict_list(scope_envelope.get("skipped"))
    scope_schema_version = str(scope_envelope.get("schema_version") or "")
    is_recoverable = scope_schema_version == ASSESSMENT_SCOPE_SCHEMA_VERSION_V2
    preview = {
        "schema_version": (
            ASSESSMENT_SCOPE_PREVIEW_SCHEMA_VERSION_V2
            if is_recoverable
            else ASSESSMENT_SCOPE_PREVIEW_SCHEMA_VERSION_V1
        ),
        "command_id": str(command_id or "").strip(),
        "requirement_set_id": str(scope_envelope.get("requirement_set_id") or ""),
        "requirement_set_version": max(
            int(scope_envelope.get("requirement_set_version") or 0), 0
        ),
        "tree_sha256": str(scope_envelope.get("tree_sha256") or ""),
        "scope": str(scope_envelope.get("scope") or "ALL"),
        "scope_sha256": str(scope_envelope.get("scope_sha256") or ""),
        "requested_node_ids": requested_ids,
        "target_node_ids": [item[0] for item in target_pairs],
        "target_requirement_ids": [item[1] for item in target_pairs],
        "requested_count": len(requested_ids),
        "target_count": len(target_pairs),
        "skipped_count": len(skipped),
        "skipped_reason_counts": dict(
            scope_envelope.get("skipped_reason_counts") or {}
        ),
        "target_examples": _dict_list(scope_envelope.get("target_examples")),
        "skipped_examples": _dict_list(scope_envelope.get("skipped_examples")),
        "publication_mode": str(scope_envelope.get("publication_mode") or ""),
    }
    if is_recoverable:
        retry_summary = _retry_summary(scope_envelope)
        preview.update(
            {
                "source_job_id": str(scope_envelope.get("source_job_id") or ""),
                "failed_snapshot_sha256": str(
                    scope_envelope.get("failed_snapshot_sha256") or ""
                ),
                "source_failed_count": retry_summary["source_failed_count"],
                "retry_target_count": retry_summary["target"],
                "already_current_count": retry_summary["already_current"],
                "retry_already_current_count": retry_summary["already_current"],
                "retry_skipped_count": retry_summary["skipped"],
                "retry_summary": retry_summary,
            }
        )
    return preview


def parse_assessment_scope(
    payload_json: str, *, expected_scope_sha256: str = ""
) -> dict[str, Any]:
    raw = str(payload_json or "").strip()
    if not raw:
        raise AssessmentScopeError(
            "analysis_scope_snapshot_missing", "分析任务没有冻结的范围快照。"
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssessmentScopeError(
            "analysis_scope_snapshot_invalid", "分析任务范围快照不是有效 JSON。"
        ) from exc
    if not isinstance(parsed, dict):
        raise AssessmentScopeError(
            "analysis_scope_snapshot_invalid", "分析任务范围快照必须是对象。"
        )
    canonical = canonical_json(parsed)
    if canonical != raw:
        raise AssessmentScopeError(
            "analysis_scope_snapshot_not_canonical", "分析任务范围快照不是规范 JSON。"
        )
    actual_hash = sha256_text(canonical)
    expected_hash = str(expected_scope_sha256 or "").strip()
    if expected_hash and actual_hash != expected_hash:
        raise AssessmentScopeError(
            "analysis_scope_snapshot_hash_mismatch", "分析任务范围快照哈希不匹配。"
        )
    schema_version = str(parsed.get("schema_version") or "")
    expected_publication_modes = {
        ASSESSMENT_SCOPE_SCHEMA_VERSION_V1: ATOMIC_BATCH_PUBLICATION_MODE,
        ASSESSMENT_SCOPE_SCHEMA_VERSION_V2: PER_NODE_RECOVERABLE_PUBLICATION_MODE,
    }
    if schema_version not in expected_publication_modes:
        raise AssessmentScopeError(
            "analysis_scope_snapshot_schema_invalid", "不支持的分析范围快照版本。"
        )
    if parsed.get("publication_mode") != expected_publication_modes[schema_version]:
        raise AssessmentScopeError(
            "analysis_scope_publication_mode_invalid", "分析范围版本与发布模式不匹配。"
        )
    normalized_scope = str(parsed.get("scope") or "").strip().upper()
    allowed_scopes = (
        {"CURRENT", "SELECTED", "ALL", "FAILED_ONLY"}
        if schema_version == ASSESSMENT_SCOPE_SCHEMA_VERSION_V2
        else {"CURRENT", "SELECTED", "ALL"}
    )
    if normalized_scope not in allowed_scopes:
        raise AssessmentScopeError(
            "analysis_scope_snapshot_invalid", "冻结分析范围类型无效。"
        )
    # Force structural validation before the snapshot is trusted by a worker.
    target_pairs = scope_target_pairs(parsed)
    universe_pairs = scope_analysis_universe_pairs(parsed)
    universe_positions = {pair: index for index, pair in enumerate(universe_pairs)}
    if (
        any(pair not in universe_positions for pair in target_pairs)
        or tuple(sorted(target_pairs, key=universe_positions.get)) != target_pairs
    ):
        raise AssessmentScopeError(
            "analysis_scope_target_binding_invalid",
            "冻结分析目标不是完整分析集合的有序子集。",
        )
    requested = _string_list(parsed.get("requested_node_ids"))
    if any(not node_id for node_id in requested) or len(requested) != len(
        set(requested)
    ):
        raise AssessmentScopeError(
            "analysis_scope_snapshot_invalid", "冻结分析范围包含重复节点。"
        )
    if schema_version == ASSESSMENT_SCOPE_SCHEMA_VERSION_V2:
        _validate_recoverable_scope_snapshot(
            parsed,
            scope=normalized_scope,
            requested=requested,
            target_pairs=target_pairs,
        )
    return {**parsed, "scope_payload_json": canonical, "scope_sha256": actual_hash}


def scope_target_pairs(scope_envelope: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return _scope_pairs(
        scope_envelope,
        field="target_pairs",
        invalid_message="冻结分析范围包含无效的节点与需求绑定。",
    )


def scope_analysis_universe_pairs(
    scope_envelope: dict[str, Any],
) -> tuple[tuple[str, str], ...]:
    pairs = _scope_pairs(
        scope_envelope,
        field="analysis_universe_pairs",
        invalid_message="冻结分析范围缺少完整的分析目标集合。",
    )
    if not pairs:
        raise AssessmentScopeError(
            "analysis_scope_universe_invalid",
            "冻结分析范围缺少完整的分析目标集合。",
        )
    return pairs


def _scope_pairs(
    scope_envelope: dict[str, Any],
    *,
    field: str,
    invalid_message: str,
) -> tuple[tuple[str, str], ...]:
    pairs = _dict_list(scope_envelope.get(field))
    normalized: list[tuple[str, str]] = []
    for pair in pairs:
        node_id = str(pair.get("node_id") or "").strip()
        requirement_id = str(pair.get("requirement_id") or "").strip()
        if not node_id or not requirement_id:
            raise AssessmentScopeError(
                "analysis_scope_target_binding_invalid",
                invalid_message,
            )
        normalized.append((node_id, requirement_id))
    if len(normalized) != len(set(normalized)):
        raise AssessmentScopeError(
            "analysis_scope_target_binding_invalid", "冻结分析范围包含重复目标。"
        )
    if len({item[0] for item in normalized}) != len(normalized) or len(
        {item[1] for item in normalized}
    ) != len(normalized):
        raise AssessmentScopeError(
            "analysis_scope_target_binding_invalid",
            "冻结分析范围中的节点与需求必须一一对应。",
        )
    return tuple(normalized)


def select_rows_for_assessment_scope(
    eligible_rows: Iterable[dict[str, Any]],
    scope_envelope: dict[str, Any],
) -> list[dict[str, Any]]:
    """Select rows only when Matrix order and node↔requirement pairs match exactly."""

    rows = list(eligible_rows)
    target_pairs = scope_target_pairs(scope_envelope)
    row_pairs = tuple(
        (
            str(row.get("matrix_node_id") or "").strip(),
            str(row.get("id") or "").strip(),
        )
        for row in rows
    )
    if row_pairs and all(
        not node_id and requirement_id for node_id, requirement_id in row_pairs
    ):
        # Compatibility for focused callers/tests that provide raw persisted
        # requirement rows.  The frozen Matrix still supplies the node binding;
        # requirement identity must remain unique and exact.
        row_by_requirement_id = {str(row.get("id") or "").strip(): row for row in rows}
        if len(row_by_requirement_id) != len(rows):
            raise AssessmentScopeError(
                "analysis_scope_worker_binding_invalid",
                "需求矩阵的分析目标绑定不唯一。",
            )
        selected: list[dict[str, Any]] = []
        for node_id, requirement_id in target_pairs:
            row = row_by_requirement_id.get(requirement_id)
            if row is None:
                raise AssessmentScopeError(
                    "analysis_scope_worker_membership_changed",
                    "冻结分析范围已不再属于当前需求矩阵。",
                )
            selected.append({**row, "matrix_node_id": node_id})
        return selected
    if any(not node_id or not requirement_id for node_id, requirement_id in row_pairs):
        raise AssessmentScopeError(
            "analysis_scope_worker_binding_invalid",
            "需求矩阵的分析目标缺少节点与需求绑定。",
        )
    row_by_pair = {pair: row for pair, row in zip(row_pairs, rows, strict=True)}
    if len(row_by_pair) != len(row_pairs):
        raise AssessmentScopeError(
            "analysis_scope_worker_binding_invalid", "需求矩阵的分析目标绑定不唯一。"
        )
    if any(pair not in row_by_pair for pair in target_pairs):
        raise AssessmentScopeError(
            "analysis_scope_worker_membership_changed",
            "冻结分析范围已不再属于当前需求矩阵。",
        )
    selected = [row_by_pair[pair] for pair in target_pairs]
    selected_pairs = tuple(
        (
            str(row.get("matrix_node_id") or "").strip(),
            str(row.get("id") or "").strip(),
        )
        for row in selected
    )
    if selected_pairs != target_pairs:
        raise AssessmentScopeError(
            "analysis_scope_worker_order_changed", "冻结分析范围的源顺序发生变化。"
        )
    return selected


def assessment_command_sha256(
    *,
    command_id: str,
    scope_sha256: str,
    requirement_set_id: str,
    requirement_set_version: int,
    requirement_tree_sha256: str,
    knowledge_base_version: int,
    profile_id: str,
    provider: str,
    assessment_mode: str,
    retrieval_backend: str,
    chat_model: str,
    embedding_model: str,
) -> str:
    payload = {
        "schema_version": ASSESSMENT_COMMAND_SCHEMA_VERSION,
        "command_id": str(command_id or "").strip(),
        "scope_sha256": str(scope_sha256 or "").strip(),
        "requirement_set_id": str(requirement_set_id or "").strip(),
        "requirement_set_version": max(int(requirement_set_version or 0), 0),
        "requirement_tree_sha256": str(requirement_tree_sha256 or "").strip(),
        "knowledge_base_version": max(int(knowledge_base_version or 0), 0),
        "profile_id": str(profile_id or "").strip(),
        "provider": str(provider or "").strip(),
        "assessment_mode": str(assessment_mode or "").strip(),
        "retrieval_backend": str(retrieval_backend or "").strip(),
        "chat_model": str(chat_model or "").strip(),
        "embedding_model": str(embedding_model or "").strip(),
    }
    return sha256_text(canonical_json(payload))


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _nodes_in_source_order(
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
        for child_id in node.child_ids:
            visit(child_id)

    for root_id in matrix.root_node_ids:
        visit(root_id)
    return tuple(ordered)


def _is_target(node: MatrixNodeStateV1) -> bool:
    return bool(
        node.matrix_visibility is MatrixNodeVisibility.VISIBLE
        and node.disposition is MatrixNodeDisposition.ANALYZE
        and node.analysis_eligibility is MatrixAnalysisEligibility.ELIGIBLE
        and str(node.requirement_id or "").strip()
    )


def _skip_reason(node: MatrixNodeStateV1) -> str:
    if node.matrix_visibility is MatrixNodeVisibility.SYSTEM_NON_BUSINESS:
        return "SYSTEM_NON_BUSINESS"
    if node.matrix_visibility is not MatrixNodeVisibility.VISIBLE:
        return "NOT_VISIBLE"
    if node.disposition is not MatrixNodeDisposition.ANALYZE:
        return "NOT_MARKED_FOR_ANALYSIS"
    if node.analysis_eligibility is not MatrixAnalysisEligibility.ELIGIBLE:
        return "NOT_ELIGIBLE"
    return "MISSING_REQUIREMENT_BINDING"


def _node_example(node: MatrixNodeStateV1) -> dict[str, Any]:
    label = str(
        node.display_title
        or node.label
        or node.summary
        or node.display_text
        or node.requirement_code
        or node.node_id
    ).strip()
    return {
        "node_id": node.node_id,
        "requirement_id": str(node.requirement_id or ""),
        "requirement_code": str(node.requirement_code or ""),
        "label": label[:500],
    }


def _validate_recoverable_scope_snapshot(
    scope_envelope: dict[str, Any],
    *,
    scope: str,
    requested: list[str],
    target_pairs: tuple[tuple[str, str], ...],
) -> None:
    required_fields = {
        "source_job_id",
        "failed_snapshot_sha256",
        "retry_summary",
    }
    if any(field not in scope_envelope for field in required_fields):
        raise AssessmentScopeError(
            "analysis_scope_retry_snapshot_invalid",
            "可恢复分析范围缺少失败重试快照字段。",
        )
    source_job_id = str(scope_envelope.get("source_job_id") or "").strip()
    failed_snapshot_sha256 = str(
        scope_envelope.get("failed_snapshot_sha256") or ""
    ).strip()
    retry_summary = _retry_summary(scope_envelope)
    if scope == "FAILED_ONLY":
        if not source_job_id or not _is_sha256(failed_snapshot_sha256):
            raise AssessmentScopeError(
                "analysis_scope_retry_snapshot_invalid",
                "失败项重试没有绑定有效的来源任务与失败快照。",
            )
        if requested != [node_id for node_id, _requirement_id in target_pairs]:
            raise AssessmentScopeError(
                "analysis_scope_retry_snapshot_invalid",
                "失败项重试节点与冻结目标不一致。",
            )
        if retry_summary["target"] != len(target_pairs) or retry_summary[
            "source_failed_count"
        ] != sum(
            retry_summary[key] for key in ("target", "already_current", "skipped")
        ):
            raise AssessmentScopeError(
                "analysis_scope_retry_summary_invalid",
                "失败项重试计数与冻结目标不一致。",
            )
        return
    if source_job_id or failed_snapshot_sha256 or any(retry_summary.values()):
        raise AssessmentScopeError(
            "analysis_scope_retry_snapshot_invalid",
            "普通分析范围不能携带失败项重试快照。",
        )


def _retry_summary(scope_envelope: Mapping[str, Any]) -> dict[str, int]:
    value = scope_envelope.get("retry_summary")
    expected_fields = {
        "source_failed_count",
        "target",
        "already_current",
        "skipped",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise AssessmentScopeError(
            "analysis_scope_retry_summary_invalid", "失败项重试计数快照无效。"
        )
    return {
        field: _non_negative_int(
            value[field], code="analysis_scope_retry_summary_invalid"
        )
        for field in (
            "source_failed_count",
            "target",
            "already_current",
            "skipped",
        )
    }


def _non_negative_int(value: Any, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AssessmentScopeError(code, "失败项重试计数必须是非负整数。")
    return value


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value]


__all__ = [
    "ASSESSMENT_SCOPE_PREVIEW_SCHEMA_VERSION",
    "ASSESSMENT_SCOPE_PREVIEW_SCHEMA_VERSION_V1",
    "ASSESSMENT_SCOPE_PREVIEW_SCHEMA_VERSION_V2",
    "ASSESSMENT_SCOPE_SCHEMA_VERSION",
    "ASSESSMENT_SCOPE_SCHEMA_VERSION_V1",
    "ASSESSMENT_SCOPE_SCHEMA_VERSION_V2",
    "ATOMIC_BATCH_PUBLICATION_MODE",
    "PER_NODE_RECOVERABLE_PUBLICATION_MODE",
    "AssessmentScopeError",
    "assessment_command_sha256",
    "assessment_scope_preview",
    "build_assessment_scope",
    "parse_assessment_scope",
    "scope_analysis_universe_pairs",
    "scope_target_pairs",
    "select_rows_for_assessment_scope",
]
