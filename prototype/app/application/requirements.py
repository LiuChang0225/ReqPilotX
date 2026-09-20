from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from app import db
from app.adapters.persistence.assessment_results import (
    persist_serving_result,
)
from app.application.errors import ApplicationError
from app.application.requirements_contracts import (
    ActivateRequirementSetRequest,
    ApplyReviewedExtractionRequest,
    LockExtractionRequest,
    ReviewedRequirementEntry,
)
from app.domain.assessment.execution_policy import evaluate_assessment_execution
from app.services.ai_copilot import (
    load_ai_copilot_config,
    run_ai_copilot_assessment,
)
from app.services.complex_table_mode import (
    hydrate_complex_row_metadata,
    is_complex_review_only_row,
    is_complex_table_row,
    row_is_shadowed_by_complex_group,
)
from app.services.preview_cache import (
    get_preview_cache_state,
    load_cached_preview,
)
from app.services.table_assets import (
    _refresh_enriched_entry_text_fields,
    enrich_entries_with_table_assets,
    enrich_entry_with_table_assets,
    normalize_preserved_entry_table_assets,
    prepare_table_asset_urls,
    prepare_table_ref_urls,
)

logger = logging.getLogger(__name__)

_COMPLEX_ROW_FIELD_NAMES = (
    "complex_table_mode",
    "complex_zone_id",
    "complex_family",
    "complex_role",
    "complex_group_label",
    "complex_split_level",
    "complex_warnings",
    "complex_row_policy",
)

def _copy_complex_row_fields(row: dict[str, object]) -> dict[str, object]:
    hydrated = hydrate_complex_row_metadata(dict(row))
    return {field: hydrated.get(field) for field in _COMPLEX_ROW_FIELD_NAMES}

def list_document_requirement_sets(project_id: str, document_id: str) -> dict:
    from app.application.projects import _require_project

    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document.get("project_id") != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )
    requirement_sets = db.list_requirement_sets(project_id, document_id)
    business_key_by_set_id: dict[str, str] = {}
    for item in requirement_sets:
        rows = db.list_requirements(
            project_id,
            document_id,
            requirement_set_id=str(item.get("id") or ""),
        )
        inspection = _inspect_requirement_set_tree(item, rows)
        business_key = str(inspection.pop("_business_key", "") or "")
        item.update(inspection)
        if business_key:
            business_key_by_set_id[str(item.get("id") or "")] = business_key
    canonical_set_by_business_key: dict[str, str] = {}
    for item in requirement_sets:
        set_id = str(item.get("id") or "")
        business_key = business_key_by_set_id.get(set_id, "")
        if not business_key:
            continue
        canonical_set_id = canonical_set_by_business_key.setdefault(
            business_key,
            set_id,
        )
        item["duplicate_of_requirement_set_id"] = (
            "" if canonical_set_id == set_id else canonical_set_id
        )
    return {
        "project_id": project_id,
        "document_id": document_id,
        "active_requirement_set_id": str(
            document.get("active_requirement_set_id") or ""
        ),
        "requirement_sets": requirement_sets,
    }


