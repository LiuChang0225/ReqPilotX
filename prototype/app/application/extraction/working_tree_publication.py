from __future__ import annotations

import hashlib
import json
import re
import uuid
from threading import RLock
from typing import Any

from app import db
from app.application.errors import ApplicationError
from app.application.extraction.entry_classification_overlay import (
    apply_accepted_entry_classification_overlay,
    carry_entry_classifications_to_published_tree,
)
from app.application.extraction.entry_classification_repository import (
    EntryClassificationSnapshotCorrupted,
    entry_classification_lineage_values,
    get_entry_classification_snapshot_or_initial,
)
from app.application.extraction.processing_pipeline import (
    LOCAL_STRUCTURE_PROJECTION_MODE,
)
from app.application.extraction.requirement_tree import (
    build_requirement_tree_snapshot,
)
from app.application.extraction.requirement_tree_snapshots import (
    RequirementTreeSnapshotError,
    delete_requirement_set_tree_object,
    load_requirement_set_tree_snapshot,
    persist_requirement_tree_snapshot,
)
from app.application.transport import ResponseSink
from app.domain.entry_classification import (
    EntryClassificationStatus,
    build_initial_entry_classification_snapshot,
)
from app.domain.extraction.requirement_tree import (
    RequirementTreeMode,
    RequirementTreeNodeV1,
    RequirementTreeSnapshotV1,
    RequirementTreeState,
    canonical_requirement_tree_sha256,
)

_WORKING_TREE_PUBLICATION_LOCK = RLock()


def _load_current_unclassified_working_tree(
    project_id: str,
    document_id: str,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    RequirementTreeSnapshotV1,
]:
    from app.application.extraction.document_jobs import (
        _llm_extraction_result_matches_runtime,
    )
    from app.application.extraction.review_support import (
        _require_project_document,
    )
    from app.application.extraction.structure_workspace import (
        _rebuild_structure_revision_context,
        _serialize_structure_workbench_for_api,
        _structure_agent_job_payload,
    )

    document = _require_project_document(project_id, document_id)
    job, payload, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=True,
    )
    if str(payload.get("mode") or "") != LOCAL_STRUCTURE_PROJECTION_MODE:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "working_tree_publication_requires_local_structure",
                "message": (
                    "当前结果包含独立的语义审阅链路，请从该链路生成 DRAFT。"
                ),
            },
        )
    if not _llm_extraction_result_matches_runtime(payload):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "working_tree_runtime_binding_mismatch",
                "message": "当前需求树需要重新提取后才能生成 DRAFT。",
            },
        )

    # Rebuild and validate the immutable SourceIR/base/revision chain before
    # any publication write.  The user-visible Working tree identity itself
    # must still come from the same durable presentation read model used by
    # Structure GET and entry classification.  Re-projecting presentation
    # fields from the replayed SourceIR here can produce a different canonical
    # tree even though the validated source and effective Structure identities
    # are unchanged, leaving an exact ACCEPTED classification unreachable.
    _validated_source_ir, _validated_base_tree, revisions = (
        _rebuild_structure_revision_context(
            document=document,
            agent=agent,
        )
    )
    from app.application.extraction.table_projection_overrides import (
        apply_table_projection_overrides,
    )

    payload = apply_table_projection_overrides(
        project_id=project_id,
        document_id=document_id,
        source_sha256=str(document.get("source_sha256") or ""),
        payload=payload,
    )
    workbench = _serialize_structure_workbench_for_api(
        project_id=project_id,
        document_id=document_id,
        document=document,
        job=job,
        payload=payload,
        agent=agent,
        revisions=revisions,
        # The exact current table revisions were already overlaid above.  Do
        # not read and project them a second time inside the serializer.
        table_projection_revision_heads=[],
    )
    raw_working_tree = workbench.get("requirement_tree")
    try:
        working_tree = RequirementTreeSnapshotV1.model_validate(
            raw_working_tree
        )
    except (TypeError, ValueError) as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "working_tree_presentation_unavailable",
                "message": (
                    "当前需求树展示投影无法验证，请重新读取或重新提取后重试。"
                ),
            },
        ) from exc
    return document, job, payload, workbench, working_tree


def _working_tree_source_structure_identity(
    working_tree: RequirementTreeSnapshotV1,
) -> tuple[str, str, str]:
    return (
        working_tree.source_identity.source_sha256,
        canonical_requirement_tree_sha256(working_tree),
        working_tree.structure_identity.effective_snapshot_id,
    )


def _assert_working_tree_still_current(
    accepted_tree: RequirementTreeSnapshotV1,
    current_tree: RequirementTreeSnapshotV1,
) -> None:
    if _working_tree_source_structure_identity(
        current_tree
    ) == _working_tree_source_structure_identity(accepted_tree):
        return
    raise ApplicationError(
        status_code=409,
        detail={
            "code": "working_tree_identity_changed",
            "message": (
                "需求结构已在生成确认清单的过程中发生变化；"
                "请刷新结构与清单分类后重试。"
            ),
        },
    )


def _reload_and_assert_working_tree_still_current(
    project_id: str,
    document_id: str,
    accepted_tree: RequirementTreeSnapshotV1,
) -> None:
    (
        _document,
        _structure_job,
        _payload,
        _workbench,
        current_tree,
    ) = _load_current_unclassified_working_tree(project_id, document_id)
    _assert_working_tree_still_current(accepted_tree, current_tree)


def _publication_currentness_generation(
    project_id: str,
    document_id: str,
) -> int:
    generation = db.get_sor_document_publication_currentness_generation(
        project_id,
        document_id,
    )
    if generation is None:
        raise ApplicationError(
            status_code=404,
            detail="SOR document not found for this project.",
        )
    return generation