def _inspect_requirement_set_tree(
    requirement_set: dict[str, Any],
    stored_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify one version and describe candidates without calling them final."""

    from app.application.extraction.requirement_tree_snapshots import (
        RequirementTreeSnapshotError,
        load_requirement_set_tree_snapshot,
    )
    from app.application.extraction.working_tree_publication import (
        _working_tree_publication_key,
    )
    from app.domain.extraction.requirement_tree import RequirementTreeState

    stored_ids = {
        str(row.get("id") or "")
        for row in stored_rows
        if str(row.get("id") or "")
    }
    output: dict[str, Any] = {
        "requirement_tree_state": "MISSING",
        "requirement_tree_issue": {
            "code": "REQUIREMENT_TREE_SNAPSHOT_MISSING",
        },
        "requirement_tree_has_evidence_bundle": False,
        "tree_lock_ready": False,
        "review_candidate_entry_count": 0,
        "structure_proxy_count": 0,
        "table_record_count": 0,
        "semantic_requirement_count": 0,
        "stored_review_entry_count": len(stored_ids),
        "included_requirement_count": sum(
            _is_ai_eligible_requirement(row) for row in stored_rows
        ),
        "reference_count": sum(
            str(row.get("entry_kind") or "").strip().upper() == "REFERENCE"
            for row in stored_rows
        ),
        "excluded_count": sum(
            str(row.get("entry_kind") or "").strip().upper() == "EXCLUDED"
            for row in stored_rows
        ),
        "count_semantics": (
            "REVIEW_CANDIDATES_NOT_CONFIRMED_FINAL_REQUIREMENTS"
        ),
        "_business_key": "",
    }
    object_key = str(
        requirement_set.get("requirement_tree_object_key") or ""
    ).strip()
    tree_sha256 = str(
        requirement_set.get("requirement_tree_sha256") or ""
    ).strip()
    publication_state = str(
        requirement_set.get("publication_state") or ""
    ).strip().upper()
    if not object_key or not tree_sha256:
        if publication_state == "PUBLISHING":
            output.update(
                {
                    "requirement_tree_state": "PUBLISHING",
                    "requirement_tree_issue": {
                        "code": "REQUIREMENT_TREE_PUBLICATION_IN_PROGRESS",
                    },
                }
            )
        elif (
            publication_state in {"READY", "FAILED"}
            or bool(object_key) != bool(tree_sha256)
        ):
            output.update(
                {
                    "requirement_tree_state": "INVALID",
                    "requirement_tree_issue": {
                        "code": "REQUIREMENT_TREE_SNAPSHOT_INVALID",
                    },
                }
            )
        return output
    try:
        snapshot = load_requirement_set_tree_snapshot(requirement_set)
    except RequirementTreeSnapshotError:
        output.update(
            {
                "requirement_tree_state": "INVALID",
                "requirement_tree_issue": {
                    "code": "REQUIREMENT_TREE_SNAPSHOT_INVALID",
                },
            }
        )
        return output
    if snapshot.state is not RequirementTreeState.READY:
        output["requirement_tree_state"] = (
            "MISSING"
            if snapshot.state is RequirementTreeState.REQUIRES_REEXTRACTION
            else snapshot.state.value
        )
        return output

    reviewable = tuple(
        node
        for node in snapshot.nodes
        if node.include_in_view
        and node.projection_role
        in {
            "STRUCTURE_PROXY",
            "DIRECT_CONTENT",
            "WHOLE_TABLE_REVIEW",
            "TABLE_RECORD",
            "REQUIREMENT",
        }
    )
    snapshot_entry_ids = {
        str(node.requirement_entry_id or "")
        for node in reviewable
        if str(node.requirement_entry_id or "")
    }
    has_bundle = snapshot.requirement_evidence_bundle is not None
    counts = {
        "review_candidate_entry_count": len(reviewable),
        "structure_proxy_count": sum(
            node.projection_role == "STRUCTURE_PROXY" for node in reviewable
        ),
        "direct_content_count": sum(
            node.projection_role == "DIRECT_CONTENT" for node in reviewable
        ),
        "whole_table_count": sum(
            node.projection_role == "WHOLE_TABLE_REVIEW"
            for node in reviewable
        ),
        "table_record_count": sum(
            node.projection_role == "TABLE_RECORD" for node in reviewable
        ),
        "semantic_requirement_count": sum(
            node.projection_role == "REQUIREMENT" for node in reviewable
        ),
    }
    if has_bundle and snapshot_entry_ids != stored_ids:
        output.update(
            {
                **counts,
                "requirement_tree_state": "INVALID",
                "requirement_tree_has_evidence_bundle": True,
                "requirement_tree_issue": {
                    "code": "REQUIREMENT_TREE_ENTRY_BINDING_MISMATCH",
                },
            }
        )
        return output
    output.update(
        {
            **counts,
            "requirement_tree_state": "READY",
            "requirement_tree_issue": None,
            "requirement_tree_has_evidence_bundle": has_bundle,
            "tree_lock_ready": has_bundle,
            "_business_key": _working_tree_publication_key(snapshot),
        }
    )
    return output

def _activate_document_requirement_set(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    payload: ActivateRequirementSetRequest | None = None,
) -> dict:
    from app.application.projects import _require_project

    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document.get("project_id") != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )
    target = db.get_requirement_set(requirement_set_id)
    if (
        not target
        or target.get("project_id") != project_id
        or target.get("sor_document_id") != document_id
    ):
        raise ApplicationError(
            status_code=404, detail="Requirement set not found for this SOR document."
        )
    publication_key = str(target.get("publication_key") or "").strip()
    publication_state = str(
        target.get("publication_state") or ""
    ).strip().upper()
    if publication_state == "PUBLISHING":
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_publication_in_progress",
                "message": (
                    "确认清单版本仍在生成中，请稍后重试；"
                    "系统尚未激活该版本。"
                ),
            },
        )
    if publication_key and publication_state != "READY":
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_publication_not_ready",
                "message": "确认清单发布状态尚未就绪，不能设为当前版本。",
            },
        )
    if publication_key and (
        not str(target.get("requirement_tree_object_key") or "").strip()
        or not str(target.get("requirement_tree_sha256") or "").strip()
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_tree_not_persisted",
                "message": "确认清单缺少已持久化的可信需求树，不能激活。",
            },
        )
    from app.application.extraction.requirement_tree_snapshots import (
        RequirementTreeSnapshotError,
        load_requirement_set_tree_snapshot,
    )
    from app.domain.extraction.requirement_tree import RequirementTreeState

    try:
        target_tree = load_requirement_set_tree_snapshot(target)
    except RequirementTreeSnapshotError as exc:
        raise ApplicationError(
            status_code=409,
            detail=(
                "此版本的需求结构树无法验证，不能设为当前版本。"
                "既有数据和导出仍保留；请重新提取并生成新版本。"
            ),
        ) from exc
    if target_tree.state is not RequirementTreeState.READY:
        raise ApplicationError(
            status_code=409,
            detail=(
                "此历史版本缺少可信需求结构树，不能设为当前版本。"
                "请重新提取并生成新版本。"
            ),
        )
    exact_working_tree_publication = False
    if publication_key:
        from app.application.extraction.working_tree_publication import (
            _working_tree_publication_key,
        )

        exact_working_tree_publication = publication_key == (
            _working_tree_publication_key(
                target_tree,
                entry_classification_snapshot_id=str(
                    target.get("entry_classification_snapshot_id") or ""
                ).strip(),
                entry_classification_snapshot_sha256=str(
                    target.get("entry_classification_snapshot_sha256") or ""
                ).strip(),
            )
        )
    impact_receipt = None
    source_requirement_set_id = str(target.get("supersedes_id") or "").strip()
    target_is_inactive_draft = (
        str(target.get("status") or "").upper() == "DRAFT"
        and not bool(int(target.get("is_active") or 0))
    )
    target_is_editable_derivation = bool(
        target_is_inactive_draft
        and source_requirement_set_id
        and not exact_working_tree_publication
        and not str(
            target.get("enrichment_publication_binding_id") or ""
        ).strip()
    )
    target_requires_source_cas = bool(
        source_requirement_set_id
        and (
            target_is_editable_derivation
            or (
                target_is_inactive_draft
                and exact_working_tree_publication
            )
        )
    )
    if target_requires_source_cas:
        if (
            str(document.get("active_requirement_set_id") or "")
            != source_requirement_set_id
        ):
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "requirement_set_activation_source_changed",
                    "message": "当前需求版本已变化，请刷新后重新生成确认清单。",
                },
            )
    if target_is_editable_derivation:
        source_requirement_set = db.get_requirement_set(
            source_requirement_set_id
        )
        if str(
            (source_requirement_set or {}).get("requirement_tree_sha256") or ""
        ).strip():
            from app.application.requirement_set_impact_service import (
                reconcile_requirement_set_impact_for_draft,
            )

            impact_receipt = reconcile_requirement_set_impact_for_draft(
                project_id=project_id,
                document_id=document_id,
                requirement_set_id=requirement_set_id,
            )
    from app.adapters.persistence.requirement_set_impacts import (
        RequirementSetImpactPersistenceError,
    )

    try:
        if target_is_editable_derivation and impact_receipt is not None:
            from app.adapters.persistence.requirement_set_impacts import (
                finalize_staged_requirement_set_activation,
                get_requirement_set_impact_receipt,
            )
            from app.application.requirement_set_impact_service import (
                REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION,
                requirement_set_analysis_target_node_ids,
            )

            if payload and (
                payload.expected_enrichment_publication_binding_id
                or payload.expected_enrichment_publication_binding_sha256
            ):
                raise RequirementSetImpactPersistenceError(
                    "Editable derivation activation cannot use an enrichment binding."
                )
            stored_receipt = get_requirement_set_impact_receipt(
                str(impact_receipt.get("receipt_id") or "")
            )
            if stored_receipt is None:
                raise RequirementSetImpactPersistenceError(
                    "Editable derivation impact receipt is not durable."
                )
            activated = finalize_staged_requirement_set_activation(
                stored_receipt,
                analysis_input_schema_version=(
                    REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
                ),
                workflow_stage="REVIEWING",
                expected_active_requirement_set_id=(
                    payload.expected_active_requirement_set_id
                    if payload and payload.expected_active_requirement_set_id
                    else source_requirement_set_id
                ),
                analysis_target_node_ids=(
                    requirement_set_analysis_target_node_ids(
                        project_id=project_id,
                        document_id=document_id,
                        requirement_set_id=requirement_set_id,
                    )
                ),
            )
        else:
            activated = db.activate_requirement_set(
                project_id,
                document_id,
                requirement_set_id,
                workflow_stage="EXTRACTION_LOCKED"
                if str(target.get("status") or "").upper() == "LOCKED"
                else "REVIEWING",
                expected_active_requirement_set_id=(
                    payload.expected_active_requirement_set_id
                    if payload and payload.expected_active_requirement_set_id
                    else (
                        source_requirement_set_id
                        if target_requires_source_cas
                        else ""
                    )
                ),
                expected_enrichment_publication_binding_id=(
                    payload.expected_enrichment_publication_binding_id
                    if payload
                    else ""
                ),
                expected_enrichment_publication_binding_sha256=(
                    payload.expected_enrichment_publication_binding_sha256
                    if payload
                    else ""
                ),
            )
    except (
        db.RequirementSetMutationBlocked,
        RequirementSetImpactPersistenceError,
    ) as exc:
        raise ApplicationError(status_code=409, detail=str(exc)) from exc
    if not activated:
        raise ApplicationError(
            status_code=409, detail="Requirement set could not be activated."
        )
    return {
        "project_id": project_id,
        "document_id": document_id,
        "active_requirement_set": activated,
        "document": db.get_sor_document(document_id),
        "impact_receipt": impact_receipt,
    }

def activate_document_requirement_set(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    payload: ActivateRequirementSetRequest | None = None,
) -> dict:
    return _activate_document_requirement_set(
        project_id,
        document_id,
        requirement_set_id,
        payload,
    )

def restore_document_requirement_set(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    payload: ActivateRequirementSetRequest | None = None,
) -> dict:
    return _activate_document_requirement_set(
        project_id,
        document_id,
        requirement_set_id,
        payload,
    )

def derive_document_requirement_set(
    project_id: str, document_id: str, requirement_set_id: str
) -> dict:
    from app.application.projects import _require_project

    _require_project(project_id)
    document = db.get_sor_document(document_id)
    target = db.get_requirement_set(requirement_set_id)
    if (
        not document
        or document.get("project_id") != project_id
        or not target
        or target.get("project_id") != project_id
        or target.get("sor_document_id") != document_id
    ):
        raise ApplicationError(
            status_code=404, detail="Requirement set not found for this SOR document."
        )
    if str(document.get("active_requirement_set_id") or "") != requirement_set_id:
        raise ApplicationError(
            status_code=409,
            detail="Activate the requirement set before deriving a draft.",
        )
    if str(target.get("status") or "").upper() != "LOCKED":
        return {
            "project_id": project_id,
            "document_id": document_id,
            "requirement_set": target,
            "created": False,
            "impact_receipt": None,
        }
    try:
        draft = _fork_requirement_set_for_edit_with_tree(project_id, document_id)
    except db.RequirementSetMutationBlocked as exc:
        raise ApplicationError(status_code=409, detail=str(exc)) from exc
    if not draft:
        raise ApplicationError(
            status_code=409,
            detail="Unable to derive an editable requirement-set draft.",
        )
    impact_receipt = draft.pop("_impact_receipt", None)
    return {
        "project_id": project_id,
        "document_id": document_id,
        "requirement_set": draft,
        "created": True,
        "impact_receipt": impact_receipt,
        "affected_result_count": len(
            (impact_receipt or {}).get("stale_result_node_ids") or []
        ),
    }


def _fork_requirement_set_for_edit_with_tree(
    project_id: str,
    document_id: str,
) -> dict[str, Any]:
    """Derive an editable set without ever dropping its immutable tree."""

    from app.application.extraction.requirement_tree_snapshots import (
        RequirementTreeSnapshotError,
        clone_requirement_set_tree_snapshot,
        delete_unowned_requirement_set_tree_object,
        load_requirement_set_tree_snapshot,
    )
    from app.domain.extraction.requirement_tree import RequirementTreeState

    source_set = db.get_active_requirement_set(document_id) or {}
    if (
        str(source_set.get("project_id") or "") != project_id
        or str(source_set.get("sor_document_id") or "") != document_id
    ):
        raise ApplicationError(
            status_code=409,
            detail="No active Requirement Set is available to derive.",
        )
    try:
        source_tree = load_requirement_set_tree_snapshot(source_set)
    except RequirementTreeSnapshotError as exc:
        raise ApplicationError(
            status_code=409,
            detail=(
                "The active Requirement Set has no verifiable requirement tree. "
                "Re-extract the document before deriving a new version."
            ),
        ) from exc
    if source_tree.state is not RequirementTreeState.READY:
        raise ApplicationError(
            status_code=409,
            detail=(
                "The active historical version cannot be safely derived because "
                "its structure lineage is unproven. Re-extract a new version."
            ),
        )

    verified_source_publication_basis_key = ""
    classification_snapshot_id = str(
        source_set.get("entry_classification_snapshot_id") or ""
    ).strip()
    classification_snapshot_sha256 = str(
        source_set.get("entry_classification_snapshot_sha256") or ""
    ).strip()
    enrichment_binding_id = str(
        source_set.get("enrichment_publication_binding_id") or ""
    ).strip()
    enrichment_binding_sha256 = str(
        source_set.get("enrichment_publication_binding_sha256") or ""
    ).strip()
    if enrichment_binding_id:
        from app.application.runtime import RUNTIME_STATE

        container = RUNTIME_STATE.container
        if container is None:
            raise ApplicationError(
                status_code=409,
                detail=(
                    "The active Requirement Set enrichment lineage cannot be "
                    "verified in the current runtime."
                ),
            )
        repository = container.requirement_enrichment_repository
        try:
            binding = repository.get_publication_binding_v2(
                requirement_set_id=str(source_set.get("id") or ""),
                binding_id=enrichment_binding_id,
            )
            preview = (
                repository.get_publication_binding_preview_v2(
                    project_id=project_id,
                    document_id=document_id,
                    session_id=binding.session_id,
                    binding_preview_id=binding.binding_preview_id,
                )
                if binding is not None
                else None
            )
        except (AttributeError, RuntimeError, ValueError) as exc:
            raise ApplicationError(
                status_code=409,
                detail=(
                    "The active Requirement Set enrichment lineage changed "
                    "before the editable version was derived."
                ),
            ) from exc
        if (
            binding is None
            or preview is None
            or binding.payload_sha256 != enrichment_binding_sha256
            or preview.payload_sha256 != binding.binding_preview_sha256
        ):
            raise ApplicationError(
                status_code=409,
                detail=(
                    "The active Requirement Set enrichment lineage is no longer "
                    "verifiable."
                ),
            )
        classification_snapshot_id = (
            preview.projection.entry_classification_snapshot_id
        )
        classification_snapshot_sha256 = (
            preview.projection.entry_classification_sha256
        )

    if (
        str(source_set.get("publication_key") or "").strip()
        or enrichment_binding_id
    ):
        from app.application.extraction.working_tree_publication import (
            _working_tree_publication_key,
        )

        verified_source_publication_basis_key = _working_tree_publication_key(
            source_tree,
            entry_classification_snapshot_id=classification_snapshot_id,
            entry_classification_snapshot_sha256=(
                classification_snapshot_sha256
            ),
        )

    draft, requirement_id_mapping = db.fork_requirement_set_for_edit_with_mapping(
        project_id,
        document_id,
        expected_source_requirement_set_id=str(source_set.get("id") or ""),
        verified_source_publication_basis_key=(
            verified_source_publication_basis_key
        ),
        expected_source_tree_sha256=str(
            source_set.get("requirement_tree_sha256") or ""
        ),
        expected_source_enrichment_binding_id=enrichment_binding_id,
        expected_source_enrichment_binding_sha256=enrichment_binding_sha256,
        derived_classification_snapshot_id=classification_snapshot_id,
        derived_classification_snapshot_sha256=(
            classification_snapshot_sha256
        ),
        activate=False,
    )
    if not draft:
        return {}
    try:
        clone_requirement_set_tree_snapshot(
            source_requirement_set=source_set,
            target_requirement_set=draft,
            requirement_id_mapping=requirement_id_mapping,
        )
        from app.adapters.persistence.requirement_matrix_states import (
            clone_requirement_matrix_node_states,
        )

        clone_requirement_matrix_node_states(
            project_id=project_id,
            document_id=document_id,
            source_requirement_set_id=str(source_set.get("id") or ""),
            target_requirement_set_id=str(draft.get("id") or ""),
            allowed_node_ids={node.id for node in source_tree.nodes},
        )
        from app.adapters.persistence.requirement_set_impacts import (
            apply_requirement_set_impact_receipt,
            finalize_staged_requirement_set_activation,
        )
        from app.application.requirement_set_impact_service import (
            REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION,
            build_requirement_set_impact_receipt,
            requirement_set_analysis_target_node_ids,
        )

        impact_receipt = build_requirement_set_impact_receipt(
            project_id=project_id,
            document_id=document_id,
            old_requirement_set_id=str(source_set.get("id") or ""),
            new_requirement_set_id=str(draft.get("id") or ""),
        )
        impact_result = apply_requirement_set_impact_receipt(
            impact_receipt,
            analysis_input_schema_version=(
                REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
            ),
        )
        activated = finalize_staged_requirement_set_activation(
            impact_result.receipt,
            analysis_input_schema_version=(
                REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
            ),
            workflow_stage="REVIEWING",
            expected_active_requirement_set_id=str(source_set.get("id") or ""),
            analysis_target_node_ids=requirement_set_analysis_target_node_ids(
                project_id=project_id,
                document_id=document_id,
                requirement_set_id=str(draft.get("id") or ""),
            ),
        )
        if str(activated.get("id") or "") != str(draft.get("id") or ""):
            raise db.RequirementSetMutationBlocked(
                "The staged Requirement Set could not be activated."
            )
    except Exception as exc:
        # This is the staging compensation boundary, not an error classifier.
        # Any ordinary failure after the inactive DRAFT is created must attempt
        # the same guarded cleanup; otherwise an unexpected adapter/driver
        # exception can strand a durable staging version indefinitely.
        draft_id = str(draft.get("id") or "")
        discarded = db.discard_inactive_requirement_set(
            document_id,
            draft_id,
            project_id=project_id,
        )
        if discarded:
            delete_unowned_requirement_set_tree_object(
                project_id=project_id,
                document_id=document_id,
                requirement_set_id=draft_id,
            )
        raise ApplicationError(
            status_code=500 if discarded else 409,
            detail=(
                "The editable version was not activated because its frozen tree "
                "or impact receipt could not be verified."
            ),
        ) from exc
    refreshed = db.get_requirement_set(str(draft.get("id") or "")) or draft
    refreshed["_impact_receipt"] = {
        **impact_result.receipt.model_dump(mode="json"),
        "created": impact_result.created,
        "applied": impact_result.applied,
        "retained_result_count": impact_result.retained_count,
        "stale_result_count": impact_result.stale_count,
        "cleared_result_count": impact_result.cleared_count,
    }
    return refreshed

def get_document_workspace(
    project_id: str,
    document_id: str,
    include_preview: bool = False,
    requirement_set_id: str = "",
) -> dict:
    from app.application.knowledge import _list_project_knowledge_documents
    from app.application.projects import _require_project
    from app.application.runtime import _runtime_record_path

    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )
    document = db.backfill_sor_document_runtime(document_id) or document
    selected_set_id = str(requirement_set_id or "").strip()
    selected_set = None
    workspace_document = dict(document)
    if selected_set_id:
        selected_set = db.get_requirement_set(selected_set_id)
        if (
            selected_set is None
            or str(selected_set.get("project_id") or "") != project_id
            or str(selected_set.get("sor_document_id") or "") != document_id
        ):
            raise ApplicationError(
                status_code=404,
                detail="Requirement set not found for this SOR document.",
            )
        workspace_document.update(
            {
                "requirement_set_version": int(selected_set.get("version") or 0),
                "extraction_locked_at": str(selected_set.get("locked_at") or ""),
                "preview_generation_id": str(
                    selected_set.get("preview_generation_id") or ""
                ),
                "ai_status": str(selected_set.get("latest_ai_run_status") or "NOT_RUN"),
                "ai_assessed_at": str(
                    selected_set.get("latest_ai_run_finished_at") or ""
                ),
                "ai_requirement_set_version": (
                    int(selected_set.get("version") or 0)
                    if selected_set.get("latest_ai_run_id")
                    else 0
                ),
            }
        )
    requirements = db.list_requirements(
        project_id,
        document_id,
        requirement_set_id=selected_set_id or None,
    )
    requirements = _enrich_requirements_with_preview_context(
        requirements, str(_runtime_record_path(workspace_document))
    )
    knowledge_docs = _list_project_knowledge_documents(project_id)
    preview, preview_meta = load_cached_preview(workspace_document)
    active_set_id = str(document.get("active_requirement_set_id") or "").strip()
    selected_set_is_active = bool(selected_set_id and selected_set_id == active_set_id)
    if selected_set_id and not selected_set_is_active:
        # A non-active Requirement Set is a self-contained historical/DRAFT
        # projection. Never overlay it with the active document preview.
        preview = None
    workspace = _build_document_workspace(
        project=project,
        document=workspace_document,
        requirements=requirements,
        knowledge_docs_count=len(knowledge_docs),
        preview=preview,
        preview_meta=preview_meta,
    )
    if selected_set is not None:
        read_only_reason = (
            None
            if selected_set_is_active
            else "NON_ACTIVE_REQUIREMENT_SET_MUST_BE_ACTIVATED"
        )
        workspace["selected_requirement_set"] = {
            **selected_set,
            "is_active": selected_set_is_active,
        }
        workspace["read_only_reason"] = read_only_reason
        workspace_document_payload = workspace.get("document")
        if isinstance(workspace_document_payload, dict):
            workspace_document_payload.update(
                {
                    "requirement_set_id": selected_set_id,
                    "requirement_set_is_active": selected_set_is_active,
                    "read_only_reason": read_only_reason,
                }
            )
        if not selected_set_is_active:
            actions = workspace.get("actions")
            if isinstance(actions, dict):
                actions.update(
                    {
                        "can_lock_extraction": False,
                        "can_export": False,
                        "can_export_draft": False,
                        "can_export_formal": False,
                        "can_run_ai_analysis": False,
                    }
                )
    return workspace if include_preview else _strip_workspace_preview(workspace)

def lock_document_extraction(
    project_id: str,
    document_id: str,
    payload: LockExtractionRequest,
) -> dict:
    from app.application.knowledge import _list_project_knowledge_documents
    from app.application.projects import _require_project
    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )
    document = db.backfill_sor_document_runtime(document_id) or document
    active_requirement_set_id = str(
        document.get("active_requirement_set_id") or ""
    ).strip()
    requested_requirement_set_id = str(payload.requirement_set_id).strip()
    if not requested_requirement_set_id:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "requirement_set_id_required",
                "message": "锁定前必须明确选择一个需求版本。",
            },
        )
    if (
        requested_requirement_set_id
        and requested_requirement_set_id != active_requirement_set_id
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_selection_changed",
                "message": (
                    "当前选中的需求版本已变化，请刷新后再锁定。"
                ),
                "selected_requirement_set_id": requested_requirement_set_id,
                "active_requirement_set_id": active_requirement_set_id,
            },
        )
    active_requirement_set = db.get_requirement_set(active_requirement_set_id) or {}
    if (
        str(active_requirement_set.get("status") or "").upper() == "DRAFT"
        and str(active_requirement_set.get("supersedes_id") or "").strip()
        and str(active_requirement_set.get("requirement_tree_sha256") or "").strip()
    ):
        # A disposition write and its affected-only projection are deliberately
        # separate commits.  Reconcile once more before computing the strict
        # readiness token so a failed/interrupted PATCH can never be locked as
        # though its carried-result projection were current.
        from app.application.requirement_set_impact_service import (
            reconcile_requirement_set_impact_for_draft,
        )

        reconcile_requirement_set_impact_for_draft(
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=active_requirement_set_id,
        )
    lock_readiness, matrix = _build_requirement_set_lock_readiness(
        project_id,
        document_id,
        requested_requirement_set_id,
        document=document,
    )
    strict_lock = bool(payload.schema_version)
    if (
        strict_lock
        and payload.expected_readiness_sha256
        != lock_readiness["readiness_sha256"]
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_lock_readiness_changed",
                "message": "锁定就绪性已变化，请刷新确认范围后重试。",
                "expected_readiness_sha256": payload.expected_readiness_sha256,
                "actual_readiness_sha256": lock_readiness["readiness_sha256"],
                "readiness": lock_readiness,
            },
        )
    if not lock_readiness["can_lock"]:
        blocker_codes = {
            str(item.get("code") or "")
            for item in lock_readiness["hard_blockers"]
        }
        if "REQUIREMENT_MATRIX_REQUIRES_REEXTRACTION" in blocker_codes:
            code = "matrix_requires_reextraction"
            message = "当前提取运行时已升级，请重新提取并生成新的需求版本。"
        elif "REQUIREMENT_MATRIX_COUNT_MISMATCH" in blocker_codes:
            code = "requirement_matrix_counts_invalid"
            message = "需求矩阵计数与冻结分析范围不一致，请刷新后重试。"
        else:
            code = "requirement_set_not_lockable"
            message = "当前需求版本不能锁定，请刷新后重试。"
        raise ApplicationError(
            status_code=409,
            detail={
                "code": code,
                "message": message,
                "readiness": _legacy_lock_readiness(lock_readiness),
            },
        )

    matrix_state = matrix.state.value
    analysis_target_count = int(
        lock_readiness["counts"]["analysis_targets"]
    )
    matrix_lock_readiness = _legacy_lock_readiness(lock_readiness)

    # Locking must not refresh or rewrite frozen version content from today's
    # preview cache.  The response may project the stored rows, while the Matrix
    # remains the sole checklist and content authority.
    requirements = db.list_requirements(
        project_id,
        document_id,
        requirement_set_id=requested_requirement_set_id,
    )
    knowledge_docs = _list_project_knowledge_documents(project_id)
    waiver_reason = str(payload.waiver_reason).strip()
    try:
        locked = db.lock_sor_document_extraction(
            document_id,
            waiver_reason=waiver_reason,
            expected_requirement_set_id=active_requirement_set_id,
            expected_matrix_mutable_state_sha256=str(
                lock_readiness.get("mutable_state_sha256") or ""
            ),
        )
    except db.RequirementSetMutationBlocked as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_lock_readiness_changed",
                "message": "需求清单在锁定前已变化，请重新读取摘要后确认。",
            },
        ) from exc
    preview, preview_meta = load_cached_preview(locked)
    workspace = _build_document_workspace(
        project=project,
        document=locked,
        requirements=requirements,
        knowledge_docs_count=len(knowledge_docs),
        preview=preview,
        preview_meta=preview_meta,
    )
    locked_requirement_set = db.get_requirement_set(
        active_requirement_set_id
    ) or {}
    response_payload = _strip_workspace_preview(workspace)
    response_readiness = response_payload.get("readiness")
    if isinstance(response_readiness, dict):
        response_readiness["lock"] = matrix_lock_readiness
    response_summary = response_payload.get("summary")
    if isinstance(response_summary, dict):
        response_summary.update(
            {
                "lock_basis": "REQUIREMENT_MATRIX_SNAPSHOT_V1",
                "matrix_state": matrix_state,
                "matrix_total_node_count": matrix.counts.total_nodes,
                "matrix_visible_node_count": matrix.counts.visible_nodes,
                "matrix_analysis_target_count": analysis_target_count,
            }
        )
    response_payload.update(
        {
            "locked_requirement_set_id": active_requirement_set_id,
            "locked_requirement_set_version": int(
                locked_requirement_set.get("version") or 0
            ),
            "locked_review_candidate_count": matrix.counts.total_nodes,
            "locked_requirement_count": analysis_target_count,
            "locked_reference_count": matrix.counts.retained_nodes,
            "locked_excluded_count": matrix.counts.excluded_nodes,
            "locked_visible_count": matrix.counts.visible_nodes,
            "locked_matrix_counts": matrix.counts.model_dump(mode="json"),
            "locked_count_semantics": "REQUIREMENT_MATRIX_SNAPSHOT_V1",
            "lock_readiness": lock_readiness,
            "readiness_sha256": lock_readiness["readiness_sha256"],
            "next_destination": lock_readiness["next_destination"],
            "analysis_available": analysis_target_count > 0,
        }
    )
    return response_payload


def get_document_lock_readiness(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> dict[str, Any]:
    """Return the canonical, side-effect-free lock preview for one set."""

    from app.application.projects import _require_project

    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document.get("project_id") != project_id:
        raise ApplicationError(
            status_code=404,
            detail="SOR document not found for this project.",
        )
    readiness, _ = _build_requirement_set_lock_readiness(
        project_id,
        document_id,
        str(requirement_set_id or "").strip(),
        document=document,
    )
    return readiness


def _build_requirement_set_lock_readiness(
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    *,
    document: dict[str, Any] | None = None,
):
    from app.adapters.persistence.requirement_matrix_states import (
        requirement_matrix_mutable_state_sha256,
    )
    from app.application.extraction.requirement_tree_snapshots import (
        RequirementTreeSnapshotError,
        load_requirement_set_tree_snapshot,
    )
    from app.application.requirement_matrix import build_requirement_matrix_snapshot
    from app.domain.requirement_matrix import (
        MatrixNodeDisposition,
        MatrixNodeKind,
        MatrixNodeVisibility,
        MatrixSnapshotState,
        is_analysis_target,
    )

    requested_set_id = str(requirement_set_id or "").strip()
    if not requested_set_id:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "requirement_set_id_required",
                "message": "锁定前必须明确选择一个需求版本。",
            },
        )
    current_document = document or db.get_sor_document(document_id)
    requirement_set = db.get_requirement_set(requested_set_id)
    if (
        current_document is None
        or current_document.get("project_id") != project_id
        or requirement_set is None
        or requirement_set.get("project_id") != project_id
        or requirement_set.get("sor_document_id") != document_id
    ):
        raise ApplicationError(
            status_code=404,
            detail="Requirement set not found for this SOR document.",
        )

    mutable_state_sha256 = requirement_matrix_mutable_state_sha256(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=requested_set_id,
    )
    matrix = build_requirement_matrix_snapshot(
        project_id,
        document_id,
        requested_set_id,
        require_current_runtime=True,
    )
    if requirement_matrix_mutable_state_sha256(
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=requested_set_id,
    ) != mutable_state_sha256:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_lock_readiness_changed",
                "message": "需求清单在生成锁定摘要期间已变化，请重新读取摘要。",
            },
        )
    hard_blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    active_set_id = str(
        current_document.get("active_requirement_set_id") or ""
    ).strip()
    if active_set_id != requested_set_id or not bool(
        int(requirement_set.get("is_active") or 0)
    ):
        hard_blockers.append(
            {
                "code": "REQUIREMENT_SET_SELECTION_CHANGED",
                "selected_requirement_set_id": requested_set_id,
                "active_requirement_set_id": active_set_id,
                "hard": True,
            }
        )

    category_by_source_id: dict[str, str] = {}
    classification_tree = None
    classification_metadata_available = False
    if matrix.state is MatrixSnapshotState.READY:
        try:
            classification_tree = load_requirement_set_tree_snapshot(
                requirement_set
            )
        except RequirementTreeSnapshotError:
            classification_tree = None
        classification_metadata_available = bool(
            classification_tree is not None
            and str(
                requirement_set.get("entry_classification_snapshot_id") or ""
            ).strip()
            and str(
                requirement_set.get("entry_classification_snapshot_sha256")
                or ""
            ).strip()
        )
        if classification_tree is not None:
            for node in classification_tree.nodes:
                classification = str(node.classification or "").strip().upper()
                if classification not in {
                    "TECHNICAL",
                    "NON_TECHNICAL",
                    "INFORMATION",
                }:
                    continue
                source_id = str(node.workflow_node_id or node.id)
                prior = category_by_source_id.setdefault(source_id, classification)
                if prior != classification:
                    warnings.append(
                        {
                            "code": "SOURCE_CLASSIFICATION_CONFLICT",
                            "source_entry_id": source_id,
                            "hard": False,
                        }
                    )

    business_nodes = tuple(
        node
        for node in matrix.nodes
        if node.matrix_visibility is not MatrixNodeVisibility.SYSTEM_NON_BUSINESS
    )
    analysis_targets = tuple(
        node
        for node in matrix.nodes
        if is_analysis_target(node)
    )
    counts = {
        "visible_content": len(business_nodes),
        "analysis_targets": len(analysis_targets),
        "retained_visible": sum(
            node.disposition is MatrixNodeDisposition.RETAIN
            for node in business_nodes
        ),
        "excluded_visible": sum(
            node.disposition is MatrixNodeDisposition.EXCLUDE
            for node in business_nodes
        ),
        "skipped_visible": len(business_nodes) - len(analysis_targets),
        "technical_source_entries": sum(
            value == "TECHNICAL" for value in category_by_source_id.values()
        ),
        "non_technical_source_entries": sum(
            value == "NON_TECHNICAL" for value in category_by_source_id.values()
        ),
        "information_source_entries": sum(
            value == "INFORMATION" for value in category_by_source_id.values()
        ),
        "source_entries": len(category_by_source_id),
        "atomic_requirements": sum(
            node.display_role is MatrixNodeKind.ATOMIC_REQUIREMENT
            for node in business_nodes
        ),
        "stale_analysis_results": sum(
            node.analysis_result_state.value == "STALE_REFERENCE"
            for node in analysis_targets
        ),
    }
    count_metadata = {
        "content_classification": (
            "AVAILABLE"
            if classification_metadata_available
            else "LEGACY_UNAVAILABLE"
        ),
        "entry_composition": (
            "AVAILABLE"
            if classification_metadata_available
            else "LEGACY_UNAVAILABLE"
        ),
        "source": (
            "FROZEN_TREE"
            if classification_metadata_available
            else "LEGACY_MATRIX"
        ),
    }
    if matrix.state is not MatrixSnapshotState.READY:
        hard_blockers.append(
            {
                "code": "REQUIREMENT_MATRIX_REQUIRES_REEXTRACTION",
                "matrix_state": matrix.state.value,
                "hard": True,
            }
        )
    elif matrix.counts.analyze_nodes != len(analysis_targets):
        hard_blockers.append(
            {
                "code": "REQUIREMENT_MATRIX_COUNT_MISMATCH",
                "matrix_analyze_nodes": matrix.counts.analyze_nodes,
                "actual_analysis_target_count": len(analysis_targets),
                "hard": True,
            }
        )
    stale_analysis_result_count = counts["stale_analysis_results"]
    if stale_analysis_result_count:
        warnings.append(
            {
                "code": "STALE_ANALYSIS_RESULTS_REQUIRE_RERUN",
                "count": stale_analysis_result_count,
                "message": (
                    f"有 {stale_analysis_result_count} 条历史分析结果已失效，"
                    "锁定后需要重新分析。"
                ),
                "hard": False,
            }
        )

    canonical_basis = {
        "schema_version": "requirement-set-lock-readiness-basis/v1",
        "requirement_set_id": requested_set_id,
        "tree_sha256": str(matrix.tree_sha256 or ""),
        "mutable_state_sha256": mutable_state_sha256,
        "nodes": [
            {
                "node_id": node.node_id,
                "disposition": node.disposition.value,
                "save_revision": node.save_revision,
                "analysis_eligibility": node.analysis_eligibility.value,
                "matrix_visibility": node.matrix_visibility.value,
                "analysis_result_state": node.analysis_result_state.value,
            }
            for node in sorted(matrix.nodes, key=lambda item: item.node_id)
        ],
    }
    readiness_sha256 = hashlib.sha256(
        json.dumps(
            canonical_basis,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    status = str(requirement_set.get("status") or "").strip().upper()
    return (
        {
            "schema_version": "requirement-set-lock-readiness/v1",
            "project_id": project_id,
            "document_id": document_id,
            "requirement_set_id": requested_set_id,
            "tree_sha256": str(matrix.tree_sha256 or ""),
            "matrix_state": matrix.state.value,
            "readiness_sha256": readiness_sha256,
            "mutable_state_sha256": mutable_state_sha256,
            "can_lock": not hard_blockers and status == "DRAFT",
            "next_destination": (
                "ANALYSIS" if counts["analysis_targets"] > 0 else "MATRIX"
            ),
            "counts": counts,
            "count_metadata": count_metadata,
            "hard_blockers": hard_blockers,
            "warnings": warnings,
        },
        matrix,
    )


def _legacy_lock_readiness(readiness: dict[str, Any]) -> dict[str, Any]:
    hard_blockers = list(readiness.get("hard_blockers") or [])
    return {
        "ready": bool(readiness.get("can_lock")),
        "blockers": hard_blockers,
        "hard_blockers": hard_blockers,
        "warnings": list(readiness.get("warnings") or []),
        "requires_waiver_reason": bool(readiness.get("warnings")),
        "basis": "REQUIREMENT_MATRIX_SNAPSHOT_V1",
        "matrix_state": str(readiness.get("matrix_state") or ""),
        "requirement_set_id": str(readiness.get("requirement_set_id") or ""),
        "analysis_target_count": int(
            (readiness.get("counts") or {}).get("analysis_targets") or 0
        ),
        "readiness_sha256": str(readiness.get("readiness_sha256") or ""),
        "next_destination": str(readiness.get("next_destination") or "MATRIX"),
    }

def apply_reviewed_extraction(
    project_id: str, document_id: str, payload: ApplyReviewedExtractionRequest
) -> dict:
    from app.application.knowledge import _list_project_knowledge_documents
    from app.application.projects import _require_project

    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )

    reviewed_rows = _build_analysis_rows_from_review_payload(payload.entries)
    if not reviewed_rows:
        raise ApplicationError(
            status_code=422, detail="No reviewed requirement rows to apply."
        )

    inserted = _persist_requirements(
        project_id=project_id,
        sor_document_id=document_id,
        rows=reviewed_rows,
        knowledge_docs=[],
        dedupe_version=_detect_dedupe_version(reviewed_rows),
        workflow_stage="REVIEWING",
    )
    document_state = db.get_sor_document(document_id) or document
    knowledge_docs = _list_project_knowledge_documents(project_id)
    return {
        "project_id": project_id,
        "document_id": document_id,
        "requirements_generated": inserted,
        "knowledge_docs_used": 0,
        "knowledge_docs_available": len(knowledge_docs),
        "knowledge_base_version": int(project.get("knowledge_base_version") or 0),
        "workflow_stage": str(document_state.get("workflow_stage") or "REVIEWING"),
        "requirement_set_version": int(
            document_state.get("requirement_set_version") or 0
        ),
        "ai_status": str(document_state.get("ai_status") or "NOT_RUN"),
        "ai_triggered": False,
        "dedupe_version": _detect_dedupe_version(reviewed_rows),
    }

def save_review_entry(
    project_id: str, document_id: str, payload: ReviewedRequirementEntry
) -> dict:
    from app.application.ingestion import _normalize_preview_generation_id
    from app.application.projects import _require_project
    from app.application.runtime import _runtime_record_path

    project = _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )

    requested_set_id = str(payload.requirement_set_id or "").strip()
    active_set_id = str(document.get("active_requirement_set_id") or "").strip()
    if not requested_set_id:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "requirement_set_id_required",
                "message": "保存需求处置时必须明确指定当前需求版本。",
            },
        )
    if requested_set_id != active_set_id:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "requirement_set_selection_changed",
                "message": "当前审阅版本已变化，请刷新后再保存。",
                "selected_requirement_set_id": requested_set_id,
                "active_requirement_set_id": active_set_id,
            },
        )
    current_requirements = db.list_requirements(
        project_id,
        document_id,
        requirement_set_id=requested_set_id,
    )
    normalized = _normalize_review_entry_payload(payload)
    complex_table_row = is_complex_table_row(normalized)
    if complex_table_row:
        normalized = normalize_preserved_entry_table_assets(normalized)
    matched = _find_requirement_for_review_entry(current_requirements, normalized)
    if matched and not normalized["keep"]:
        matched_set = db.get_requirement_set(
            str(matched.get("requirement_set_id") or "")
        )
        if matched_set and str(
            matched_set.get("requirement_tree_object_key") or ""
        ).strip():
            # A published RequirementTree snapshot owns this stable entry ID.
            # Exclusion is a downstream disposition, not physical deletion.
            normalized["keep"] = True
            normalized["entry_kind"] = "EXCLUDED"
    if normalized["keep"] or matched:
        active_set = db.get_active_requirement_set(document_id)
        if active_set and str(active_set.get("status") or "").upper() == "LOCKED":
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "requirement_set_locked",
                    "message": (
                        "该需求版本已锁定，不能修改需求内容或处置。"
                        "请从需求提取生成新的审阅版本。"
                    ),
                },
            )
    if not complex_table_row:
        active_set_id = str(document.get("active_requirement_set_id") or "").strip()
        normalized, _ = enrich_entry_with_table_assets(
            normalized,
            document_id=document_id,
            snapshot_root=_table_snapshot_root(document_id, active_set_id),
            source_file_path=str(_runtime_record_path(document)),
        )
        matched = (
            _find_requirement_for_review_entry(current_requirements, normalized)
            or matched
        )
    action = "noop"
    persisted_row: dict | None = matched

    if normalized["keep"]:
        if matched:
            updated_row = db.update_requirement_entry(
                str(matched.get("id") or ""),
                source_page=normalized.get("source_page"),
                source_anchor=str(normalized.get("source_anchor") or "Unknown"),
                requirement_text=str(normalized.get("display_text") or ""),
                requirement_type=str(
                    normalized.get("requirement_type") or "FUNCTIONAL"
                ),
                priority=str(normalized.get("priority") or "MEDIUM"),
                entry_kind=str(normalized.get("entry_kind") or "REQUIREMENT"),
                text_relation=str(normalized.get("text_relation") or "distinct"),
                is_deduped=bool(normalized.get("is_deduped")),
                parent_section_code=str(normalized.get("parent_section_code") or ""),
                parent_section_title=str(normalized.get("parent_section_title") or ""),
                parent_section_label=str(normalized.get("parent_section_label") or ""),
                heading_text=str(normalized.get("heading_text") or ""),
                raw_text=str(normalized.get("raw_text") or ""),
                display_text=str(normalized.get("display_text") or ""),
                merged_from_req_codes=_json_dumps_list(
                    normalized.get("merged_from_req_codes")
                ),
                merged_from_entry_ids=_json_dumps_list(
                    normalized.get("merged_from_entry_ids")
                ),
                dedupe_version=str(normalized.get("dedupe_version") or ""),
                table_refs=_json_dumps(normalized.get("table_refs") or []),
                content_blocks=_json_dumps(normalized.get("content_blocks") or []),
                has_table_asset=bool(normalized.get("has_table_asset")),
                table_render_mode=str(normalized.get("table_render_mode") or ""),
                preview_generation_id=_normalize_preview_generation_id(
                    document.get("preview_generation_id")
                ),
            )
            persisted_row = (
                db.get_requirement(str(updated_row.get("id") or "")) or matched
            )
            action = "updated"
        else:
            used_req_codes = {
                str(item.get("req_code") or "").strip()
                for item in current_requirements
                if str(item.get("req_code") or "").strip()
            }
            base_req_code = str(
                normalized.get("req_code") or normalized.get("entry_id") or ""
            ).strip() or (f"REQ{len(current_requirements) + 1:04d}")
            req_code = _ensure_unique_req_code(base_req_code, used_req_codes)
            inserted_row = db.insert_requirement(
                project_id=project_id,
                sor_document_id=document_id,
                req_code=req_code,
                source_page=normalized.get("source_page"),
                source_anchor=str(normalized.get("source_anchor") or "Unknown"),
                requirement_text=str(normalized.get("display_text") or ""),
                requirement_type=str(
                    normalized.get("requirement_type") or "FUNCTIONAL"
                ),
                priority=str(normalized.get("priority") or "MEDIUM"),
                status="DRAFT",
                entry_kind=str(normalized.get("entry_kind") or "REQUIREMENT"),
                text_relation=str(normalized.get("text_relation") or "distinct"),
                is_deduped=bool(normalized.get("is_deduped")),
                parent_section_code=str(normalized.get("parent_section_code") or ""),
                parent_section_title=str(normalized.get("parent_section_title") or ""),
                parent_section_label=str(normalized.get("parent_section_label") or ""),
                heading_text=str(normalized.get("heading_text") or ""),
                raw_text=str(normalized.get("raw_text") or ""),
                display_text=str(normalized.get("display_text") or ""),
                merged_from_req_codes=_json_dumps_list(
                    normalized.get("merged_from_req_codes")
                ),
                merged_from_entry_ids=_json_dumps_list(
                    normalized.get("merged_from_entry_ids")
                ),
                dedupe_version=str(normalized.get("dedupe_version") or ""),
                table_refs=_json_dumps(normalized.get("table_refs") or []),
                content_blocks=_json_dumps(normalized.get("content_blocks") or []),
                has_table_asset=bool(normalized.get("has_table_asset")),
                table_render_mode=str(normalized.get("table_render_mode") or ""),
                preview_generation_id=_normalize_preview_generation_id(
                    document.get("preview_generation_id")
                ),
            )
            persisted_row = (
                db.get_requirement(str(inserted_row.get("id") or "")) or inserted_row
            )
            action = "inserted"
    elif matched:
        db.delete_requirement(str(matched.get("id") or ""))
        persisted_row = matched
        action = "removed"

    if action != "noop":
        document = (
            db.increment_sor_document_requirement_set_version(document_id, "REVIEWING")
            or document
        )
        _sync_document_table_assets(project_id, document_id)
    else:
        document = db.backfill_sor_document_runtime(document_id) or document

    requirements = db.list_requirements(project_id, document_id)
    requirements = _enrich_requirements_with_preview_context(
        requirements, str(_runtime_record_path(document))
    )
    knowledge_docs = db.list_knowledge_documents(project_id)
    preview, preview_meta = load_cached_preview(document)
    workspace = _build_document_workspace(
        project=project,
        document=document,
        requirements=requirements,
        knowledge_docs_count=len(knowledge_docs),
        preview=preview,
        preview_meta=preview_meta,
    )
    response_workspace = _strip_workspace_preview(workspace)
    saved_entry = prepare_table_ref_urls(
        _build_saved_review_entry(normalized, persisted_row, keep=action != "removed"),
        project_id=project_id,
        document_id=document_id,
    )
    return {
        **response_workspace,
        "saved_entry": saved_entry,
        "review_save": {
            "action": action,
            "entry_id": normalized.get("entry_id"),
            "req_code": saved_entry.get("req_code"),
        },
    }

def list_requirements(project_id: str, document_id: str | None = None) -> list[dict]:
    from app.application.projects import _require_project
    from app.application.runtime import _runtime_record_path

    _require_project(project_id)
    document = None
    if document_id:
        document = db.get_sor_document(document_id)
        if not document or document["project_id"] != project_id:
            raise ApplicationError(
                status_code=404, detail="SOR document not found for this project."
            )
    rows = db.list_requirements(project_id, document_id)
    if document:
        rows = _enrich_requirements_with_preview_context(
            rows, str(_runtime_record_path(document))
        )
        rows = _attach_table_asset_urls_to_rows(
            rows, project_id=project_id, document_id=document_id
        )
    return _present_requirements(rows)

def get_review_logs(requirement_id: str) -> list[dict]:
    requirement = db.get_requirement(requirement_id)
    if not requirement:
        raise ApplicationError(status_code=404, detail="Requirement not found.")
    return db.list_review_logs(requirement_id)

def _safe_name(file_name: str) -> str:
    return (
        "".join(ch for ch in file_name if ch.isalnum() or ch in {".", "_", "-"}).strip()
        or "file"
    )

def _dump_snapshot_json(requirements: list[dict]) -> str:
    snapshot: list[dict] = []
    for row in requirements:
        snapshot.append(
            {
                "id": row.get("id"),
                "req_code": row.get("req_code"),
                "display_req_code": row.get("display_req_code"),
                "source_page": row.get("source_page"),
                "source_anchor": row.get("source_anchor"),
                "requirement_text": row.get("requirement_text"),
                "display_text": row.get("display_text"),
                "raw_text": row.get("raw_text"),
                "heading_text": row.get("heading_text"),
                "parent_section_code": row.get("parent_section_code"),
                "parent_section_title": row.get("parent_section_title"),
                "parent_section_label": row.get("parent_section_label"),
                "entry_kind": row.get("entry_kind"),
                "text_relation": row.get("text_relation"),
                "is_deduped": row.get("is_deduped"),
                "merged_from_req_codes": row.get("merged_from_req_codes"),
                "merged_from_entry_ids": row.get("merged_from_entry_ids"),
                "dedupe_version": row.get("dedupe_version"),
                "table_refs": row.get("table_refs", []),
                "content_blocks": row.get("content_blocks", []),
                "has_table_asset": row.get("has_table_asset"),
                "table_render_mode": row.get("table_render_mode"),
                "complex_table_mode": row.get("complex_table_mode"),
                "complex_zone_id": row.get("complex_zone_id"),
                "complex_family": row.get("complex_family"),
                "complex_role": row.get("complex_role"),
                "complex_group_label": row.get("complex_group_label"),
                "complex_split_level": row.get("complex_split_level"),
                "complex_warnings": row.get("complex_warnings", []),
                "complex_row_policy": row.get("complex_row_policy"),
                "requirement_type": row.get("requirement_type"),
                "priority": row.get("priority"),
                "ai_decision": row.get("ai_decision"),
                "compliance_status": row.get("compliance_status"),
                "ai_reason": row.get("ai_reason"),
                "ai_confidence": row.get("ai_confidence"),
                "assessment_mode": row.get("assessment_mode"),
                "assessment_reference_status": row.get("assessment_reference_status"),
                "source_requirement_id": row.get("source_requirement_id"),
                "analysis_label": row.get("analysis_label"),
                "analysis_basis": row.get("analysis_basis"),
                "normalized_requirement": row.get("normalized_requirement"),
                "risk_level": row.get("risk_level"),
                "clarification_question": row.get("clarification_question"),
                "evidence_findings": row.get("evidence_findings", []),
                "structured_information_gap": row.get(
                    "structured_information_gap", {}
                ),
                "sub_judgments": row.get("sub_judgments", []),
                "final_decision": row.get("final_decision"),
                "engineer_reason": row.get("engineer_reason"),
                "status": row.get("status"),
                "evidences": row.get("evidences", []),
            }
        )
    return json.dumps(snapshot, ensure_ascii=False)

def _parse_snapshot_json(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]

def _build_analysis_rows_from_preview(preview: dict) -> list[dict]:
    rows = (
        preview.get("complete_requirement_entries") if isinstance(preview, dict) else []
    )
    if not isinstance(rows, list):
        return []
    normalized: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if is_complex_review_only_row(row):
            continue
        entry_kind = str(row.get("entry_kind") or "REQUIREMENT").strip().upper()
        if entry_kind not in {"REQUIREMENT", "HYBRID"}:
            continue
        text = str(row.get("display_text") or row.get("requirement_text") or "").strip()
        if not text:
            continue
        base = {
            **row,
            "entry_kind": entry_kind,
            "section_code": str(row.get("section_code") or "").strip(),
            "section_title": str(row.get("section_title") or "").strip(),
            "section_label": str(row.get("section_label") or "").strip(),
            "section_key": str(row.get("section_key") or "").strip(),
            "section_instance_key": str(row.get("section_instance_key") or "").strip(),
            "display_text": text,
            "raw_text": str(
                row.get("raw_text") or row.get("requirement_text") or text
            ).strip(),
            "text_relation": str(row.get("text_relation") or "distinct").strip()
            or "distinct",
            "is_deduped": bool(row.get("is_deduped")),
            "parent_section_code": str(row.get("parent_section_code") or "").strip(),
            "parent_section_title": str(row.get("parent_section_title") or "").strip(),
            "parent_section_label": str(row.get("parent_section_label") or "").strip(),
            "owner_section_code": str(row.get("owner_section_code") or "").strip(),
            "owner_section_title": str(row.get("owner_section_title") or "").strip(),
            "owner_section_label": str(row.get("owner_section_label") or "").strip(),
            "structural_parent_section_code": str(
                row.get("structural_parent_section_code") or ""
            ).strip(),
            "structural_parent_section_key": str(
                row.get("structural_parent_section_key") or ""
            ).strip(),
            "structural_parent_section_title": str(
                row.get("structural_parent_section_title") or ""
            ).strip(),
            "structural_parent_section_label": str(
                row.get("structural_parent_section_label") or ""
            ).strip(),
            "merged_from_req_codes": row.get("merged_from_req_codes") or [],
            "merged_from_entry_ids": row.get("merged_from_entry_ids") or [],
            "table_refs": row.get("table_refs") or [],
            "content_blocks": row.get("content_blocks") or [],
            "has_table_asset": bool(row.get("has_table_asset")),
            "table_render_mode": str(row.get("table_render_mode") or "").strip(),
            **_copy_complex_row_fields(row),
        }
        normalized.append(
            hydrate_complex_row_metadata(_refresh_enriched_entry_text_fields(base))
        )
    return _attach_assessment_source_context(normalized)

def _assessment_context_item(row: dict) -> dict[str, object]:
    return {
        "entry_id": str(row.get("entry_id") or row.get("id") or "").strip(),
        "req_code": str(row.get("req_code") or "").strip(),
        "text": str(
            row.get("raw_text")
            or row.get("display_text")
            or row.get("requirement_text")
            or ""
        ).strip(),
        "source_page": row.get("source_page"),
        "source_anchor": str(row.get("source_anchor") or "").strip(),
        "parent_section_code": str(
            row.get("parent_section_code") or ""
        ).strip(),
        "parent_section_title": str(
            row.get("parent_section_title") or ""
        ).strip(),
    }

def _attach_assessment_source_context(rows: list[dict]) -> list[dict]:
    """Attach bounded, traceable hierarchy context to preview requirements."""

    copied_rows = [dict(row) for row in rows]
    by_identity: dict[str, dict] = {}
    children_by_parent: dict[str, list[dict]] = {}
    for row in copied_rows:
        for key in ("entry_id", "id", "req_code"):
            identity = str(row.get(key) or "").strip()
            if identity:
                by_identity.setdefault(identity, row)
        parent_identity = str(
            row.get("parent_entry_id")
            or row.get("parent_requirement_id")
            or row.get("parent_id")
            or ""
        ).strip()
        if parent_identity:
            children_by_parent.setdefault(parent_identity, []).append(row)

    def _section_identity(row: dict) -> tuple[str, str]:
        return (
            str(
                row.get("section_instance_key")
                or row.get("section_key")
                or row.get("parent_section_code")
                or ""
            ).strip(),
            str(row.get("parent_section_title") or "").strip(),
        )

    for index, row in enumerate(copied_rows):
        parent_identity = str(
            row.get("parent_entry_id")
            or row.get("parent_requirement_id")
            or row.get("parent_id")
            or ""
        ).strip()
        if not row.get("direct_parent") and parent_identity in by_identity:
            row["direct_parent"] = _assessment_context_item(
                by_identity[parent_identity]
            )

        own_identities = {
            str(row.get(key) or "").strip()
            for key in ("entry_id", "id", "req_code")
            if str(row.get(key) or "").strip()
        }
        if not row.get("direct_children"):
            children: list[dict[str, object]] = []
            seen_children: set[int] = set()
            for identity in own_identities:
                for child in children_by_parent.get(identity, []):
                    if id(child) in seen_children:
                        continue
                    seen_children.add(id(child))
                    children.append(_assessment_context_item(child))
            if children:
                row["direct_children"] = children[:8]

        if not row.get("adjacent_context"):
            section_identity = _section_identity(row)
            adjacent: list[dict[str, object]] = []
            for neighbor_index in (
                index - 2,
                index - 1,
                index + 1,
                index + 2,
            ):
                if not 0 <= neighbor_index < len(copied_rows):
                    continue
                neighbor = copied_rows[neighbor_index]
                if _section_identity(neighbor) != section_identity:
                    continue
                adjacent.append(_assessment_context_item(neighbor))
            if adjacent:
                row["adjacent_context"] = adjacent

        if not row.get("ancestor_path"):
            ancestors: list[dict[str, str]] = []
            seen_ancestors: set[tuple[str, str]] = set()
            for code_key, title_key, source in (
                (
                    "structural_parent_section_code",
                    "structural_parent_section_title",
                    "structural_parent",
                ),
                ("owner_section_code", "owner_section_title", "owner_section"),
                (
                    "parent_section_code",
                    "parent_section_title",
                    "current_parent_section",
                ),
            ):
                code = str(row.get(code_key) or "").strip()
                title = str(row.get(title_key) or "").strip()
                signature = (code, title)
                if not (code or title) or signature in seen_ancestors:
                    continue
                seen_ancestors.add(signature)
                ancestors.append(
                    {
                        "section_code": code,
                        "section_title": title,
                        "path_source": source,
                    }
                )
            if ancestors:
                row["ancestor_path"] = ancestors
                row["ancestor_path_status"] = "PARTIAL"
    return copied_rows

def _build_analysis_rows_from_review_payload(
    entries: list[ReviewedRequirementEntry],
) -> list[dict]:
    normalized: list[dict] = []
    for item in entries:
        row = item.model_dump()
        if not bool(row.get("keep", True)):
            continue
        if is_complex_review_only_row(row):
            continue
        entry_kind = str(row.get("entry_kind") or "REQUIREMENT").strip().upper()
        if entry_kind not in {"REQUIREMENT", "HYBRID", "HEADING"}:
            continue
        display_text = str(
            row.get("display_text") or row.get("requirement_text") or ""
        ).strip()
        raw_text = str(
            row.get("raw_text") or row.get("requirement_text") or display_text
        ).strip()
        preferred_text = raw_text or display_text
        if not preferred_text:
            continue
        normalized.append(
            _refresh_enriched_entry_text_fields(
                {
                    "entry_id": str(row.get("entry_id") or row.get("req_code") or ""),
                    "req_code": str(row.get("req_code") or "").strip(),
                    "entry_kind": entry_kind,
                    "section_code": str(row.get("section_code") or "").strip(),
                    "section_title": str(row.get("section_title") or "").strip(),
                    "section_label": str(row.get("section_label") or "").strip(),
                    "section_key": str(row.get("section_key") or "").strip(),
                    "section_instance_key": str(
                        row.get("section_instance_key") or ""
                    ).strip(),
                    "source_page": row.get("source_page"),
                    "source_page_end": row.get("source_page_end"),
                    "source_anchor": str(row.get("source_anchor") or "Unknown"),
                    "display_text": preferred_text,
                    "raw_text": preferred_text,
                    "requirement_type": str(
                        row.get("requirement_type") or "FUNCTIONAL"
                    ).strip()
                    or "FUNCTIONAL",
                    "priority": str(row.get("priority") or "MEDIUM").strip()
                    or "MEDIUM",
                    "text_relation": str(row.get("text_relation") or "distinct").strip()
                    or "distinct",
                    "is_deduped": bool(row.get("is_deduped")),
                    "heading_text": str(row.get("heading_text") or "").strip(),
                    "parent_section_code": str(
                        row.get("parent_section_code") or ""
                    ).strip(),
                    "parent_section_title": str(
                        row.get("parent_section_title") or ""
                    ).strip(),
                    "parent_section_label": str(
                        row.get("parent_section_label") or ""
                    ).strip(),
                    "owner_section_code": str(
                        row.get("owner_section_code") or ""
                    ).strip(),
                    "owner_section_title": str(
                        row.get("owner_section_title") or ""
                    ).strip(),
                    "owner_section_label": str(
                        row.get("owner_section_label") or ""
                    ).strip(),
                    "structural_parent_section_code": str(
                        row.get("structural_parent_section_code") or ""
                    ).strip(),
                    "structural_parent_section_key": str(
                        row.get("structural_parent_section_key") or ""
                    ).strip(),
                    "structural_parent_section_title": str(
                        row.get("structural_parent_section_title") or ""
                    ).strip(),
                    "structural_parent_section_label": str(
                        row.get("structural_parent_section_label") or ""
                    ).strip(),
                    "merged_from_req_codes": row.get("merged_from_req_codes") or [],
                    "merged_from_entry_ids": row.get("merged_from_entry_ids") or [],
                    "table_refs": row.get("table_refs") or [],
                    "content_blocks": row.get("content_blocks") or [],
                    "has_table_asset": bool(row.get("has_table_asset")),
                    "table_render_mode": str(
                        row.get("table_render_mode") or ""
                    ).strip(),
                    "dedupe_version": str(
                        row.get("dedupe_version") or "heading_requirement_v1"
                    ),
                    **_copy_complex_row_fields(row),
                }
            )
        )
    return normalized

def _persist_requirements(
    *,
    project_id: str,
    sor_document_id: str,
    rows: list[dict],
    knowledge_docs: list[dict],
    dedupe_version: str = "",
    workflow_stage: str = "REVIEWING",
) -> int:
    from app.application.ingestion import _normalize_preview_generation_id
    from app.application.knowledge import _ensure_profile_knowledge_syncs

    document = db.get_sor_document(sor_document_id) or {}
    preview_generation_id = _normalize_preview_generation_id(
        document.get("preview_generation_id")
    )
    requirement_set = db.create_requirement_set(
        project_id,
        sor_document_id,
        source_hash="",
        preview_generation_id=preview_generation_id,
    )
    requirement_set_id = str(requirement_set.get("id") or "")
    if not requirement_set_id:
        raise RuntimeError("Failed to create requirement set draft.")
    used_req_codes: set[str] = set()
    inserted = 0
    try:
        materialized_rows, table_assets = _materialize_document_table_assets(
            rows,
            sor_document_id=sor_document_id,
            requirement_set_id=requirement_set_id,
        )
        for index, item in enumerate(materialized_rows, start=1):
            base_req_code = str(item.get("req_code") or "").strip() or f"REQ{index:04d}"
            req_code = _ensure_unique_req_code(base_req_code, used_req_codes)
            used_req_codes.add(req_code)
            display_text = str(
                item.get("display_text") or item.get("requirement_text") or ""
            ).strip()
            raw_text = str(item.get("raw_text") or display_text).strip()
            heading_text = str(item.get("heading_text") or "").strip()
            req = db.insert_requirement(
                project_id=project_id,
                sor_document_id=sor_document_id,
                requirement_set_id=requirement_set_id,
                req_code=req_code,
                source_page=item.get("source_page"),
                source_anchor=item.get("source_anchor") or "Unknown",
                requirement_text=display_text,
                requirement_type=str(
                    item.get("requirement_type") or "FUNCTIONAL"
                ).strip()
                or "FUNCTIONAL",
                priority=str(item.get("priority") or "MEDIUM").strip() or "MEDIUM",
                status="DRAFT",
                entry_kind=str(item.get("entry_kind") or "REQUIREMENT").strip().upper()
                or "REQUIREMENT",
                text_relation=str(item.get("text_relation") or "distinct").strip()
                or "distinct",
                is_deduped=bool(item.get("is_deduped")),
                parent_section_code=str(item.get("parent_section_code") or "").strip(),
                parent_section_title=str(
                    item.get("parent_section_title") or ""
                ).strip(),
                parent_section_label=str(
                    item.get("parent_section_label") or ""
                ).strip(),
                heading_text=heading_text,
                raw_text=raw_text,
                display_text=display_text,
                merged_from_req_codes=_json_dumps_list(
                    item.get("merged_from_req_codes")
                ),
                merged_from_entry_ids=_json_dumps_list(
                    item.get("merged_from_entry_ids")
                ),
                dedupe_version=str(item.get("dedupe_version") or dedupe_version or ""),
                table_refs=_json_dumps(item.get("table_refs") or []),
                content_blocks=_json_dumps(item.get("content_blocks") or []),
                has_table_asset=bool(item.get("has_table_asset")),
                table_render_mode=str(item.get("table_render_mode") or ""),
                preview_generation_id=preview_generation_id,
            )
            if knowledge_docs:
                ai_config = load_ai_copilot_config()
                _ensure_profile_knowledge_syncs(project_id, knowledge_docs, ai_config)
                assessment_input = {
                    **req,
                    "display_text": display_text,
                    "requirement_text": display_text,
                    "requirement_type": str(
                        item.get("requirement_type") or "FUNCTIONAL"
                    ).strip()
                    or "FUNCTIONAL",
                    "parent_section_code": str(
                        item.get("parent_section_code") or ""
                    ).strip(),
                    "parent_section_title": str(
                        item.get("parent_section_title") or ""
                    ).strip(),
                    "parent_section_label": str(
                        item.get("parent_section_label") or ""
                    ).strip(),
                    "heading_text": heading_text,
                    "content_blocks": item.get("content_blocks") or [],
                    "table_refs": item.get("table_refs") or [],
                }
                result = run_ai_copilot_assessment(
                    assessment_input, knowledge_docs, config=ai_config
                )
                assessment = result["assessment"]
                runs = [
                    run for run in (result.get("runs") or []) if isinstance(run, dict)
                ]
                _record_assessment_runs(req["id"], runs)
                verdict = evaluate_assessment_execution(
                    assessment=assessment,
                    runs=runs,
                    configured_mode=str(ai_config.assessment_mode or ""),
                )
                if verdict.serviceable:
                    from app.application.requirement_set_impact_service import (
                        REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION,
                        requirement_analysis_input_sha256,
                    )

                    persist_serving_result(
                        req["id"],
                        result,
                        profile_id=ai_config.profile_id,
                        provider=ai_config.provider,
                        retrieval_backend=ai_config.retrieval_backend,
                        analysis_input_schema_version=(
                            REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
                        ),
                        analysis_input_sha256=(
                            requirement_analysis_input_sha256(assessment_input)
                        ),
                    )
                else:
                    logger.warning(
                        "Skipped non-serviceable AI candidate while importing requirement %s: %s",
                        req["id"],
                        verdict.failure_reason,
                    )
            inserted += 1
        activated = db.activate_requirement_set(
            project_id,
            sor_document_id,
            requirement_set_id,
            workflow_stage=workflow_stage,
            table_assets=table_assets,
            preview_generation_id=preview_generation_id,
        )
        if not activated:
            raise RuntimeError("Failed to activate generated requirement set.")
    except Exception:
        db.discard_inactive_requirement_set(
            sor_document_id,
            requirement_set_id,
            project_id=project_id,
        )
        failed_snapshot_root = _table_snapshot_root(
            sor_document_id, requirement_set_id, create=False
        )
        if failed_snapshot_root.exists():
            shutil.rmtree(failed_snapshot_root, ignore_errors=True)
        raise
    return inserted

def _detect_dedupe_version(rows: list[dict]) -> str:
    for row in rows:
        token = str(row.get("dedupe_version") or "").strip()
        if token:
            return token
    return ""

def _ensure_unique_req_code(base: str, used: set[str]) -> str:
    token = str(base or "").strip() or "REQ0000"
    if token not in used:
        return token
    suffix = 2
    while f"{token}-D{suffix}" in used:
        suffix += 1
    return f"{token}-D{suffix}"

def _json_dumps_list(value: object) -> str:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    elif value is None:
        items = []
    else:
        text = str(value).strip()
        items = [text] if text else []
    return json.dumps(items, ensure_ascii=False)

def _json_dumps(value: object) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)

def _table_snapshot_root(
    document_id: str,
    requirement_set_id: str = "",
    *,
    create: bool = True,
) -> Path:
    from app.application.runtime import TABLE_SNAPSHOT_DIR

    document_token = (
        re.sub(r"[^A-Za-z0-9._-]+", "_", str(document_id or "").strip()) or "preview"
    )
    set_token = re.sub(r"[^A-Za-z0-9._-]+", "_", str(requirement_set_id or "").strip())
    path = TABLE_SNAPSHOT_DIR / "persisted" / document_token
    if set_token:
        path = path / set_token
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path

def _purge_table_snapshot_root(document_id: str) -> None:
    path = _table_snapshot_root(document_id, create=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)

def _materialize_document_table_assets(
    rows: list[dict],
    *,
    sor_document_id: str,
    requirement_set_id: str = "",
) -> tuple[list[dict], list[dict]]:
    from app.application.runtime import _runtime_record_path

    source_file_path = ""
    copied_rows = [dict(item) for item in rows]
    document = db.get_sor_document(sor_document_id)
    if document:
        source_file_path = str(_runtime_record_path(document)).strip()
        preview, _ = load_cached_preview(document)
        preview_index = _build_requirement_row_index(
            _build_analysis_rows_from_preview(preview or {})
        )
        for row in copied_rows:
            preview_row = _find_indexed_requirement_row(
                row, preview_index, allow_weak_identifier=True
            )
            if not preview_row:
                continue
            if (
                _coerce_positive_int(row.get("source_page")) <= 0
                and _coerce_positive_int(preview_row.get("source_page")) > 0
            ):
                row["source_page"] = preview_row.get("source_page")
            if _coerce_positive_int(row.get("source_page_end")) <= 0:
                preview_end = _coerce_positive_int(preview_row.get("source_page_end"))
                preview_start = _coerce_positive_int(preview_row.get("source_page"))
                if preview_end > 0 or preview_start > 0:
                    row["source_page_end"] = preview_end or preview_start
    return enrich_entries_with_table_assets(
        copied_rows,
        document_id=sor_document_id,
        snapshot_root=_table_snapshot_root(sor_document_id, requirement_set_id),
        source_file_path=source_file_path,
    )

def _sync_document_table_assets(project_id: str, sor_document_id: str) -> None:
    from app.application.ingestion import _normalize_preview_generation_id

    document = db.get_sor_document(sor_document_id) or {}
    preview_generation_id = _normalize_preview_generation_id(
        document.get("preview_generation_id")
    )
    requirement_set_id = str(document.get("active_requirement_set_id") or "").strip()
    if not requirement_set_id:
        return
    current_rows = db.list_requirements(project_id, sor_document_id)
    materialized_rows, table_assets = _materialize_document_table_assets(
        current_rows,
        sor_document_id=sor_document_id,
        requirement_set_id=requirement_set_id,
    )
    for row in materialized_rows:
        requirement_id = str(row.get("id") or "").strip()
        if not requirement_id:
            continue
        db.update_requirement_entry(
            requirement_id,
            source_page=row.get("source_page"),
            source_anchor=str(row.get("source_anchor") or "Unknown"),
            requirement_text=str(
                row.get("requirement_text") or row.get("display_text") or ""
            ),
            requirement_type=str(row.get("requirement_type") or "FUNCTIONAL"),
            priority=str(row.get("priority") or "MEDIUM"),
            entry_kind=str(row.get("entry_kind") or "REQUIREMENT"),
            text_relation=str(row.get("text_relation") or "distinct"),
            is_deduped=bool(row.get("is_deduped")),
            parent_section_code=str(row.get("parent_section_code") or ""),
            parent_section_title=str(row.get("parent_section_title") or ""),
            parent_section_label=str(row.get("parent_section_label") or ""),
            heading_text=str(row.get("heading_text") or ""),
            raw_text=str(row.get("raw_text") or ""),
            display_text=str(
                row.get("display_text") or row.get("requirement_text") or ""
            ),
            merged_from_req_codes=_json_dumps_list(row.get("merged_from_req_codes")),
            merged_from_entry_ids=_json_dumps_list(row.get("merged_from_entry_ids")),
            dedupe_version=str(row.get("dedupe_version") or ""),
            table_refs=_json_dumps(row.get("table_refs") or []),
            content_blocks=_json_dumps(row.get("content_blocks") or []),
            has_table_asset=bool(row.get("has_table_asset")),
            table_render_mode=str(row.get("table_render_mode") or ""),
            preview_generation_id=preview_generation_id,
        )
    db.replace_document_table_assets(
        project_id,
        sor_document_id,
        table_assets,
        requirement_set_id=requirement_set_id,
        preview_generation_id=preview_generation_id,
    )

def _enrich_requirements_with_preview_context(
    rows: list[dict], file_path: str
) -> list[dict]:
    from app.application.ingestion import (
        _normalize_preview_generation_id,
        _resolve_active_preview_generation_id,
        _row_visual_assets_match_active_generation,
        _strip_preview_visual_assets,
    )

    if not rows or not file_path:
        return rows
    document = None
    for row in rows:
        document_id = str(row.get("sor_document_id") or "").strip()
        if document_id:
            document = db.get_sor_document(document_id)
            if document:
                break
    if document is None:
        return rows
    preview, _ = load_cached_preview(document)
    if preview is None:
        return rows
    active_generation_id = _resolve_active_preview_generation_id(
        preview=preview, document=document
    )
    requirement_set_ids = {
        str(row.get("requirement_set_id") or "").strip()
        for row in rows
        if str(row.get("requirement_set_id") or "").strip()
    }
    refresh_visual_assets = not requirement_set_ids
    if len(requirement_set_ids) == 1:
        requirement_set_id = next(iter(requirement_set_ids))
        # Preview enrichment is only valid for the active set. Historical sets
        # may share a generation identifier with a fork, but their persisted
        # visual assets are immutable and must never be overlaid by today's cache.
        if (
            requirement_set_id
            != str(document.get("active_requirement_set_id") or "").strip()
        ):
            return rows
        requirement_set = db.get_requirement_set(requirement_set_id) or {}
        set_generation_id = _normalize_preview_generation_id(
            requirement_set.get("preview_generation_id")
        )
        if (
            set_generation_id
            and active_generation_id
            and set_generation_id != active_generation_id
        ):
            return rows
        refresh_visual_assets = True
    elif len(requirement_set_ids) > 1:
        return rows
    preview_index = _build_requirement_row_index(
        _build_analysis_rows_from_preview(preview)
    )
    enriched: list[dict] = []
    pending_updates: list[dict[str, str]] = []
    for row in rows:
        copied = dict(row)
        changed = False
        if refresh_visual_assets and not _row_visual_assets_match_active_generation(
            copied, active_generation_id
        ):
            copied = _strip_preview_visual_assets(copied)
            changed = True
        meta = _find_indexed_requirement_row(
            row, preview_index, allow_weak_identifier=True
        )
        if not meta:
            enriched.append(copied if changed else row)
            continue
        for key in (
            "full_parent_clause",
            "parent_clause_text",
            "ancestor_path",
            "ancestor_path_status",
            "direct_parent",
            "direct_children",
            "adjacent_context",
        ):
            if copied.get(key) in (None, "", [], {}) and meta.get(key) not in (
                None,
                "",
                [],
                {},
            ):
                copied[key] = meta.get(key)
                changed = True
        for key in (
            "parent_section_code",
            "parent_section_title",
            "parent_section_label",
        ):
            if not str(copied.get(key) or "").strip():
                value = str(meta.get(key) or "").strip()
                if value:
                    copied[key] = value
                    changed = True
        for key in (
            "section_code",
            "section_title",
            "section_label",
            "section_key",
            "section_instance_key",
            "owner_section_code",
            "owner_section_title",
            "owner_section_label",
            "structural_parent_section_code",
            "structural_parent_section_key",
            "structural_parent_section_title",
            "structural_parent_section_label",
        ):
            if not str(copied.get(key) or "").strip():
                value = str(meta.get(key) or "").strip()
                if value:
                    copied[key] = value
                    changed = True
        if not copied.get("table_refs") and meta.get("table_refs"):
            copied["table_refs"] = meta.get("table_refs") or []
            changed = True
        if not copied.get("content_blocks") and meta.get("content_blocks"):
            copied["content_blocks"] = meta.get("content_blocks") or []
            changed = True
        if not bool(copied.get("has_table_asset")) and bool(
            meta.get("has_table_asset")
        ):
            copied["has_table_asset"] = True
            changed = True
        if (
            not str(copied.get("table_render_mode") or "").strip()
            and str(meta.get("table_render_mode") or "").strip()
        ):
            copied["table_render_mode"] = str(
                meta.get("table_render_mode") or ""
            ).strip()
            changed = True
        if not bool(copied.get("complex_table_mode")) and bool(
            meta.get("complex_table_mode")
        ):
            copied.update(_copy_complex_row_fields(meta))
            changed = True
        if (
            refresh_visual_assets
            and active_generation_id
            and str(copied.get("preview_generation_id") or "").strip()
            != active_generation_id
        ):
            copied["preview_generation_id"] = active_generation_id
            changed = True
        if copied.get("table_refs") or copied.get("content_blocks"):
            refreshed = hydrate_complex_row_metadata(
                normalize_preserved_entry_table_assets(copied)
            )
            refreshed = _refresh_enriched_entry_text_fields(refreshed)
            if (
                str(refreshed.get("display_text") or "").strip()
                != str(copied.get("display_text") or "").strip()
                or str(refreshed.get("raw_text") or "").strip()
                != str(copied.get("raw_text") or "").strip()
            ):
                copied = refreshed
                changed = True
        if changed and str(copied.get("id") or "").strip():
            pending_updates.append(
                {
                    "id": str(copied.get("id") or "").strip(),
                    "parent_section_code": str(
                        copied.get("parent_section_code") or ""
                    ).strip(),
                    "parent_section_title": str(
                        copied.get("parent_section_title") or ""
                    ).strip(),
                    "parent_section_label": str(
                        copied.get("parent_section_label") or ""
                    ).strip(),
                }
            )
        enriched.append(copied)
    if pending_updates:
        db.update_requirement_parent_sections(pending_updates)
    return enriched

def _load_document_preview(document: dict | None) -> dict | None:
    preview, _ = load_cached_preview(document)
    return preview

def _attach_table_asset_urls_to_rows(
    rows: list[dict], *, project_id: str, document_id: str
) -> list[dict]:
    return [
        prepare_table_ref_urls(row, project_id=project_id, document_id=document_id)
        for row in rows or []
        if isinstance(row, dict)
    ]

def _attach_table_asset_urls_to_preview(
    preview: dict | None, *, project_id: str, document_id: str
) -> dict | None:
    from app.application.ingestion import (
        _filter_table_assets_by_generation,
        _resolve_active_preview_generation_id,
    )

    if not isinstance(preview, dict):
        return preview
    document = db.get_sor_document(document_id)
    active_generation_id = _resolve_active_preview_generation_id(
        preview=preview, document=document
    )
    copied = dict(preview)
    copied["complete_requirement_entries"] = _attach_table_asset_urls_to_rows(
        copied.get("complete_requirement_entries") or [],
        project_id=project_id,
        document_id=document_id,
    )
    copied["complete_requirement_entries_raw"] = _attach_table_asset_urls_to_rows(
        copied.get("complete_requirement_entries_raw") or [],
        project_id=project_id,
        document_id=document_id,
    )
    assets = copied.get("table_assets")
    if not isinstance(assets, list) or not assets:
        assets = db.list_document_table_assets(project_id, document_id)
    assets = _filter_table_assets_by_generation(
        assets, active_generation_id=active_generation_id
    )
    copied["table_assets"] = [
        prepare_table_asset_urls(asset, project_id=project_id, document_id=document_id)
        for asset in assets
        if isinstance(asset, dict)
    ]
    return copied

def _strip_workspace_preview(workspace: dict[str, object]) -> dict[str, object]:
    copied = dict(workspace)
    copied.pop("preview", None)
    copied.pop("review_entries", None)
    copied.pop("review_entries_raw", None)
    copied.pop("preview_generated_at", None)
    return copied

def _build_published_analysis_fallback_workspace(
    *,
    project: dict[str, Any],
    document: dict[str, Any],
    requirements: list[dict[str, Any]],
    knowledge_docs_count: int,
    projection_error: str,
) -> dict[str, object]:
    """Return a truthful minimal response after the serving transaction committed."""
    from app.application.assessment import _serialize_ai_analysis_document


    return {
        "project": {
            "id": project.get("id"),
            "name": project.get("name"),
            "customer_name": project.get("customer_name"),
            "knowledge_base_version": int(project.get("knowledge_base_version") or 0),
        },
        "document": {
            **_serialize_ai_analysis_document(document),
            "project_id": document.get("project_id"),
            "file_name": document.get("file_name"),
            "file_type": document.get("file_type"),
        },
        "summary": {
            "requirement_count": len(requirements),
            "knowledge_doc_count": max(int(knowledge_docs_count or 0), 0),
        },
        "actions": {
            "can_run_ai_analysis": True,
            "can_reload_workspace": True,
        },
        "ai_profiles": [],
        "requirements": requirements,
        "workspace_projection": {
            "status": "FAILED",
            "error": str(projection_error or "workspace response assembly failed"),
            "published": True,
            "recommended_action": "Reload the document workspace; the published assessment remains current.",
        },
    }

def _build_document_workspace(
    *,
    project: dict,
    document: dict,
    requirements: list[dict],
    knowledge_docs_count: int = 0,
    preview: dict | None = None,
    preview_meta: dict | None = None,
) -> dict:
    from app.application.ingestion import (
        _normalize_preview_generation_id,
        _resolve_active_preview_generation_id,
    )
    from app.application.knowledge import (
        _attach_knowledge_syncs,
        _build_ai_profile_catalog,
        _list_project_knowledge_documents,
    )

    ai_runtime = load_ai_copilot_config()
    project_id = str(project.get("id") or "")
    document_id = str(document.get("id") or "")
    knowledge_docs = _attach_knowledge_syncs(
        _list_project_knowledge_documents(project_id)
    )
    ai_profiles = _build_ai_profile_catalog(knowledge_docs)
    active_set_id = str(document.get("active_requirement_set_id") or "").strip()
    active_set = db.get_requirement_set(active_set_id) if active_set_id else None
    active_tree_inspection = (
        _inspect_requirement_set_tree(active_set, requirements)
        if active_set is not None
        else {
            "requirement_tree_state": "MISSING",
            "requirement_tree_has_evidence_bundle": False,
            "tree_lock_ready": False,
        }
    )
    set_preview_generation_id = _normalize_preview_generation_id(
        (active_set or {}).get("preview_generation_id")
    )
    cached_preview_generation_id = _normalize_preview_generation_id(
        (preview or {}).get("preview_generation_id")
        if isinstance(preview, dict)
        else ""
    )
    if (
        preview is not None
        and set_preview_generation_id
        and cached_preview_generation_id
        and set_preview_generation_id != cached_preview_generation_id
    ):
        # A restored historical Requirement Set must never be overlaid with the
        # latest extraction's table assets. Its persisted rows remain authoritative.
        preview = None
    preview = _attach_table_asset_urls_to_preview(
        preview, project_id=project_id, document_id=document_id
    )
    requirement_set_version = _coerce_int(document.get("requirement_set_version"))
    knowledge_base_version = _coerce_int(project.get("knowledge_base_version"))
    ai_requirement_set_version = _coerce_int(document.get("ai_requirement_set_version"))
    ai_knowledge_base_version = _coerce_int(document.get("ai_knowledge_base_version"))
    ai_status = str(document.get("ai_status") or "NOT_RUN").strip().upper() or "NOT_RUN"
    preview_meta = preview_meta or get_preview_cache_state(document)
    ai_stale = ai_status == "STALE" or (
        ai_status == "READY"
        and (
            ai_requirement_set_version != requirement_set_version
            or ai_knowledge_base_version != knowledge_base_version
        )
    )
    if ai_stale and ai_status == "READY":
        ai_status = "STALE"
    preview_generation_id = _resolve_active_preview_generation_id(
        preview=preview, document=document
    )

    review_entries: list[dict] = []
    review_entries_raw: list[dict] = []
    presented_requirements, order_diagnostics = _prepare_requirements_for_display(
        requirements, preview=preview
    )
    if preview is not None:
        review_entries = _overlay_review_entries(
            preview.get("complete_requirement_entries"),
            requirements,
            prefer_raw=False,
            preview_generation_id=preview_generation_id,
        )
        review_entries_raw = _overlay_review_entries(
            preview.get("complete_requirement_entries_raw")
            or preview.get("complete_requirement_entries"),
            requirements,
            prefer_raw=True,
            preview_generation_id=preview_generation_id,
        )
        if not review_entries and requirements:
            review_entries = [dict(item, keep=True) for item in presented_requirements]
        if not review_entries_raw and requirements:
            review_entries_raw = [
                dict(item, keep=True) for item in presented_requirements
            ]
        presented_requirements, order_diagnostics = (
            _build_workspace_requirements_from_review_entries(
                review_entries,
                fallback_requirements=requirements,
                preview=preview,
            )
        )

    ai_eligible_count = sum(
        1 for row in presented_requirements if _is_ai_eligible_requirement(row)
    )
    assessment_target_count = sum(
        1 for row in presented_requirements if _is_assessment_target_requirement(row)
    )
    info_count = sum(
        1 for row in presented_requirements if _is_info_only_requirement(row)
    )
    decision_rows = [
        row for row in presented_requirements if _is_ai_eligible_requirement(row)
    ]
    decision_eligible_count = len(decision_rows)
    heading_count = sum(
        1
        for row in presented_requirements
        if str(row.get("entry_kind") or "REQUIREMENT").strip().upper() == "HEADING"
    )
    human_decision_count = sum(
        1 for row in decision_rows if str(row.get("final_decision") or "").strip()
    )
    unassigned_count = max(decision_eligible_count - human_decision_count, 0)
    human_coverage = (
        human_decision_count / decision_eligible_count
        if decision_eligible_count
        else 1.0
    )
    effective_conclusion_count = 0
    missing_effective_conclusion_count = decision_eligible_count
    effective_conclusion_coverage = 1.0 if not decision_eligible_count else 0.0
    ai_default_count = 0
    human_override_count = 0
    human_native_count = 0
    matrix_lock_readiness: dict[str, Any] | None = None
    matrix_snapshot = None
    if active_set_id and bool(active_tree_inspection.get("tree_lock_ready")):
        try:
            matrix_lock_readiness, matrix_snapshot = (
                _build_requirement_set_lock_readiness(
                    project_id,
                    document_id,
                    active_set_id,
                    document=document,
                )
            )
        except ApplicationError:
            matrix_lock_readiness = None
            matrix_snapshot = None
    matrix_ready = bool(
        matrix_lock_readiness is not None
        and matrix_snapshot is not None
        and matrix_lock_readiness.get("matrix_state") == "READY"
    )
    if matrix_ready:
        from app.domain.requirement_matrix import (
            count_missing_source_business_nodes,
            is_analysis_target,
            summarize_effective_conclusions,
        )

        matrix_targets = tuple(
            node
            for node in matrix_snapshot.nodes
            if is_analysis_target(node)
        )
        decision_eligible_count = len(matrix_targets)
        ai_eligible_count = len(matrix_targets)
        assessment_target_count = len(matrix_targets)
        human_decision_count = sum(
            bool(
                node.manual_decision is not None
                and node.manual_decision.decision.strip()
            )
            for node in matrix_targets
        )
        unassigned_count = max(
            decision_eligible_count - human_decision_count,
            0,
        )
        human_coverage = (
            human_decision_count / decision_eligible_count
            if decision_eligible_count
            else 1.0
        )
        conclusion_summary = summarize_effective_conclusions(matrix_targets)
        effective_conclusion_count = (
            conclusion_summary.effective_conclusion_count
        )
        missing_effective_conclusion_count = (
            conclusion_summary.missing_effective_conclusion_count
        )
        effective_conclusion_coverage = (
            conclusion_summary.effective_conclusion_coverage
        )
        ai_default_count = conclusion_summary.ai_default_count
        human_override_count = conclusion_summary.human_override_count
        human_native_count = conclusion_summary.human_native_count
        missing_source_count = count_missing_source_business_nodes(
            matrix_snapshot.nodes
        )
    else:
        missing_source_count = sum(
            1
            for row in decision_rows
            if not _coerce_positive_int(row.get("source_page"))
            and str(row.get("source_anchor") or "").strip().casefold()
            in {
                "",
                "unknown",
                "unknown source",
                "n/a",
                "na",
                "未提供",
                "未知",
            }
        )
    table_conflict_count = sum(
        1 for row in decision_rows if bool(row.get("complex_warnings"))
    )
    lock_blockers: list[dict[str, object]] = []
    if matrix_ready:
        lock_blockers.extend(matrix_lock_readiness.get("hard_blockers") or [])
    elif decision_eligible_count <= 0:
        lock_blockers.append({"code": "EMPTY_DECISION_SET", "count": 0, "hard": True})
    tree_lock_ready = bool(active_tree_inspection.get("tree_lock_ready"))
    if not tree_lock_ready:
        tree_state = str(
            active_tree_inspection.get("requirement_tree_state") or "MISSING"
        )
        blocker_code = {
            "INVALID": "REQUIREMENT_TREE_INVALID",
            "PUBLISHING": "REQUIREMENT_TREE_PUBLISHING",
        }.get(tree_state, "PREVIEW_NOT_STABLE")
        lock_blockers.append(
            {
                "code": blocker_code,
                "status": str(preview_meta.get("preview_status") or ""),
                "requirement_tree_state": tree_state,
                "hard": True,
            }
        )
    lock_basis = (
        "REQUIREMENT_TREE_EVIDENCE"
        if tree_lock_ready
        else "NONE"
    )
    lock_warnings: list[dict[str, object]] = []
    if missing_source_count:
        lock_warnings.append(
            {"code": "MISSING_SOURCE", "count": missing_source_count, "hard": False}
        )
    if table_conflict_count:
        lock_warnings.append(
            {"code": "TABLE_CONFLICT", "count": table_conflict_count, "hard": False}
        )
    locked = bool(str(document.get("extraction_locked_at") or "").strip())
    formal_blockers = list(lock_blockers)
    if not locked:
        formal_blockers.append({"code": "REQUIREMENT_SET_NOT_LOCKED", "hard": True})
    if (
        not str((active_set or {}).get("preview_generation_id") or "").strip()
        and str((active_set or {}).get("preview_status_at_lock") or "")
        .strip()
        .upper()
        != "READY"
    ):
        formal_blockers.append(
            {
                "code": "PREVIEW_NOT_STABLE",
                "status": "MISSING_REQUIREMENT_SET_PREVIEW",
                "hard": True,
            }
        )
    if missing_effective_conclusion_count:
        formal_blockers.append(
            {
                "code": "MISSING_EFFECTIVE_CONCLUSIONS",
                "count": missing_effective_conclusion_count,
                "hard": True,
            }
        )
    readiness = {
        "lock": {
            "ready": not lock_blockers and not locked,
            "blockers": lock_blockers,
            "hard_blockers": lock_blockers,
            "warnings": lock_warnings,
            "requires_waiver_reason": bool(lock_warnings),
            "basis": lock_basis,
            "requirement_tree_state": str(
                active_tree_inspection.get("requirement_tree_state")
                or "MISSING"
            ),
        },
        "formal_export": {
            "ready": not formal_blockers,
            "blockers": formal_blockers,
            "hard_blockers": formal_blockers,
            "warnings": lock_warnings,
        },
    }
    presented_requirements = _attach_table_asset_urls_to_rows(
        presented_requirements,
        project_id=project_id,
        document_id=document_id,
    )
    review_entries = _attach_table_asset_urls_to_rows(
        review_entries, project_id=project_id, document_id=document_id
    )
    review_entries_raw = _attach_table_asset_urls_to_rows(
        review_entries_raw,
        project_id=project_id,
        document_id=document_id,
    )
    workspace: dict[str, object] = {
        "project": {
            "id": project.get("id"),
            "name": project.get("name"),
            "customer_name": project.get("customer_name"),
            "knowledge_base_version": knowledge_base_version,
        },
        "document": {
            "id": document.get("id"),
            "project_id": document.get("project_id"),
            "file_name": document.get("file_name"),
            "file_type": document.get("file_type"),
            "workflow_stage": str(document.get("workflow_stage") or "UPLOADED"),
            "requirement_set_version": requirement_set_version,
            "active_requirement_set_id": str(
                document.get("active_requirement_set_id") or ""
            ),
            "extraction_locked_at": str(document.get("extraction_locked_at") or ""),
            "ai_status": ai_status,
            "ai_assessed_at": str(document.get("ai_assessed_at") or ""),
            "ai_requirement_set_version": ai_requirement_set_version,
            "ai_knowledge_base_version": ai_knowledge_base_version,
            "ai_stale": ai_stale,
            "configured_assessment_mode": ai_runtime.assessment_mode,
            "default_ai_profile_id": ai_runtime.profile_id,
            "created_at": document.get("created_at"),
            "updated_at": document.get("updated_at"),
            "preview_generation_id": preview_generation_id,
        },
        "summary": {
            "requirement_count": len(presented_requirements),
            "ai_eligible_count": ai_eligible_count,
            "assessment_target_count": assessment_target_count,
            "info_count": info_count,
            "decision_eligible_count": decision_eligible_count,
            "heading_count": heading_count,
            "human_decision_count": human_decision_count,
            "unassigned_count": unassigned_count,
            "human_coverage": human_coverage,
            "effective_conclusion_count": effective_conclusion_count,
            "missing_effective_conclusion_count": (
                missing_effective_conclusion_count
            ),
            "effective_conclusion_coverage": effective_conclusion_coverage,
            "ai_default_count": ai_default_count,
            "human_override_count": human_override_count,
            "human_native_count": human_native_count,
            "knowledge_doc_count": int(knowledge_docs_count or 0),
            "configured_retrieval_backend": ai_runtime.retrieval_backend,
            "configured_azure_auth_mode": ai_runtime.azure_auth_mode,
            "runnable_ai_profile_count": sum(
                1 for profile in ai_profiles if profile.get("ready")
            ),
            "locked": locked,
            "order_source": str(
                order_diagnostics.get("order_source") or "fallback_page_anchor"
            ),
            "order_mismatch_count": int(
                order_diagnostics.get("order_mismatch_count") or 0
            ),
            "preview_status": str(preview_meta.get("preview_status") or "NOT_READY"),
            "preview_generated_at": str(preview_meta.get("preview_generated_at") or ""),
            "preview_cache_hit": bool(preview_meta.get("preview_cache_hit")),
            "preview_error": str(preview_meta.get("preview_error") or ""),
            "requirement_tree_state": str(
                active_tree_inspection.get("requirement_tree_state")
                or "MISSING"
            ),
            "requirement_tree_has_evidence_bundle": bool(
                active_tree_inspection.get(
                    "requirement_tree_has_evidence_bundle"
                )
            ),
            "lock_basis": lock_basis,
        },
        "actions": {
            "can_lock_extraction": (
                requirement_set_version > 0 and not locked and not lock_blockers
            ),
            "can_export": locked and matrix_ready,
            "can_export_draft": matrix_ready,
            "can_export_formal": locked and not formal_blockers and matrix_ready,
            "can_run_ai_analysis": locked
            and decision_eligible_count > 0
            and any(bool(profile.get("ready")) for profile in ai_profiles),
        },
        "readiness": readiness,
        "ai_profiles": ai_profiles,
        "requirements": presented_requirements,
    }
    if preview is not None:
        workspace["preview_generated_at"] = str(
            preview_meta.get("preview_generated_at") or ""
        )
        workspace["preview"] = preview
        workspace["review_entries"] = review_entries
        workspace["review_entries_raw"] = review_entries_raw
    return workspace

def _present_requirements(
    requirements: list[dict], preview: dict | None = None
) -> list[dict]:
    ordered, _ = _sort_requirements_for_display(requirements, preview=preview)
    presented: list[dict] = []
    for index, row in enumerate(ordered, start=1):
        copied = dict(row)
        copied["source_order"] = (
            _coerce_positive_int(copied.get("source_order")) or index
        )
        copied["display_sort_index"] = index
        copied["display_req_code"] = f"REQ{index:04d}"
        presented.append(copied)
    return presented

def _prepare_requirements_for_display(
    requirements: list[dict],
    *,
    preview: dict | None = None,
) -> tuple[list[dict], dict[str, object]]:
    from app.application.ingestion import (
        _normalize_preview_generation_id,
        _resolve_active_preview_generation_id,
        _row_visual_assets_match_active_generation,
        _strip_preview_visual_assets,
    )

    active_generation_id = _resolve_active_preview_generation_id(preview=preview)
    document_generation_cache: dict[str, str] = {}
    normalized_requirements: list[dict] = []
    for row in requirements:
        if not isinstance(row, dict):
            continue
        copied = dict(row)
        expected_generation_id = active_generation_id
        if not expected_generation_id:
            document_id = str(copied.get("sor_document_id") or "").strip()
            if document_id:
                expected_generation_id = document_generation_cache.get(document_id, "")
                if document_id not in document_generation_cache:
                    document_generation_cache[document_id] = (
                        _normalize_preview_generation_id(
                            (db.get_sor_document(document_id) or {}).get(
                                "preview_generation_id"
                            )
                        )
                    )
                    expected_generation_id = document_generation_cache.get(
                        document_id, ""
                    )
        if not str(
            copied.get("requirement_set_id") or ""
        ).strip() and not _row_visual_assets_match_active_generation(
            copied, expected_generation_id
        ):
            copied = _strip_preview_visual_assets(copied)
        normalized_requirements.append(copied)
    presented = _present_requirements(normalized_requirements, preview=preview)
    diagnostics = _build_requirement_order_diagnostics(presented, preview=preview)
    return presented, diagnostics

def _require_locked_extraction_for_export(
    project_id: str,
    *,
    document: dict | None = None,
    requirements: list[dict] | None = None,
) -> None:
    scoped_documents: list[dict] = []
    if document is not None:
        document_id = str(document.get("id") or "").strip()
        current = db.backfill_sor_document_runtime(document_id) if document_id else None
        scoped_documents.append(current or document)
    else:
        seen_document_ids: set[str] = set()
        for row in requirements or []:
            document_id = str(row.get("sor_document_id") or "").strip()
            if not document_id or document_id in seen_document_ids:
                continue
            seen_document_ids.add(document_id)
            current = db.backfill_sor_document_runtime(
                document_id
            ) or db.get_sor_document(document_id)
            if current and current.get("project_id") == project_id:
                scoped_documents.append(current)

    unlocked_documents = [
        item
        for item in scoped_documents
        if not str(item.get("extraction_locked_at") or "").strip()
    ]
    if not unlocked_documents:
        return

    names = ", ".join(
        str(item.get("file_name") or item.get("id") or "").strip()
        for item in unlocked_documents[:3]
    )
    detail = "Compliance Matrix export requires a locked extraction. Lock the reviewed requirement set before exporting."
    if names:
        detail = f"{detail} Unlocked document(s): {names}."
    raise ApplicationError(status_code=409, detail=detail)

def _requirement_enrichment_publication_trace(row: dict) -> dict[str, object] | None:
    trace = row.get("decision_reference")
    if not isinstance(trace, dict):
        raw_trace = row.get("decision_reference_json")
        if isinstance(raw_trace, str) and raw_trace.strip():
            try:
                decoded = json.loads(raw_trace)
            except json.JSONDecodeError:
                return None
            trace = decoded if isinstance(decoded, dict) else None
    if not isinstance(trace, dict):
        return None
    if str(trace.get("schema_version") or "") != "requirement-enrichment-trace/v1":
        return None

    publication_order = trace.get("publication_order")
    source_order = trace.get("source_order")
    if (
        isinstance(publication_order, bool)
        or not isinstance(publication_order, int)
        or publication_order < 0
        or isinstance(source_order, bool)
        or not isinstance(source_order, int)
        or source_order < 0
    ):
        return None
    publication_key = str(trace.get("publication_key") or "").strip()
    preview_id = str(trace.get("preview_id") or "").strip()
    publication_entry_id = str(trace.get("publication_entry_id") or "").strip()
    parent_publication_entry_id = str(
        trace.get("parent_publication_entry_id") or ""
    ).strip()
    if not publication_key or not preview_id or not publication_entry_id:
        return None
    return {
        "publication_key": publication_key,
        "preview_id": preview_id,
        "publication_entry_id": publication_entry_id,
        "parent_publication_entry_id": parent_publication_entry_id,
        "publication_order": publication_order,
        "source_order": source_order,
    }

def _validated_enrichment_publication_rows(
    requirements: list[dict],
) -> list[tuple[int, int, int, dict]] | None:
    """Return canonical rows only when the complete persisted trace is coherent."""

    if not requirements:
        return None
    traced: list[tuple[int, int, int, dict, dict[str, object]]] = []
    for original_index, row in enumerate(requirements):
        trace = _requirement_enrichment_publication_trace(row)
        if trace is None:
            return None
        traced.append(
            (
                int(trace["publication_order"]),
                int(trace["source_order"]),
                original_index,
                row,
                trace,
            )
        )

    publication_keys = {str(item[4]["publication_key"]) for item in traced}
    preview_ids = {str(item[4]["preview_id"]) for item in traced}
    publication_orders = {item[0] for item in traced}
    entry_ids = {str(item[4]["publication_entry_id"]) for item in traced}
    if (
        len(publication_keys) != 1
        or len(preview_ids) != 1
        or publication_orders != set(range(len(traced)))
        or len(entry_ids) != len(traced)
    ):
        return None

    ordered = sorted(traced, key=lambda item: (item[0], item[2]))
    if [item[1] for item in ordered] != sorted(item[1] for item in ordered):
        return None
    order_by_entry_id = {
        str(item[4]["publication_entry_id"]): item[0] for item in ordered
    }
    for publication_order, _, _, _, trace in ordered:
        parent_id = str(trace["parent_publication_entry_id"])
        if parent_id and (
            parent_id not in order_by_entry_id
            or order_by_entry_id[parent_id] >= publication_order
        ):
            return None
    return [
        (publication_order, source_order, original_index, row)
        for publication_order, source_order, original_index, row, _ in ordered
    ]

def _sort_requirements_for_display(
    requirements: list[dict],
    preview: dict | None = None,
) -> tuple[list[dict], dict[str, object]]:
    preview_order = _build_preview_requirement_order_index(preview)
    if not preview_order:
        enrichment_rows = _validated_enrichment_publication_rows(requirements)
        if enrichment_rows is not None:
            ordered: list[dict] = []
            for display_order, (
                publication_order,
                publication_source_order,
                _,
                row,
            ) in enumerate(enrichment_rows, start=1):
                copied = dict(row)
                copied["preview_rank"] = None
                copied["publication_order"] = publication_order
                copied["publication_source_order"] = publication_source_order
                copied["display_order"] = display_order
                copied["source_order"] = display_order
                ordered.append(copied)
            return ordered, {
                "order_source": "enrichment_publication",
                "order_mismatch_count": 0,
            }
        ranked: list[tuple[tuple, dict]] = []
        for original_index, row in enumerate(requirements):
            copied = dict(row)
            copied["preview_rank"] = None
            ranked.append(
                (_fallback_requirement_display_sort_key(copied, original_index), copied)
            )
        ranked.sort(key=lambda item: item[0])
        ordered = [row for _, row in ranked]
        for display_order, row in enumerate(ordered, start=1):
            row["display_order"] = display_order
            row["source_order"] = display_order
        return ordered, {
            "order_source": "fallback_page_anchor",
            "order_mismatch_count": 0,
        }

    matched: list[tuple[int, int, dict]] = []
    unmatched: list[tuple[int, dict]] = []
    mismatch_count = 0
    for original_index, row in enumerate(requirements):
        preview_rank = _lookup_preview_rank(row, preview_order)
        copied = dict(row)
        if preview_rank < 10**9:
            copied["preview_rank"] = preview_rank
            copied["source_order"] = preview_rank + 1
            matched.append((preview_rank, original_index, copied))
        else:
            copied["preview_rank"] = None
            copied["source_order"] = 0
            unmatched.append((original_index, copied))
            mismatch_count += 1

    matched.sort(key=lambda item: (item[0], item[1]))
    matched_rows = [row for _, _, row in matched]
    last_matched_index_by_owner: dict[str, int] = {}
    for matched_index, row in enumerate(matched_rows):
        for token in _row_owner_section_tokens(row):
            last_matched_index_by_owner[token] = matched_index

    unmatched_after_index: dict[int, list[dict]] = {}
    unmatched_tail: list[dict] = []
    for _, row in unmatched:
        anchor_indices = [
            last_matched_index_by_owner[token]
            for token in _row_owner_section_tokens(row)
            if token in last_matched_index_by_owner
        ]
        if anchor_indices:
            unmatched_after_index.setdefault(max(anchor_indices), []).append(row)
        else:
            unmatched_tail.append(row)

    ordered: list[dict] = []
    for matched_index, row in enumerate(matched_rows):
        ordered.append(row)
        ordered.extend(unmatched_after_index.get(matched_index, []))
    ordered.extend(unmatched_tail)

    for display_order, row in enumerate(ordered, start=1):
        row["display_order"] = display_order
        if row.get("preview_rank") is None:
            row["source_order"] = display_order

    return ordered, {
        "order_source": "review_overlay",
        "order_mismatch_count": mismatch_count,
    }

def _build_requirement_order_diagnostics(
    requirements: list[dict],
    *,
    preview: dict | None = None,
) -> dict[str, object]:
    preview_order = _build_preview_requirement_order_index(preview)
    if not requirements:
        return {
            "order_source": "review_overlay"
            if preview_order
            else "fallback_page_anchor",
            "order_mismatch_count": 0,
        }
    if not preview_order:
        if _validated_enrichment_publication_rows(requirements) is not None:
            return {
                "order_source": "enrichment_publication",
                "order_mismatch_count": 0,
            }
        return {"order_source": "fallback_page_anchor", "order_mismatch_count": 0}
    mismatch_count = sum(
        1 for row in requirements if _lookup_preview_rank(row, preview_order) >= 10**9
    )
    return {"order_source": "review_overlay", "order_mismatch_count": mismatch_count}

def _build_preview_requirement_order_index(preview: dict | None) -> dict[str, object]:
    if not isinstance(preview, dict):
        return {}
    rows = (
        preview.get("complete_requirement_entries_raw")
        or preview.get("complete_requirement_entries")
        or []
    )
    if not isinstance(rows, list):
        return {}
    normalized_rows = [dict(row) for row in rows if isinstance(row, dict)]
    return _build_requirement_row_index(normalized_rows)

def _lookup_preview_rank(row: dict, preview_order: dict[str, object]) -> int:
    if not preview_order:
        return 10**9
    matched = _find_indexed_requirement_row(
        row, preview_order, allow_weak_identifier=True
    )
    if matched is None:
        return 10**9
    rank_by_row_id = preview_order.get("rank_by_row_id")
    if not isinstance(rank_by_row_id, dict):
        return 10**9
    try:
        return int(rank_by_row_id.get(id(matched), 10**9))
    except (TypeError, ValueError):
        return 10**9

def _fallback_requirement_display_sort_key(row: dict, original_index: int) -> tuple:
    page = _coerce_positive_int(row.get("source_page"))
    page_end = _coerce_positive_int(row.get("source_page_end")) or page
    section_key = _natural_sort_key(
        str(
            row.get("owner_section_code")
            or row.get("section_code")
            or row.get("parent_section_code")
            or row.get("owner_section_label")
            or row.get("parent_section_label")
            or ""
        )
    )
    anchor_key = _natural_sort_key(str(row.get("source_anchor") or ""))
    req_code_key = _natural_sort_key(str(row.get("req_code") or ""))
    text_key = _natural_sort_key(
        str(
            row.get("raw_text")
            or row.get("display_text")
            or row.get("requirement_text")
            or ""
        )
    )
    return (
        page if page > 0 else 10**9,
        page_end if page_end > 0 else 10**9,
        section_key,
        anchor_key,
        req_code_key,
        text_key,
        original_index,
    )

def _requirement_match_tokens(row: dict) -> list[str]:
    tokens: list[str] = []
    req_code = str(row.get("req_code") or "").strip()
    if req_code:
        tokens.append(f"req:{req_code}")
        base = _base_req_code(req_code)
        if base:
            tokens.append(f"base:{base}")
    entry_id = str(row.get("entry_id") or "").strip()
    if entry_id:
        tokens.append(f"entry:{entry_id}")
    page = _coerce_positive_int(row.get("source_page"))
    text = str(
        row.get("raw_text")
        or row.get("display_text")
        or row.get("requirement_text")
        or ""
    ).strip()
    if page > 0 and text:
        tokens.append(f"fp:{page}:{_normalize_requirement_match_text(text)}")
    return tokens

def _normalize_requirement_match_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    return normalized[:240]

def _normalize_source_anchor(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())

def _requirement_anchor_match_key(row: dict) -> tuple[int, str] | None:
    page = _coerce_positive_int(row.get("source_page"))
    anchor = _normalize_source_anchor(row.get("source_anchor"))
    if page <= 0 or not anchor:
        return None
    return (page, anchor)

def _requirement_page_text_match_key(row: dict) -> tuple[int, str] | None:
    page = _coerce_positive_int(row.get("source_page"))
    text = _normalize_requirement_match_text(
        str(
            row.get("raw_text")
            or row.get("display_text")
            or row.get("requirement_text")
            or ""
        )
    )
    if page <= 0 or not text:
        return None
    return (page, text)

def _requirement_anchor_text_match_key(row: dict) -> tuple[str, str] | None:
    anchor = _normalize_source_anchor(row.get("source_anchor"))
    text = _normalize_requirement_match_text(
        str(
            row.get("raw_text")
            or row.get("display_text")
            or row.get("requirement_text")
            or ""
        )
    )
    if not anchor or not text:
        return None
    return (anchor, text)

def _requirement_anchor_only_key(row: dict) -> str:
    return _normalize_source_anchor(row.get("source_anchor"))

def _requirement_exact_match_key(row: dict) -> tuple[int, str, str] | None:
    anchor_key = _requirement_anchor_match_key(row)
    text_key = _normalize_requirement_match_text(
        str(
            row.get("raw_text")
            or row.get("display_text")
            or row.get("requirement_text")
            or ""
        )
    )
    if anchor_key is None or not text_key:
        return None
    return (anchor_key[0], anchor_key[1], text_key)

def _row_has_strong_requirement_identity(row: dict) -> bool:
    return bool(
        _requirement_anchor_match_key(row)
        or _requirement_page_text_match_key(row)
        or _requirement_anchor_text_match_key(row)
        or _requirement_anchor_only_key(row)
    )

def _requirement_entry_kind_group(row: dict) -> str:
    return str(row.get("entry_kind") or "").strip().upper()

def _normalized_section_identity_value(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())

def _requirement_structural_identity_keys(row: dict) -> list[str]:
    kind = _requirement_entry_kind_group(row)
    if not kind:
        return []
    section_tokens: list[str] = []
    for field_name in (
        "section_instance_key",
        "section_key",
        "owner_section_code",
        "parent_section_code",
        "section_code",
    ):
        value = _normalized_section_identity_value(row.get(field_name))
        if value and value not in section_tokens:
            prefix = "code" if field_name.endswith("_code") else "section"
            section_tokens.append(f"{prefix}:{value}")
    identifier_tokens: list[str] = []
    entry_id = str(row.get("entry_id") or "").strip()
    if entry_id:
        identifier_tokens.append(f"entry:{entry_id}")
    req_code = str(row.get("req_code") or "").strip()
    if req_code:
        identifier_tokens.append(f"req:{req_code}")
        base_req_code = _base_req_code(req_code)
        if base_req_code:
            identifier_tokens.append(f"base:{base_req_code}")
    if not section_tokens or not identifier_tokens:
        return []
    keys: list[str] = []
    for section_token in section_tokens:
        for identifier_token in identifier_tokens:
            keys.append(f"{kind}|{section_token}|{identifier_token}")
    return keys

def _row_owner_section_tokens(row: dict) -> list[str]:
    tokens: list[str] = []
    for field_name in (
        "section_instance_key",
        "section_key",
        "owner_section_code",
        "owner_section_label",
        "owner_section_title",
        "section_code",
        "parent_section_code",
        "parent_section_label",
        "parent_section_title",
    ):
        value = _normalized_section_identity_value(row.get(field_name))
        if value and value not in tokens:
            tokens.append(value)
    return tokens

def _rows_have_compatible_entry_kind(left: dict, right: dict) -> bool:
    left_kind = _requirement_entry_kind_group(left)
    right_kind = _requirement_entry_kind_group(right)
    if not left_kind or not right_kind:
        return True
    if left_kind == right_kind:
        return True
    pair = {left_kind, right_kind}
    if pair <= {"REQUIREMENT", "HYBRID"}:
        return True
    if pair <= {"HEADING", "HYBRID"}:
        return True
    return False

def _append_requirement_row_to_index(index: dict[str, object], row: dict) -> None:
    rows = index.setdefault("rows", [])
    if isinstance(rows, list):
        rows.append(row)
        rank_by_row_id = index.setdefault("rank_by_row_id", {})
        if isinstance(rank_by_row_id, dict):
            rank_by_row_id[id(row)] = len(rows) - 1

    def _append_bucket(bucket_name: str, key: object) -> None:
        if key in (None, "", ()):
            return
        bucket = index.setdefault(bucket_name, {})
        if not isinstance(bucket, dict):
            return
        bucket.setdefault(key, []).append(row)

    _append_bucket("by_exact", _requirement_exact_match_key(row))
    _append_bucket("by_anchor", _requirement_anchor_match_key(row))
    _append_bucket("by_page_text", _requirement_page_text_match_key(row))
    _append_bucket("by_anchor_text", _requirement_anchor_text_match_key(row))
    _append_bucket("by_anchor_only", _requirement_anchor_only_key(row))
    for identity_key in _requirement_structural_identity_keys(row):
        _append_bucket("by_identity", identity_key)
    entry_id = str(row.get("entry_id") or "").strip()
    if entry_id:
        _append_bucket("by_entry", entry_id)
    req_code = str(row.get("req_code") or "").strip()
    if req_code:
        _append_bucket("by_req", req_code)
        base_req_code = _base_req_code(req_code)
        if base_req_code:
            _append_bucket("by_base_req", base_req_code)

def _build_requirement_row_index(rows: list[dict]) -> dict[str, object]:
    index: dict[str, object] = {
        "rows": [],
        "rank_by_row_id": {},
        "by_identity": {},
        "by_exact": {},
        "by_anchor": {},
        "by_page_text": {},
        "by_anchor_text": {},
        "by_anchor_only": {},
        "by_entry": {},
        "by_req": {},
        "by_base_req": {},
    }
    for row in rows or []:
        if isinstance(row, dict):
            _append_requirement_row_to_index(index, row)
    return index

def _first_unique_index_match(
    index: dict[str, object], bucket_name: str, key: object
) -> dict | None:
    if key in (None, "", ()):
        return None
    bucket = index.get(bucket_name)
    if not isinstance(bucket, dict):
        return None
    matches = bucket.get(key)
    if not isinstance(matches, list) or len(matches) != 1:
        return None
    candidate = matches[0]
    return candidate if isinstance(candidate, dict) else None

def _candidate_supports_weak_requirement_match(target: dict, candidate: dict) -> bool:
    target_exact = _requirement_exact_match_key(target)
    candidate_exact = _requirement_exact_match_key(candidate)
    if target_exact and candidate_exact and target_exact == candidate_exact:
        return True

    target_anchor = _requirement_anchor_match_key(target)
    candidate_anchor = _requirement_anchor_match_key(candidate)
    if target_anchor and candidate_anchor and target_anchor == candidate_anchor:
        return True

    target_page_text = _requirement_page_text_match_key(target)
    candidate_page_text = _requirement_page_text_match_key(candidate)
    if (
        target_page_text
        and candidate_page_text
        and target_page_text == candidate_page_text
    ):
        return True

    target_anchor_text = _requirement_anchor_text_match_key(target)
    candidate_anchor_text = _requirement_anchor_text_match_key(candidate)
    if (
        target_anchor_text
        and candidate_anchor_text
        and target_anchor_text == candidate_anchor_text
    ):
        return True

    target_anchor_only = _requirement_anchor_only_key(target)
    candidate_anchor_only = _requirement_anchor_only_key(candidate)
    if (
        target_anchor_only
        and candidate_anchor_only
        and target_anchor_only == candidate_anchor_only
    ):
        return True

    return not _row_has_strong_requirement_identity(target)

def _find_indexed_requirement_row(
    row: dict,
    index: dict[str, object],
    *,
    allow_weak_identifier: bool = True,
) -> dict | None:
    rows = index.get("rows")
    if not isinstance(rows, list) or not rows:
        return None

    for identity_key in _requirement_structural_identity_keys(row):
        candidate = _first_unique_index_match(index, "by_identity", identity_key)
        if candidate is not None and _rows_have_compatible_entry_kind(row, candidate):
            return candidate

    for bucket_name, key in (
        ("by_exact", _requirement_exact_match_key(row)),
        ("by_anchor", _requirement_anchor_match_key(row)),
        ("by_page_text", _requirement_page_text_match_key(row)),
        ("by_anchor_text", _requirement_anchor_text_match_key(row)),
        ("by_anchor_only", _requirement_anchor_only_key(row)),
    ):
        candidate = _first_unique_index_match(index, bucket_name, key)
        if candidate is not None and _rows_have_compatible_entry_kind(row, candidate):
            return candidate

    if not allow_weak_identifier:
        return None

    seen_candidates: set[int] = set()
    weak_tokens: list[tuple[str, str]] = []
    entry_id = str(row.get("entry_id") or "").strip()
    if entry_id:
        weak_tokens.append(("by_entry", entry_id))
    req_code = str(row.get("req_code") or "").strip()
    if req_code:
        weak_tokens.append(("by_req", req_code))
        base_req_code = _base_req_code(req_code)
        if base_req_code:
            weak_tokens.append(("by_base_req", base_req_code))

    for bucket_name, token in weak_tokens:
        bucket = index.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        for candidate in bucket.get(token, []):
            if not isinstance(candidate, dict):
                continue
            candidate_id = id(candidate)
            if candidate_id in seen_candidates:
                continue
            seen_candidates.add(candidate_id)
            if not _rows_have_compatible_entry_kind(row, candidate):
                continue
            if _candidate_supports_weak_requirement_match(row, candidate):
                return candidate
    return None

def _natural_sort_key(value: str) -> tuple:
    parts = re.split(r"(\d+)", str(value or "").strip().lower())
    key: list[tuple[int, object]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)

def _coerce_positive_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0

def _record_assessment_runs(requirement_id: str, runs: list[dict[str, Any]]) -> None:
    for run in runs:
        if not isinstance(run, dict):
            continue
        db.add_assessment_run(
            requirement_id=requirement_id,
            profile_id=str(run.get("profile_id") or ""),
            provider=str(run.get("provider") or ""),
            mode=str(run.get("mode") or ""),
            retrieval_backend=str(run.get("retrieval_backend") or ""),
            model_name=str(run.get("model_name") or ""),
            prompt_version=str(run.get("prompt_version") or ""),
            input_payload_json=str(run.get("input_payload_json") or "{}"),
            output_payload_json=str(run.get("output_payload_json") or ""),
            validation_status=str(run.get("validation_status") or ""),
            fallback_reason=str(run.get("fallback_reason") or ""),
        )

def _is_ai_eligible_requirement(row: dict) -> bool:
    return str(row.get("entry_kind") or "REQUIREMENT").strip().upper() in {
        "REQUIREMENT",
        "HYBRID",
    }

def _is_info_only_requirement(row: dict) -> bool:
    return str(row.get("analysis_label") or "").strip().upper() == "INFO"

def _is_assessment_target_requirement(row: dict) -> bool:
    return _is_ai_eligible_requirement(row) and not _is_info_only_requirement(row)

def _coerce_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

def _overlay_review_entries(
    rows: object,
    persisted_requirements: list[dict],
    *,
    prefer_raw: bool,
    preview_generation_id: str = "",
) -> list[dict]:
    if not isinstance(rows, list):
        return []
    persisted_index = _build_requirement_row_index(
        [
            dict(item)
            for item in (persisted_requirements or [])
            if isinstance(item, dict)
        ]
    )
    overlaid: list[dict] = []
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        row["preview_rank"] = index
        row["display_order"] = index + 1
        if preview_generation_id:
            row["preview_generation_id"] = preview_generation_id
        persisted = _find_indexed_requirement_row(
            row, persisted_index, allow_weak_identifier=True
        )
        if persisted and not _persisted_review_matches_preview_row(persisted, row):
            persisted = None
        if persisted:
            merged_refs, merged_blocks, merged_has_asset, merged_render_mode = (
                _merge_preview_table_assets_for_review_row(
                    preview_row=row,
                    persisted_row=persisted,
                    active_preview_generation_id=preview_generation_id,
                )
            )
            row["id"] = persisted.get("id")
            row["requirement_set_id"] = persisted.get("requirement_set_id")
            row["keep"] = True
            row["entry_kind"] = (
                str(
                    persisted.get("entry_kind")
                    or row.get("entry_kind")
                    or "REQUIREMENT"
                )
                .strip()
                .upper()
            )
            row["requirement_type"] = str(
                persisted.get("requirement_type") or row.get("requirement_type") or ""
            )
            row["priority"] = str(
                persisted.get("priority") or row.get("priority") or ""
            )
            row["dedupe_version"] = str(
                persisted.get("dedupe_version") or row.get("dedupe_version") or ""
            )
            row["table_refs"] = merged_refs
            row["content_blocks"] = merged_blocks
            row["has_table_asset"] = merged_has_asset
            row["table_render_mode"] = merged_render_mode
            row["status"] = persisted.get("status")
            row["ai_decision"] = persisted.get("ai_decision")
            row["compliance_status"] = persisted.get("compliance_status")
            row["ai_reason"] = persisted.get("ai_reason")
            row["ai_confidence"] = persisted.get("ai_confidence")
            row["assessment_mode"] = persisted.get("assessment_mode")
            row["analysis_label"] = persisted.get("analysis_label")
            row["analysis_basis"] = persisted.get("analysis_basis")
            row["normalized_requirement"] = persisted.get("normalized_requirement")
            row["risk_level"] = persisted.get("risk_level")
            row["clarification_question"] = persisted.get("clarification_question")
            row["evidence_findings"] = persisted.get(
                "evidence_findings", []
            )
            row["structured_information_gap"] = persisted.get(
                "structured_information_gap", {}
            )
            row["sub_judgments"] = persisted.get("sub_judgments", [])
            row["final_decision"] = persisted.get("final_decision")
            row["engineer_reason"] = persisted.get("engineer_reason")
            row["evidences"] = persisted.get("evidences", [])
        else:
            row["keep"] = False
            row["entry_kind"] = (
                str(row.get("entry_kind") or "REQUIREMENT").strip().upper()
                or "REQUIREMENT"
            )
        if prefer_raw:
            row["requirement_text"] = str(
                row.get("raw_text")
                or row.get("requirement_text")
                or row.get("display_text")
                or ""
            )
        else:
            row["requirement_text"] = str(
                row.get("display_text")
                or row.get("requirement_text")
                or row.get("raw_text")
                or ""
            )
        if row.get("table_refs") or row.get("content_blocks"):
            row = _refresh_enriched_entry_text_fields(row)
        row["entry_id"] = (
            row.get("entry_id") or row.get("req_code") or f"ENTRY-{index + 1}"
        )
        overlaid.append(row)
    return overlaid

def _merge_preview_table_assets_for_review_row(
    *,
    preview_row: dict,
    persisted_row: dict,
    active_preview_generation_id: str = "",
) -> tuple[list[dict], list[dict], bool, str]:
    from app.application.ingestion import _row_visual_assets_match_active_generation

    preview_refs = [
        dict(item)
        for item in (preview_row.get("table_refs") or [])
        if isinstance(item, dict)
    ]
    persisted_refs = (
        [
            dict(item)
            for item in (persisted_row.get("table_refs") or [])
            if isinstance(item, dict)
        ]
        if _row_visual_assets_match_active_generation(
            persisted_row, active_preview_generation_id
        )
        else []
    )
    merged_refs: list[dict]

    if preview_refs:
        persisted_ref_by_id = {
            str(item.get("table_id") or "").strip(): item
            for item in persisted_refs
            if str(item.get("table_id") or "").strip()
        }
        merged_refs = []
        for ref in preview_refs:
            merged = dict(ref)
            table_id = str(ref.get("table_id") or "").strip()
            persisted_ref = persisted_ref_by_id.get(table_id)
            if persisted_ref:
                export_variant = (
                    str(persisted_ref.get("export_variant") or "").strip().lower()
                )
                recommended = (
                    str(merged.get("recommended_export_variant") or "").strip().lower()
                )
                override_marker = persisted_ref.get("export_variant_overridden")
                should_preserve_variant = export_variant in {"slice", "source"} and (
                    bool(override_marker)
                    or (
                        override_marker is None
                        and recommended in {"slice", "source"}
                        and export_variant != recommended
                    )
                )
                if should_preserve_variant:
                    merged["export_variant"] = export_variant
                    if recommended in {"slice", "source"}:
                        merged["export_variant_overridden"] = (
                            export_variant != recommended
                        )
            merged_refs.append(merged)
    else:
        merged_refs = persisted_refs

    preview_blocks = (
        preview_row.get("content_blocks")
        if isinstance(preview_row.get("content_blocks"), list)
        else []
    )
    persisted_blocks = (
        persisted_row.get("content_blocks")
        if _row_visual_assets_match_active_generation(
            persisted_row, active_preview_generation_id
        )
        and isinstance(persisted_row.get("content_blocks"), list)
        else []
    )
    merged_blocks = preview_blocks or persisted_blocks or []
    persisted_has_asset = (
        bool(persisted_row.get("has_table_asset"))
        if _row_visual_assets_match_active_generation(
            persisted_row, active_preview_generation_id
        )
        else False
    )
    merged_has_asset = (
        bool(preview_row.get("has_table_asset"))
        if preview_row.get("has_table_asset") is not None
        else persisted_has_asset
    )
    persisted_render_mode = (
        str(persisted_row.get("table_render_mode") or "").strip()
        if _row_visual_assets_match_active_generation(
            persisted_row, active_preview_generation_id
        )
        else ""
    )
    merged_render_mode = str(
        preview_row.get("table_render_mode") or persisted_render_mode or ""
    ).strip()
    if merged_refs:
        merged_has_asset = True
    return merged_refs, merged_blocks, merged_has_asset, merged_render_mode

def _persisted_review_matches_preview_row(persisted: dict, row: dict) -> bool:
    persisted_exact = _requirement_exact_match_key(persisted)
    preview_exact = _requirement_exact_match_key(row)
    if persisted_exact and preview_exact and persisted_exact == preview_exact:
        return True

    persisted_anchor = _requirement_anchor_match_key(persisted)
    preview_anchor = _requirement_anchor_match_key(row)
    if persisted_anchor and preview_anchor and persisted_anchor == preview_anchor:
        return True

    persisted_page_text = _requirement_page_text_match_key(persisted)
    preview_page_text = _requirement_page_text_match_key(row)
    if (
        persisted_page_text
        and preview_page_text
        and persisted_page_text == preview_page_text
    ):
        return True

    persisted_anchor_text = _requirement_anchor_text_match_key(persisted)
    preview_anchor_text = _requirement_anchor_text_match_key(row)
    if (
        persisted_anchor_text
        and preview_anchor_text
        and persisted_anchor_text == preview_anchor_text
    ):
        return True

    persisted_anchor_only = _requirement_anchor_only_key(persisted)
    preview_anchor_only = _requirement_anchor_only_key(row)
    if (
        persisted_anchor_only
        and preview_anchor_only
        and persisted_anchor_only == preview_anchor_only
    ):
        return True
    return False

def _build_workspace_requirements_from_review_entries(
    rows: list[dict],
    *,
    fallback_requirements: list[dict],
    preview: dict | None,
) -> tuple[list[dict], dict[str, object]]:
    from app.application.ingestion import (
        _resolve_active_preview_generation_id,
        _row_visual_assets_match_active_generation,
        _strip_preview_visual_assets,
    )

    active_generation_id = _resolve_active_preview_generation_id(preview=preview)
    current_rows: list[dict] = []
    for row in rows or []:
        if not bool(row.get("keep", True)):
            continue
        copied = hydrate_complex_row_metadata(dict(row))
        if not _row_visual_assets_match_active_generation(copied, active_generation_id):
            copied = _strip_preview_visual_assets(copied)
        copied["requirement_text"] = str(
            row.get("raw_text")
            or row.get("requirement_text")
            or row.get("display_text")
            or ""
        ).strip()
        copied["raw_text"] = str(
            row.get("raw_text") or copied["requirement_text"] or ""
        ).strip()
        copied["display_text"] = str(
            row.get("display_text") or copied["raw_text"] or ""
        ).strip()
        copied["table_refs"] = row.get("table_refs") or []
        copied["content_blocks"] = row.get("content_blocks") or []
        copied["has_table_asset"] = bool(row.get("has_table_asset"))
        copied["table_render_mode"] = str(row.get("table_render_mode") or "")
        current_rows.append(copied)
    current_index = _build_requirement_row_index(current_rows)
    active_complex_groups = [
        row for row in current_rows if is_complex_table_row(row, include_summary=False)
    ]
    for row in fallback_requirements or []:
        if (
            not _is_ai_eligible_requirement(row)
            and str(row.get("entry_kind") or "").upper() != "HEADING"
        ):
            continue
        if row_is_shadowed_by_complex_group(row, active_complex_groups):
            continue
        if (
            _find_indexed_requirement_row(
                row, current_index, allow_weak_identifier=True
            )
            is not None
        ):
            continue
        copied = hydrate_complex_row_metadata(dict(row))
        if not _row_visual_assets_match_active_generation(copied, active_generation_id):
            copied = _strip_preview_visual_assets(copied)
        copied["requirement_text"] = str(
            copied.get("raw_text")
            or copied.get("requirement_text")
            or copied.get("display_text")
            or ""
        ).strip()
        copied["raw_text"] = str(
            copied.get("raw_text") or copied["requirement_text"] or ""
        ).strip()
        copied["display_text"] = str(
            copied.get("display_text") or copied["raw_text"] or ""
        ).strip()
        copied["table_refs"] = copied.get("table_refs") or []
        copied["content_blocks"] = copied.get("content_blocks") or []
        copied["has_table_asset"] = bool(copied.get("has_table_asset"))
        copied["table_render_mode"] = str(copied.get("table_render_mode") or "")
        current_rows.append(copied)
        _append_requirement_row_to_index(current_index, copied)
    if not current_rows:
        return _prepare_requirements_for_display(fallback_requirements, preview=preview)
    return _prepare_requirements_for_display(current_rows, preview=preview)

def _base_req_code(value: object) -> str:
    import re

    token = str(value or "").strip()
    if not token:
        return ""
    return re.sub(r"-D\d+$", "", token, flags=re.I)

def _normalize_review_entry_payload(
    item: ReviewedRequirementEntry,
) -> dict[str, object]:
    row = item.model_dump()
    display_text = str(
        row.get("display_text") or row.get("requirement_text") or ""
    ).strip()
    raw_text = str(
        row.get("raw_text") or row.get("requirement_text") or display_text
    ).strip()
    preferred_text = raw_text or display_text
    normalized = {
        "entry_id": str(row.get("entry_id") or row.get("req_code") or "").strip(),
        "req_code": str(row.get("req_code") or "").strip(),
        "keep": bool(row.get("keep", True)),
        "entry_kind": str(row.get("entry_kind") or "REQUIREMENT").strip().upper()
        or "REQUIREMENT",
        "section_code": str(row.get("section_code") or "").strip(),
        "section_title": str(row.get("section_title") or "").strip(),
        "section_label": str(row.get("section_label") or "").strip(),
        "section_key": str(row.get("section_key") or "").strip(),
        "section_instance_key": str(row.get("section_instance_key") or "").strip(),
        "source_page": row.get("source_page"),
        "source_page_end": row.get("source_page_end"),
        "source_anchor": str(row.get("source_anchor") or "Unknown").strip()
        or "Unknown",
        "display_text": preferred_text,
        "raw_text": preferred_text,
        "requirement_type": str(row.get("requirement_type") or "FUNCTIONAL").strip()
        or "FUNCTIONAL",
        "priority": str(row.get("priority") or "MEDIUM").strip() or "MEDIUM",
        "text_relation": str(row.get("text_relation") or "distinct").strip()
        or "distinct",
        "is_deduped": bool(row.get("is_deduped")),
        "heading_text": str(row.get("heading_text") or "").strip(),
        "parent_section_code": str(row.get("parent_section_code") or "").strip(),
        "parent_section_title": str(row.get("parent_section_title") or "").strip(),
        "parent_section_label": str(row.get("parent_section_label") or "").strip(),
        "owner_section_code": str(row.get("owner_section_code") or "").strip(),
        "owner_section_title": str(row.get("owner_section_title") or "").strip(),
        "owner_section_label": str(row.get("owner_section_label") or "").strip(),
        "structural_parent_section_code": str(
            row.get("structural_parent_section_code") or ""
        ).strip(),
        "structural_parent_section_key": str(
            row.get("structural_parent_section_key") or ""
        ).strip(),
        "structural_parent_section_title": str(
            row.get("structural_parent_section_title") or ""
        ).strip(),
        "structural_parent_section_label": str(
            row.get("structural_parent_section_label") or ""
        ).strip(),
        "merged_from_req_codes": row.get("merged_from_req_codes") or [],
        "merged_from_entry_ids": row.get("merged_from_entry_ids") or [],
        "table_refs": row.get("table_refs") or [],
        "content_blocks": row.get("content_blocks") or [],
        "has_table_asset": bool(row.get("has_table_asset")),
        "table_render_mode": str(row.get("table_render_mode") or "").strip(),
        "dedupe_version": str(
            row.get("dedupe_version") or "heading_requirement_v1"
        ).strip()
        or "heading_requirement_v1",
        **_copy_complex_row_fields(row),
    }
    if is_complex_table_row(normalized):
        return normalize_preserved_entry_table_assets(normalized)
    enriched, _ = enrich_entry_with_table_assets(
        normalized,
        document_id="preview",
        snapshot_root=_table_snapshot_root("preview"),
    )
    normalized.update(
        {
            "table_refs": enriched.get("table_refs") or [],
            "content_blocks": enriched.get("content_blocks") or [],
            "has_table_asset": bool(enriched.get("has_table_asset")),
            "table_render_mode": str(enriched.get("table_render_mode") or ""),
        }
    )
    return hydrate_complex_row_metadata(normalized)

def _find_requirement_for_review_entry(
    requirements: list[dict], normalized: dict[str, object]
) -> dict | None:
    # A disposition change (REQUIREMENT -> REFERENCE/EXCLUDED or back) must
    # keep the immutable Requirement Set entry identity.  Match that identity
    # before applying content/kind heuristics; otherwise the kind transition
    # itself makes the row appear unrelated and the save silently becomes a
    # no-op.
    entry_id = str(normalized.get("entry_id") or "").strip()
    if entry_id:
        identity_matches = [
            row
            for row in requirements
            if entry_id
            in {
                str(row.get("id") or "").strip(),
                str(row.get("entry_id") or "").strip(),
                str(row.get("requirement_id") or "").strip(),
            }
        ]
        if len(identity_matches) == 1:
            return identity_matches[0]

    req_code = str(normalized.get("req_code") or "").strip()
    if req_code:
        code_matches = [
            row
            for row in requirements
            if str(row.get("req_code") or "").strip() == req_code
        ]
        if len(code_matches) == 1:
            return code_matches[0]

    requirement_index = _build_requirement_row_index(
        [dict(row) for row in requirements if isinstance(row, dict)]
    )
    return _find_indexed_requirement_row(
        dict(normalized), requirement_index, allow_weak_identifier=True
    )

def _build_saved_review_entry(
    normalized: dict[str, object], persisted: dict | None, *, keep: bool
) -> dict[str, object]:
    persisted = persisted or {}
    entry_id = str(
        normalized.get("entry_id")
        or persisted.get("req_code")
        or persisted.get("id")
        or ""
    ).strip()
    actual_req_code = str(
        persisted.get("req_code") or normalized.get("req_code") or ""
    ).strip()
    display_text = str(
        persisted.get("display_text")
        or normalized.get("display_text")
        or persisted.get("requirement_text")
        or normalized.get("raw_text")
        or ""
    ).strip()
    raw_text = str(
        persisted.get("raw_text")
        or normalized.get("raw_text")
        or persisted.get("requirement_text")
        or display_text
    ).strip()
    source_page = persisted.get("source_page")
    if source_page in ("", None):
        source_page = normalized.get("source_page")
    source_page_end = normalized.get("source_page_end")
    if source_page_end in ("", None):
        source_page_end = source_page
    saved = {
        "entry_id": entry_id or actual_req_code,
        "req_code": actual_req_code,
        "requirement_set_id": str(persisted.get("requirement_set_id") or "").strip(),
        "keep": bool(keep),
        "entry_kind": str(
            persisted.get("entry_kind") or normalized.get("entry_kind") or "REQUIREMENT"
        )
        .strip()
        .upper()
        or "REQUIREMENT",
        "section_code": str(
            persisted.get("section_code") or normalized.get("section_code") or ""
        ).strip(),
        "section_title": str(
            persisted.get("section_title") or normalized.get("section_title") or ""
        ).strip(),
        "section_label": str(
            persisted.get("section_label") or normalized.get("section_label") or ""
        ).strip(),
        "section_key": str(
            persisted.get("section_key") or normalized.get("section_key") or ""
        ).strip(),
        "section_instance_key": str(
            persisted.get("section_instance_key")
            or normalized.get("section_instance_key")
            or ""
        ).strip(),
        "source_page": source_page,
        "source_page_end": source_page_end,
        "source_anchor": str(
            persisted.get("source_anchor")
            or normalized.get("source_anchor")
            or "Unknown"
        ).strip()
        or "Unknown",
        "requirement_text": raw_text or display_text,
        "display_text": display_text or raw_text,
        "raw_text": raw_text or display_text,
        "requirement_type": str(
            persisted.get("requirement_type")
            or normalized.get("requirement_type")
            or "FUNCTIONAL"
        ).strip()
        or "FUNCTIONAL",
        "priority": str(
            persisted.get("priority") or normalized.get("priority") or "MEDIUM"
        ).strip()
        or "MEDIUM",
        "text_relation": str(
            persisted.get("text_relation")
            or normalized.get("text_relation")
            or "distinct"
        ).strip()
        or "distinct",
        "is_deduped": bool(
            persisted.get("is_deduped")
            if persisted.get("is_deduped") is not None
            else normalized.get("is_deduped")
        ),
        "heading_text": str(
            persisted.get("heading_text") or normalized.get("heading_text") or ""
        ).strip(),
        "parent_section_code": str(
            persisted.get("parent_section_code")
            or normalized.get("parent_section_code")
            or ""
        ).strip(),
        "parent_section_title": str(
            persisted.get("parent_section_title")
            or normalized.get("parent_section_title")
            or ""
        ).strip(),
        "parent_section_label": str(
            persisted.get("parent_section_label")
            or normalized.get("parent_section_label")
            or ""
        ).strip(),
        "owner_section_code": str(
            persisted.get("owner_section_code")
            or normalized.get("owner_section_code")
            or ""
        ).strip(),
        "owner_section_title": str(
            persisted.get("owner_section_title")
            or normalized.get("owner_section_title")
            or ""
        ).strip(),
        "owner_section_label": str(
            persisted.get("owner_section_label")
            or normalized.get("owner_section_label")
            or ""
        ).strip(),
        "structural_parent_section_code": str(
            persisted.get("structural_parent_section_code")
            or normalized.get("structural_parent_section_code")
            or ""
        ).strip(),
        "structural_parent_section_key": str(
            persisted.get("structural_parent_section_key")
            or normalized.get("structural_parent_section_key")
            or ""
        ).strip(),
        "structural_parent_section_title": str(
            persisted.get("structural_parent_section_title")
            or normalized.get("structural_parent_section_title")
            or ""
        ).strip(),
        "structural_parent_section_label": str(
            persisted.get("structural_parent_section_label")
            or normalized.get("structural_parent_section_label")
            or ""
        ).strip(),
        "merged_from_req_codes": persisted.get("merged_from_req_codes")
        if isinstance(persisted.get("merged_from_req_codes"), list)
        else list(normalized.get("merged_from_req_codes") or []),
        "merged_from_entry_ids": persisted.get("merged_from_entry_ids")
        if isinstance(persisted.get("merged_from_entry_ids"), list)
        else list(normalized.get("merged_from_entry_ids") or []),
        "dedupe_version": str(
            persisted.get("dedupe_version") or normalized.get("dedupe_version") or ""
        ).strip(),
        "table_refs": persisted.get("table_refs")
        if isinstance(persisted.get("table_refs"), list)
        else list(normalized.get("table_refs") or []),
        "content_blocks": persisted.get("content_blocks")
        if isinstance(persisted.get("content_blocks"), list)
        else list(normalized.get("content_blocks") or []),
        "has_table_asset": bool(
            persisted.get("has_table_asset")
            if persisted.get("has_table_asset") is not None
            else normalized.get("has_table_asset")
        ),
        "table_render_mode": str(
            persisted.get("table_render_mode")
            or normalized.get("table_render_mode")
            or ""
        ).strip(),
    }
    saved.update(_copy_complex_row_fields(normalized))
    return hydrate_complex_row_metadata(saved)