def publish_working_requirement_tree_draft(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    """Freeze reviewable structure proxies without inventing semantic truth."""

    from app.application.extraction.structure_workspace import (
        _STRUCTURE_MUTATION_LOCK,
    )
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    response.headers.update(_ASSET_RESPONSE_HEADERS)
    with _WORKING_TREE_PUBLICATION_LOCK, _STRUCTURE_MUTATION_LOCK:
        currentness_before_load = _publication_currentness_generation(
            project_id,
            document_id,
        )
        (
            _document,
            _structure_job,
            payload,
            workbench,
            unclassified_working_tree,
        ) = _load_current_unclassified_working_tree(
            project_id,
            document_id,
        )
        publication_currentness_generation = (
            _publication_currentness_generation(project_id, document_id)
        )
        if publication_currentness_generation != currentness_before_load:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "working_tree_identity_changed",
                    "message": (
                        "需求结构已在生成确认清单的过程中发生变化；"
                        "请刷新结构与清单分类后重试。"
                    ),
                },
            )
        classification_initial = build_initial_entry_classification_snapshot(
            unclassified_working_tree
        )
        try:
            classification_snapshot = (
                get_entry_classification_snapshot_or_initial(
                    classification_initial
                )
            )
        except EntryClassificationSnapshotCorrupted as exc:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "entry_classification_snapshot_corrupted",
                    "message": (
                        "当前清单分类记录无法验证，请刷新清单后重试。"
                    ),
                },
            ) from exc
        if (
            classification_snapshot.status
            is not EntryClassificationStatus.ACCEPTED
        ):
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "entry_classification_acceptance_required",
                    "message": (
                        "请先完成清单分类并接受当前结果，再生成确认清单。"
                    ),
                },
            )
        try:
            working_tree = apply_accepted_entry_classification_overlay(
                unclassified_working_tree,
                classification_snapshot,
            )
        except ValueError as exc:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "entry_classification_identity_mismatch",
                    "message": (
                        "清单分类与当前需求结构树不一致，请刷新后重试。"
                    ),
                },
            ) from exc
        publishable_node_ids = {
            node.id
            for node in working_tree.nodes
            if node.projection_role
            in {
                "STRUCTURE_PROXY",
                "DIRECT_CONTENT",
                "WHOLE_TABLE_REVIEW",
                "TABLE_RECORD",
                "REQUIREMENT",
            }
            and node.include_in_view
        }
        node_by_id = {node.id: node for node in working_tree.nodes}
        requirement_nodes = tuple(
            node_by_id[node_id]
            for node_id in _requirement_tree_preorder_ids(working_tree)
            if node_id in publishable_node_ids
        )
        if (
            working_tree.state is not RequirementTreeState.READY
            or not requirement_nodes
        ):
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "working_tree_not_publishable",
                    "message": "需求结构树尚未完整就绪，不能生成不完整 DRAFT。",
                    "state": working_tree.state.value,
                },
            )

        working_tree_sha256 = canonical_requirement_tree_sha256(working_tree)
        publication_key = _working_tree_publication_key(
            working_tree,
            entry_classification_snapshot_id=(
                classification_snapshot.snapshot_id
            ),
            entry_classification_snapshot_sha256=(
                classification_snapshot.payload_sha256
            ),
        )
        # A legacy DRAFT has no accepted three-class receipt and therefore
        # cannot be claimed for a W3 publication, even if its tree bytes happen
        # to match after presentation fields are ignored.
        requirement_set: dict[str, Any] = {}
        requirement_set_created = False
        publication_owner_token = (
            db.new_requirement_set_publication_owner_token()
        )
        if not requirement_set:
            _reload_and_assert_working_tree_still_current(
                project_id,
                document_id,
                unclassified_working_tree,
            )
            try:
                requirement_set, requirement_set_created = (
                    db.create_or_get_requirement_set_for_publication(
                        project_id,
                        document_id,
                        publication_key=publication_key,
                        source_hash=(
                            working_tree.source_identity.source_sha256
                        ),
                        preview_generation_id=(
                            working_tree.source_identity.preview_generation_id
                        ),
                        entry_classification_snapshot_id=(
                            classification_snapshot.snapshot_id
                        ),
                        entry_classification_snapshot_sha256=(
                            classification_snapshot.payload_sha256
                        ),
                        entry_classification_lineage=(
                            entry_classification_lineage_values(
                                classification_snapshot
                            )
                        ),
                        publication_owner_token=publication_owner_token,
                        expected_publication_currentness_generation=(
                            publication_currentness_generation
                        ),
                    )
                )
            except db.RequirementSetPublicationCurrentnessConflict as exc:
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": "working_tree_identity_changed",
                        "message": (
                            "需求结构已在生成确认清单的过程中发生变化；"
                            "请刷新结构与清单分类后重试。"
                        ),
                    },
                ) from exc
            except db.RequirementSetPublicationConflict as exc:
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": "entry_classification_identity_changed",
                        "message": (
                            "清单分类已在生成过程中发生变化；"
                            "请刷新并重新接受当前结果。"
                        ),
                    },
                ) from exc
        requirement_set_id = str(requirement_set.get("id") or "")
        if not requirement_set_id:
            raise ApplicationError(
                status_code=500,
                detail={
                    "code": "working_tree_draft_creation_failed",
                    "message": "无法创建 DRAFT 版本。",
                },
            )
        owns_publication = bool(
            str(requirement_set.get("publication_owner_token") or "")
            == publication_owner_token
            and str(requirement_set.get("publication_state") or "").upper()
            == "PUBLISHING"
        )
        if requirement_set_created and str(
            requirement_set.get("publication_owner_token") or ""
        ) not in {"", publication_owner_token}:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "working_tree_publication_ownership_changed",
                    "message": "DRAFT 发布所有权已变化，请重新读取后重试。",
                },
            )
        if not requirement_set_created and not owns_publication:
            existing_response = _existing_publication_response(
                project_id=project_id,
                document_id=document_id,
                requirement_set=requirement_set,
                publication_key=publication_key,
                working_tree_sha256=working_tree_sha256,
                working_tree=working_tree,
                entry_classification_snapshot_id=(
                    classification_snapshot.snapshot_id
                ),
                entry_classification_snapshot_sha256=(
                    classification_snapshot.payload_sha256
                ),
            )
            if (
                str(
                    existing_response.get("publication", {}).get(
                        "publication_state"
                    )
                    or ""
                ).upper()
                == "READY"
            ):
                _reload_and_assert_working_tree_still_current(
                    project_id,
                    document_id,
                    unclassified_working_tree,
                )
            return existing_response

        try:
            published_entries = _persist_local_requirement_entries(
                project_id=project_id,
                document_id=document_id,
                requirement_set_id=requirement_set_id,
                preview_generation_id=(
                    working_tree.source_identity.preview_generation_id
                ),
                working_tree=working_tree,
                requirement_nodes=requirement_nodes,
                publication_key=publication_key,
                publication_owner_token=(
                    publication_owner_token if owns_publication else ""
                ),
            )
            snapshot = build_requirement_tree_snapshot(
                workbench=workbench,
                payload=payload,
                mode=RequirementTreeMode.REQUIREMENT_SET,
                requirement_set_id=requirement_set_id,
                requirement_entries=published_entries,
                forced_state=RequirementTreeState.READY,
            )
            snapshot = carry_entry_classifications_to_published_tree(
                working_tree,
                snapshot,
            )
            if (
                _working_tree_publication_key(
                    snapshot,
                    entry_classification_snapshot_id=(
                        classification_snapshot.snapshot_id
                    ),
                    entry_classification_snapshot_sha256=(
                        classification_snapshot.payload_sha256
                    ),
                )
                != publication_key
            ):
                raise RequirementTreeSnapshotError(
                    "published DRAFT changed the canonical publication projection"
                )
            _assert_same_tree_projection(working_tree, snapshot)
            if owns_publication:
                heartbeat = db.heartbeat_requirement_set_publication(
                    project_id,
                    document_id,
                    requirement_set_id,
                    publication_owner_token=publication_owner_token,
                )
                if not heartbeat:
                    raise db.RequirementSetMutationBlocked(
                        "Requirement Set publication lease was lost."
                    )
            persist_requirement_tree_snapshot(
                requirement_set_id=requirement_set_id,
                snapshot=snapshot,
                publication_owner_token=(
                    publication_owner_token if owns_publication else ""
                ),
                expected_publication_currentness_generation=(
                    publication_currentness_generation
                ),
                before_attach=lambda: (
                    _reload_and_assert_working_tree_still_current(
                        project_id,
                        document_id,
                        unclassified_working_tree,
                    )
                ),
            )
        except Exception as exc:
            # First atomically prove and remove the exact inactive DRAFT. A
            # retry worker may already have activated the READY publication;
            # its immutable tree must never be deleted by this failed owner.
            if owns_publication:
                discarded = db.discard_inactive_requirement_set(
                    document_id,
                    requirement_set_id,
                    project_id=project_id,
                    expected_publication_state="PUBLISHING",
                    publication_owner_token=publication_owner_token,
                )
            else:
                discarded = db.discard_inactive_requirement_set(
                    document_id,
                    requirement_set_id,
                    project_id=project_id,
                )
            if discarded:
                try:
                    delete_requirement_set_tree_object(
                        project_id=project_id,
                        document_id=document_id,
                        requirement_set_id=requirement_set_id,
                    )
                except (OSError, RuntimeError, ValueError):
                    pass
            if isinstance(exc, ApplicationError):
                raise
            if isinstance(
                exc,
                db.RequirementSetPublicationCurrentnessConflict,
            ):
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": "working_tree_identity_changed",
                        "message": (
                            "需求结构已在生成确认清单的过程中发生变化；"
                            "请刷新结构与清单分类后重试。"
                        ),
                    },
                ) from exc
            if isinstance(exc, (RequirementTreeSnapshotError, ValueError)):
                raise ApplicationError(
                    status_code=409,
                    detail={
                        "code": "working_tree_snapshot_validation_failed",
                        "message": (
                            "DRAFT 未保留，因为版本化需求树无法通过来源校验。"
                        ),
                        "detail": str(exc),
                    },
                ) from exc
            raise ApplicationError(
                status_code=500,
                detail={
                    "code": "working_tree_draft_publication_failed",
                    "message": "DRAFT 生成失败，未保留半成品版本。",
                },
            ) from exc

        return _publication_response(
            project_id=project_id,
            document_id=document_id,
            requirement_set=requirement_set,
            publication_key=publication_key,
            working_tree_sha256=working_tree_sha256,
            snapshot=snapshot,
            # An expired publication is recovered in-place: the same set,
            # version and publication key are retained, so this receipt must
            # not claim that a new Requirement Set was allocated.
            requirement_set_created=requirement_set_created,
            publication_state="READY",
            requirement_tree_persisted=True,
            entry_classification_snapshot_id=(
                classification_snapshot.snapshot_id
            ),
            entry_classification_snapshot_sha256=(
                classification_snapshot.payload_sha256
            ),
        )


def _claim_equivalent_legacy_draft(
    *,
    project_id: str,
    document_id: str,
    publication_key: str,
    working_tree: RequirementTreeSnapshotV1,
) -> dict[str, Any]:
    """Reuse the latest exact legacy DRAFT instead of minting another version."""

    for candidate in db.list_requirement_sets(project_id, document_id):
        if str(candidate.get("publication_key") or "").strip():
            continue
        if str(candidate.get("status") or "").strip().upper() != "DRAFT":
            continue
        if (
            str(candidate.get("source_hash") or "")
            != working_tree.source_identity.source_sha256
        ):
            continue
        if not str(
            candidate.get("requirement_tree_object_key") or ""
        ).strip() or not str(
            candidate.get("requirement_tree_sha256") or ""
        ).strip():
            continue
        try:
            snapshot = load_requirement_set_tree_snapshot(candidate)
        except RequirementTreeSnapshotError:
            continue
        if (
            snapshot.state is not RequirementTreeState.READY
            or snapshot.requirement_evidence_bundle is None
            or _working_tree_publication_key(snapshot) != publication_key
        ):
            continue
        return db.claim_publication_key_for_existing_draft(
            project_id,
            document_id,
            str(candidate.get("id") or ""),
            publication_key=publication_key,
        )
    return {}


def _existing_publication_response(
    *,
    project_id: str,
    document_id: str,
    requirement_set: dict[str, Any],
    publication_key: str,
    working_tree_sha256: str,
    working_tree: RequirementTreeSnapshotV1,
    entry_classification_snapshot_id: str,
    entry_classification_snapshot_sha256: str,
) -> dict[str, Any]:
    """Reuse a completed publication or expose the same in-flight identity."""

    object_key = str(
        requirement_set.get("requirement_tree_object_key") or ""
    ).strip()
    tree_sha256 = str(
        requirement_set.get("requirement_tree_sha256") or ""
    ).strip()
    publication_state = str(
        requirement_set.get("publication_state") or ""
    ).strip().upper()
    if object_key and tree_sha256:
        try:
            snapshot = load_requirement_set_tree_snapshot(requirement_set)
        except RequirementTreeSnapshotError as exc:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "existing_working_tree_publication_invalid",
                    "message": (
                        "已存在的同内容 DRAFT 无法通过快照校验；"
                        "请先修复该版本，系统不会重复生成另一份。"
                    ),
                    "requirement_set_id": str(
                        requirement_set.get("id") or ""
                    ),
                },
            ) from exc
        if (
            snapshot.state is not RequirementTreeState.READY
            or snapshot.requirement_evidence_bundle is None
        ):
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "existing_working_tree_publication_incomplete",
                    "message": (
                        "已存在的同内容 DRAFT 尚未形成可验证证据快照；"
                        "系统不会重复生成另一份。"
                    ),
                    "requirement_set_id": str(
                        requirement_set.get("id") or ""
                    ),
                },
            )
        return _publication_response(
            project_id=project_id,
            document_id=document_id,
            requirement_set=requirement_set,
            publication_key=publication_key,
            working_tree_sha256=working_tree_sha256,
            snapshot=snapshot,
            requirement_set_created=False,
            publication_state="READY",
            requirement_tree_persisted=True,
            entry_classification_snapshot_id=(
                entry_classification_snapshot_id
            ),
            entry_classification_snapshot_sha256=(
                entry_classification_snapshot_sha256
            ),
        )

    # This state is observable only across multiple server workers: the
    # process-local lock makes same-process retries wait for the first writer.
    # Returning the already allocated identity prevents a second set, row
    # batch, or object from being created while the owner finishes.
    if publication_state == "PUBLISHING":
        return _publication_response(
            project_id=project_id,
            document_id=document_id,
            requirement_set=requirement_set,
            publication_key=publication_key,
            working_tree_sha256=working_tree_sha256,
            snapshot=working_tree,
            requirement_set_created=False,
            publication_state="PUBLISHING",
            requirement_tree_persisted=False,
            entry_classification_snapshot_id=(
                entry_classification_snapshot_id
            ),
            entry_classification_snapshot_sha256=(
                entry_classification_snapshot_sha256
            ),
        )
    raise ApplicationError(
        status_code=409,
        detail={
            "code": "existing_working_tree_publication_missing",
            "message": (
                "已存在的同内容 DRAFT 缺少快照；"
                "系统不会用重复版本掩盖该问题。"
            ),
            "requirement_set_id": str(requirement_set.get("id") or ""),
        },
    )


def _publication_response(
    *,
    project_id: str,
    document_id: str,
    requirement_set: dict[str, Any],
    publication_key: str,
    working_tree_sha256: str,
    snapshot: RequirementTreeSnapshotV1,
    requirement_set_created: bool,
    publication_state: str,
    requirement_tree_persisted: bool,
    entry_classification_snapshot_id: str,
    entry_classification_snapshot_sha256: str,
) -> dict[str, Any]:
    counts = _review_candidate_counts(snapshot)
    return {
        "schema_version": "working-requirement-tree-publish-draft-response/v1",
        "project_id": project_id,
        "document_id": document_id,
        "serving_impact": "DRAFT_ONLY",
        "requirement_set_created": requirement_set_created,
        "requirement_set_activated": False,
        "publication": {
            "requirement_set_id": str(requirement_set.get("id") or ""),
            "version": int(requirement_set.get("version") or 0),
            "status": (
                str(requirement_set.get("status") or "DRAFT").strip().upper()
                or "DRAFT"
            ),
            "source": "LOCAL_STRUCTURE_PROXY",
            "publication_key": publication_key,
            "publication_state": publication_state,
            "requirement_tree_persisted": requirement_tree_persisted,
            "working_tree_sha256": working_tree_sha256,
            "entry_classification_snapshot_id": (
                entry_classification_snapshot_id
            ),
            "entry_classification_snapshot_sha256": (
                entry_classification_snapshot_sha256
            ),
            "counts": counts,
            "count_semantics": "REVIEW_CANDIDATES_NOT_CONFIRMED_FINAL_REQUIREMENTS",
        },
        "requirement_tree": snapshot.model_dump(mode="json"),
    }


def _working_tree_publication_key(
    working_tree: RequirementTreeSnapshotV1,
    *,
    entry_classification_snapshot_id: str = "",
    entry_classification_snapshot_sha256: str = "",
) -> str:
    """Hash only stable source, effective Structure, projection, and evidence."""

    classification_snapshot_id = str(
        entry_classification_snapshot_id or ""
    ).strip()
    classification_snapshot_sha256 = str(
        entry_classification_snapshot_sha256 or ""
    ).strip()
    if bool(classification_snapshot_id) != bool(
        classification_snapshot_sha256
    ):
        raise ValueError(
            "entry classification snapshot ID and SHA-256 must be paired"
        )
    if classification_snapshot_id and (
        len(classification_snapshot_id) > 200
        or re.fullmatch(
            r"[0-9a-f]{64}",
            classification_snapshot_sha256,
        )
        is None
    ):
        raise ValueError(
            "entry classification snapshot identity is invalid"
        )

    payload = {
        "schema_version": "working-tree-publication-key/v1",
        "source_sha256": working_tree.source_identity.source_sha256,
        "structure_identity": working_tree.structure_identity.model_dump(
            mode="json"
        ),
        "root_node_ids": list(working_tree.root_node_ids),
        "nodes": [
            _publication_semantic_payload(
                node.model_dump(
                    mode="json",
                    exclude={
                        "requirement_entry_id",
                        "requirement_evidence_binding_id",
                        # These fields are derived presentation/edit bindings.
                        # They were added to the backwards-readable v1 snapshot
                        # model, so including their defaults would rewrite the
                        # canonical key of a frozen pre-extension set.
                        "editable_structure_node_ids",
                        "toc_entries",
                        "source_offset",
                    },
                )
            )
            for node in working_tree.nodes
        ],
        "table_record_projections": [
            projection.model_dump(mode="json", exclude={"decision"})
            for projection in working_tree.table_record_projections
        ],
        "table_evidence_bindings": [
            binding.model_dump(mode="json")
            for binding in working_tree.table_evidence_bindings
        ],
    }
    if classification_snapshot_id:
        payload["entry_classification_snapshot"] = {
            "id": classification_snapshot_id,
            "payload_sha256": classification_snapshot_sha256,
        }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_PUBLICATION_PRESENTATION_KEYS = frozenset(
    {
        "reading_event_id",
        "source_offset",
        "caption_source_order",
        "caption_source_offset",
    }
)


def _publication_semantic_payload(value: Any) -> Any:
    """Drop derived reading/edit extensions from the frozen v1 key shape."""

    if isinstance(value, dict):
        return {
            key: _publication_semantic_payload(item)
            for key, item in value.items()
            if key not in _PUBLICATION_PRESENTATION_KEYS
        }
    if isinstance(value, list):
        return [_publication_semantic_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_publication_semantic_payload(item) for item in value]
    return value


def _review_candidate_counts(
    snapshot: RequirementTreeSnapshotV1,
) -> dict[str, int]:
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
    return {
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
        "stored_review_entry_count": sum(
            bool(node.requirement_entry_id) for node in reviewable
        ),
    }


def _requirement_tree_preorder_ids(
    snapshot: RequirementTreeSnapshotV1,
) -> tuple[str, ...]:
    """Return the immutable reviewed tree order without re-sorting siblings."""

    node_by_id = {node.id: node for node in snapshot.nodes}
    ordered: list[str] = []
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        ordered.append(node_id)
        for child_id in node_by_id[node_id].child_ids:
            visit(child_id)

    for root_id in snapshot.root_node_ids:
        visit(root_id)
    return tuple(ordered)


def _persist_local_requirement_entries(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    preview_generation_id: str,
    working_tree: RequirementTreeSnapshotV1,
    requirement_nodes: tuple[RequirementTreeNodeV1, ...],
    publication_key: str,
    publication_owner_token: str,
) -> list[dict[str, Any]]:
    node_by_id = {node.id: node for node in working_tree.nodes}
    entries: list[dict[str, Any]] = []
    used_codes: set[str] = set()
    for index, node in enumerate(requirement_nodes, start=1):
        if publication_owner_token and (index == 1 or index % 25 == 0):
            heartbeat = db.heartbeat_requirement_set_publication(
                project_id,
                document_id,
                requirement_set_id,
                publication_owner_token=publication_owner_token,
            )
            if not heartbeat:
                raise db.RequirementSetMutationBlocked(
                    "Requirement Set publication lease was lost."
                )
        base_code = f"REQ{index:04d}"
        req_code = base_code
        suffix = 2
        while req_code in used_codes:
            req_code = f"{base_code}-{suffix}"
            suffix += 1
        used_codes.add(req_code)
        parent = node_by_id.get(node.parent_id or "")
        inserted = db.insert_requirement(
            project_id=project_id,
            sor_document_id=document_id,
            requirement_set_id=requirement_set_id,
            req_code=req_code,
            source_page=node.page_start,
            source_anchor=_source_anchor(node),
            requirement_text=node.summary,
            requirement_type=_requirement_type(node.classification),
            priority="MEDIUM",
            status="DRAFT",
            entry_kind=(
                "REQUIREMENT"
                if node.projection_role == "REQUIREMENT"
                else "REFERENCE"
            ),
            text_relation="distinct",
            is_deduped=False,
            parent_section_code="",
            parent_section_title=parent.label if parent is not None else "",
            parent_section_label=parent.label if parent is not None else "",
            heading_text="",
            raw_text=node.summary,
            display_text=node.summary,
            merged_from_req_codes="[]",
            merged_from_entry_ids="[]",
            dedupe_version="requirement-tree-local-v1",
            table_refs=_json_payload(node.table_refs),
            content_blocks=_json_payload(node.content_blocks),
            has_table_asset=bool(node.table_refs),
            table_render_mode="structured" if node.table_refs else "",
            preview_generation_id=preview_generation_id,
            requirement_id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    (
                        f"reqpilotx:{requirement_set_id}:"
                        f"{publication_key}:{node.id}"
                    ),
                )
            ),
            publication_owner_token=publication_owner_token,
        )
        requirement_entry_id = str(inserted.get("id") or "")
        if not requirement_entry_id:
            raise RuntimeError("A local requirement row could not be persisted.")
        entries.append(
            {
                "requirement_tree_node_id": _stable_leaf_id(node.id),
                "requirement_entry_id": requirement_entry_id,
                "workflow_node_id": node.workflow_node_id,
                "owner_structure_node_id": node.structure_node_id,
                "source_order": node.source_order,
                "title": node.label,
                "source_text": node.summary,
                "page_start": node.page_start,
                "page_end": node.page_end,
                "source_refs": list(node.source_refs),
                "status": node.status,
                "classification": node.classification,
                "projection_role": node.projection_role,
                "table_record_id": node.table_record_id,
                "table_evidence_binding_id": (
                    node.table_evidence_binding_id
                ),
                "deterministic_rule_evidence": list(
                    node.deterministic_rule_evidence
                ),
                "include_in_view": node.include_in_view,
                "content_blocks": list(node.content_blocks),
                "table_refs": list(node.table_refs),
                "figure_refs": list(node.figure_refs),
            }
        )
    return entries


def _assert_same_tree_projection(
    working: RequirementTreeSnapshotV1,
    published: RequirementTreeSnapshotV1,
) -> None:
    if (
        working.structure_identity != published.structure_identity
        or working.root_node_ids != published.root_node_ids
        or working.table_record_projections
        != published.table_record_projections
        or working.table_evidence_bindings
        != published.table_evidence_bindings
        or tuple(node.id for node in working.nodes)
        != tuple(node.id for node in published.nodes)
    ):
        raise RequirementTreeSnapshotError(
            "published DRAFT does not preserve the working tree projection"
        )
    working_by_id = {node.id: node for node in working.nodes}
    if published.requirement_evidence_bundle is None:
        raise RequirementTreeSnapshotError(
            "published DRAFT has no immutable requirement evidence bundle"
        )
    for node in published.nodes:
        source = working_by_id[node.id]
        if (
            node.parent_id != source.parent_id
            or node.child_ids != source.child_ids
            or node.structure_node_id != source.structure_node_id
            or node.source_order != source.source_order
            or node.label != source.label
            or node.summary != source.summary
            or not _same_canonical_json_value(
                node.source_refs,
                source.source_refs,
            )
            or node.projection_role != source.projection_role
            or node.classification != source.classification
            or node.semantic_state != source.semantic_state
            or not _same_canonical_json_value(
                node.deterministic_rule_evidence,
                source.deterministic_rule_evidence,
            )
            or node.table_record_id != source.table_record_id
            or node.table_evidence_binding_id
            != source.table_evidence_binding_id
            or node.bilingual_context != source.bilingual_context
        ):
            raise RequirementTreeSnapshotError(
                "published DRAFT changed a working tree node"
            )
        if (
            node.projection_role
            in {
                "STRUCTURE_PROXY",
                "DIRECT_CONTENT",
                "WHOLE_TABLE_REVIEW",
                "TABLE_RECORD",
                "REQUIREMENT",
            }
            and (
                not node.requirement_entry_id
                or not node.requirement_evidence_binding_id
            )
        ):
            raise RequirementTreeSnapshotError(
                "published review item has no database or evidence binding"
            )


def _same_canonical_json_value(left: Any, right: Any) -> bool:
    """Compare JSON facts without treating tuple/list decoding as a change."""

    return json.dumps(
        left,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) == json.dumps(
        right,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_leaf_id(node_id: str) -> str:
    prefix = "requirement:"
    return node_id[len(prefix) :] if node_id.startswith(prefix) else node_id


def _source_anchor(node: RequirementTreeNodeV1) -> str:
    for source_ref in node.source_refs:
        source_id = str(
            source_ref.get("source_id")
            or source_ref.get("semantic_unit_id")
            or ""
        ).strip()
        if source_id:
            return source_id
    return f"page:{node.page_start}" if node.page_start else "Unknown"


def _requirement_type(classification: str) -> str:
    token = str(classification or "").upper()
    for candidate in ("SAFETY", "PERFORMANCE", "INTERFACE"):
        if candidate in token:
            return candidate
    return "FUNCTIONAL"


def _json_payload(value: Any) -> str:
    return json.dumps(
        list(value or ()),
        ensure_ascii=False,
        separators=(",", ":"),
    )


__all__ = ["publish_working_requirement_tree_draft"]
