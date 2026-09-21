from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.extraction import structure_workspace
from reqpilotx_server.application.extraction.entry_classification_repository import (
    EntryClassificationCommandConflict,
    EntryClassificationSnapshotCorrupted,
    EntryClassificationStaleWrite,
    append_entry_classification_snapshot,
    get_entry_classification_command_result,
    get_entry_classification_snapshot_or_initial,
    is_entry_classification_command_receipt,
    record_entry_classification_command_receipt,
)
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.domain.entry_classification import (
    EntryClassificationSnapshotV1,
    EntryClassificationStatus,
    EntryClassificationType,
    UnclassifiedEntriesError,
    accept_entry_classification_snapshot,
    build_initial_entry_classification_snapshot,
    revise_entry_classification_snapshot,
)
from reqpilotx_server.domain.extraction.requirement_tree import (
    RequirementTreeMode,
    RequirementTreeSnapshotV1,
    RequirementTreeState,
)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_COMMAND_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
_LOCAL_ACCEPTANCE_ACTOR = "LOCAL_USER"


class _EntryClassificationCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(pattern=_COMMAND_ID_PATTERN)
    expected_revision: int = Field(ge=0)
    expected_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_working_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_effective_structure_id: str = Field(min_length=1, max_length=200)
    expected_policy_version: str = Field(min_length=1, max_length=200)


class EntryClassificationPatchRequest(_EntryClassificationCommandRequest):
    overrides: dict[str, EntryClassificationType | None] = Field(
        default_factory=dict,
        max_length=50_000,
    )

    @field_validator("overrides")
    @classmethod
    def validate_workflow_ids(
        cls,
        value: dict[str, EntryClassificationType | None],
    ) -> dict[str, EntryClassificationType | None]:
        if any(
            not workflow_id
            or len(workflow_id) > 260
            or any(ord(character) < 32 for character in workflow_id)
            for workflow_id in value
        ):
            raise ValueError("override keys must be bounded workflow node IDs")
        return value


class EntryClassificationAcceptanceRequest(_EntryClassificationCommandRequest):
    pass


def get_document_entry_classifications(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    tree = _read_current_requirement_tree(project_id, document_id)
    initial = build_initial_entry_classification_snapshot(tree)
    try:
        current = get_entry_classification_snapshot_or_initial(initial)
    except EntryClassificationSnapshotCorrupted as exc:
        raise _classification_conflict(
            "entry_classification_snapshot_corrupted",
            "当前清单分类记录无法验证，请刷新或重新生成清单。",
        ) from exc
    _set_response_headers(response)
    return _classification_response(current, changed=False)


def patch_document_entry_classifications(
    project_id: str,
    document_id: str,
    payload: EntryClassificationPatchRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    tree = _read_current_requirement_tree(project_id, document_id)
    _require_entry_classification_mutation_allowed(
        project_id=project_id,
        document_id=document_id,
        source_sha256=tree.source_identity.source_sha256,
    )
    initial = build_initial_entry_classification_snapshot(tree)
    command_sha256 = _canonical_command_sha256(
        action="PATCH",
        project_id=project_id,
        document_id=document_id,
        payload=payload,
    )
    replay = _command_replay_or_none(
        initial,
        command_id=payload.command_id,
        command_sha256=command_sha256,
    )
    if replay is not None:
        replay_snapshot, replay_changed = replay
        _set_response_headers(response)
        return _classification_response(
            replay_snapshot,
            changed=replay_changed,
        )
    current = _current_snapshot(initial)
    _validate_expected_identity(payload, initial=initial, current=current)
    try:
        revised = revise_entry_classification_snapshot(
            current,
            overrides=payload.overrides,
            snapshot_id=str(uuid.uuid4()),
            created_at=_now_iso(),
        )
    except KeyError as exc:
        raise ApplicationError(
            status_code=422,
            code="entry_classification_override_unknown",
            detail={
                "code": "entry_classification_override_unknown",
                "message": "分类修改包含当前清单中不存在的条目。",
            },
        ) from exc
    if revised is current:
        _record_noop_command_receipt(
            current=current,
            command_id=payload.command_id,
            command_sha256=command_sha256,
            expected_revision=payload.expected_revision,
            expected_snapshot_sha256=payload.expected_snapshot_sha256,
        )
        _assert_tree_unchanged_after_write(
            project_id=project_id,
            document_id=document_id,
            previous_initial=initial,
        )
        _set_response_headers(response)
        return _classification_response(current, changed=False)
    stored = _append_snapshot(
        base=current,
        next_snapshot=revised,
        command_id=payload.command_id,
        command_sha256=command_sha256,
        expected_revision=payload.expected_revision,
        expected_snapshot_sha256=payload.expected_snapshot_sha256,
    )
    _assert_tree_unchanged_after_write(
        project_id=project_id,
        document_id=document_id,
        previous_initial=initial,
    )
    _set_response_headers(response)
    return _classification_response(stored, changed=True)


def accept_document_entry_classifications(
    project_id: str,
    document_id: str,
    payload: EntryClassificationAcceptanceRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    tree = _read_current_requirement_tree(project_id, document_id)
    _require_entry_classification_mutation_allowed(
        project_id=project_id,
        document_id=document_id,
        source_sha256=tree.source_identity.source_sha256,
    )
    initial = build_initial_entry_classification_snapshot(tree)
    command_sha256 = _canonical_command_sha256(
        action="ACCEPT",
        project_id=project_id,
        document_id=document_id,
        payload=payload,
    )
    replay = _command_replay_or_none(
        initial,
        command_id=payload.command_id,
        command_sha256=command_sha256,
    )
    if replay is not None:
        replay_snapshot, replay_changed = replay
        _set_response_headers(response)
        return _classification_response(
            replay_snapshot,
            changed=replay_changed,
        )
    current = _current_snapshot(initial)
    _validate_expected_identity(payload, initial=initial, current=current)
    try:
        accepted = accept_entry_classification_snapshot(
            current,
            snapshot_id=str(uuid.uuid4()),
            accepted_at=_now_iso(),
            accepted_by=_LOCAL_ACCEPTANCE_ACTOR,
        )
    except UnclassifiedEntriesError as exc:
        raise ApplicationError(
            status_code=422,
            code="entry_classification_incomplete",
            detail={
                "code": "entry_classification_incomplete",
                "message": "仍有未分类条目，完成分类后才能生成确认清单。",
                "unclassified_count": len(exc.workflow_node_ids),
                "workflow_node_ids": list(exc.workflow_node_ids),
            },
        ) from exc
    except ValueError as exc:
        raise _classification_conflict(
            "entry_classification_acceptance_conflict",
            "当前分类状态不能再次接受，请刷新清单。",
        ) from exc
    stored = _append_snapshot(
        base=current,
        next_snapshot=accepted,
        command_id=payload.command_id,
        command_sha256=command_sha256,
        expected_revision=payload.expected_revision,
        expected_snapshot_sha256=payload.expected_snapshot_sha256,
    )
    _assert_tree_unchanged_after_write(
        project_id=project_id,
        document_id=document_id,
        previous_initial=initial,
    )
    _set_response_headers(response)
    return _classification_response(stored, changed=True)


def _read_current_requirement_tree(
    project_id: str,
    document_id: str,
) -> RequirementTreeSnapshotV1:
    read_response = SimpleNamespace(headers={}, status_code=200)
    result = structure_workspace.get_document_structure_agent(
        project_id,
        document_id,
        read_response,
        request=None,
    )
    workbench = (
        result.get("structure_workbench")
        if isinstance(result, dict)
        else None
    )
    raw_tree = (
        workbench.get("requirement_tree")
        if isinstance(workbench, dict)
        else None
    )
    try:
        tree = RequirementTreeSnapshotV1.model_validate(raw_tree)
    except (TypeError, ValueError, ValidationError) as exc:
        raise _classification_conflict(
            "entry_classification_tree_unavailable",
            "当前需求结构树尚未就绪，不能整理清单分类。",
        ) from exc
    if (
        tree.mode is not RequirementTreeMode.WORKING
        or tree.state is not RequirementTreeState.READY
        or tree.source_identity.project_id != project_id
        or tree.source_identity.document_id != document_id
        or not tree.source_identity.source_sha256
        or not tree.structure_identity.effective_snapshot_id
    ):
        raise _classification_conflict(
            "entry_classification_tree_not_ready",
            "当前需求结构树尚未完整就绪，不能整理清单分类。",
        )
    return tree


def _current_snapshot(
    initial: EntryClassificationSnapshotV1,
) -> EntryClassificationSnapshotV1:
    try:
        return get_entry_classification_snapshot_or_initial(initial)
    except EntryClassificationSnapshotCorrupted as exc:
        raise _classification_conflict(
            "entry_classification_snapshot_corrupted",
            "当前清单分类记录无法验证，请刷新或重新生成清单。",
        ) from exc


def _require_entry_classification_mutation_allowed(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
) -> None:
    from reqpilotx_server.application.extraction.document_jobs import (
        _reject_review_mutation_while_extraction_runs,
    )

    _reject_review_mutation_while_extraction_runs(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
    )


def _command_replay_or_none(
    initial: EntryClassificationSnapshotV1,
    *,
    command_id: str,
    command_sha256: str,
) -> tuple[EntryClassificationSnapshotV1, bool] | None:
    try:
        replay = get_entry_classification_command_result(
            initial,
            command_id=command_id,
            command_sha256=command_sha256,
        )
        replay_is_receipt = (
            replay is not None
            and is_entry_classification_command_receipt(
                initial,
                command_id=command_id,
                command_sha256=command_sha256,
            )
        )
    except EntryClassificationCommandConflict as exc:
        raise _classification_conflict(
            "entry_classification_command_conflict",
            "该操作标识已用于不同的分类修改，请刷新后重试。",
        ) from exc
    except EntryClassificationSnapshotCorrupted as exc:
        raise _classification_conflict(
            "entry_classification_snapshot_corrupted",
            "当前清单分类记录无法验证，请刷新或重新生成清单。",
        ) from exc
    if replay is None:
        return None
    current = _current_snapshot(initial)
    if (
        current.snapshot_id != replay.snapshot_id
        or current.payload_sha256 != replay.payload_sha256
    ):
        raise _classification_conflict(
            "entry_classification_command_result_superseded",
            "该操作已成功，但清单随后又有更新；请刷新当前结果。",
        )
    return replay, not replay_is_receipt


def _append_snapshot(
    *,
    base: EntryClassificationSnapshotV1,
    next_snapshot: EntryClassificationSnapshotV1,
    command_id: str,
    command_sha256: str,
    expected_revision: int,
    expected_snapshot_sha256: str,
) -> EntryClassificationSnapshotV1:
    try:
        stored, _created = append_entry_classification_snapshot(
            base_snapshot=base,
            next_snapshot=next_snapshot,
            command_id=command_id,
            command_sha256=command_sha256,
            expected_revision=expected_revision,
            expected_snapshot_sha256=expected_snapshot_sha256,
        )
        return stored
    except (
        EntryClassificationCommandConflict,
        EntryClassificationStaleWrite,
    ) as exc:
        raise _classification_conflict(
            "entry_classification_write_conflict",
            "清单分类已更新，请刷新后重试。",
        ) from exc
    except EntryClassificationSnapshotCorrupted as exc:
        raise _classification_conflict(
            "entry_classification_snapshot_corrupted",
            "当前清单分类记录无法验证，请刷新或重新生成清单。",
        ) from exc


def _record_noop_command_receipt(
    *,
    current: EntryClassificationSnapshotV1,
    command_id: str,
    command_sha256: str,
    expected_revision: int,
    expected_snapshot_sha256: str,
) -> None:
    try:
        record_entry_classification_command_receipt(
            current_snapshot=current,
            command_id=command_id,
            command_sha256=command_sha256,
            expected_revision=expected_revision,
            expected_snapshot_sha256=expected_snapshot_sha256,
        )
    except EntryClassificationCommandConflict as exc:
        raise _classification_conflict(
            "entry_classification_command_conflict",
            "该操作标识已用于不同的分类修改，请刷新后重试。",
        ) from exc
    except EntryClassificationStaleWrite as exc:
        raise _classification_conflict(
            "entry_classification_write_conflict",
            "清单分类已更新，请刷新后重试。",
        ) from exc
    except EntryClassificationSnapshotCorrupted as exc:
        raise _classification_conflict(
            "entry_classification_snapshot_corrupted",
            "当前清单分类记录无法验证，请刷新或重新生成清单。",
        ) from exc


def _validate_expected_identity(
    payload: _EntryClassificationCommandRequest,
    *,
    initial: EntryClassificationSnapshotV1,
    current: EntryClassificationSnapshotV1,
) -> None:
    if (
        payload.expected_source_sha256 != initial.source_sha256
        or payload.expected_working_tree_sha256 != initial.working_tree_sha256
        or payload.expected_effective_structure_id
        != initial.effective_structure_id
        or payload.expected_policy_version != initial.policy_version
        or payload.expected_revision != current.revision
        or payload.expected_snapshot_sha256 != current.payload_sha256
    ):
        raise _classification_conflict(
            "entry_classification_identity_changed",
            "清单或结构已更新，请刷新后重试。",
        )


def _assert_tree_unchanged_after_write(
    *,
    project_id: str,
    document_id: str,
    previous_initial: EntryClassificationSnapshotV1,
) -> None:
    try:
        latest_tree = _read_current_requirement_tree(project_id, document_id)
    except ApplicationError as exc:
        raise _classification_conflict(
            "entry_classification_tree_changed_after_write",
            "结构在保存分类时发生变化；本次记录仅保留审计，请刷新当前清单。",
        ) from exc
    latest_initial = build_initial_entry_classification_snapshot(latest_tree)
    if _lineage_identity(latest_initial) != _lineage_identity(previous_initial):
        raise _classification_conflict(
            "entry_classification_tree_changed_after_write",
            "结构在保存分类时发生变化；本次记录仅保留审计，请刷新当前清单。",
        )


def _lineage_identity(snapshot: EntryClassificationSnapshotV1) -> tuple[str, ...]:
    return (
        snapshot.project_id,
        snapshot.sor_document_id,
        snapshot.source_sha256,
        snapshot.working_tree_id,
        snapshot.working_tree_sha256,
        snapshot.effective_structure_id,
        snapshot.semantic_snapshot_id,
        snapshot.policy_version,
    )


def _canonical_command_sha256(
    *,
    action: str,
    project_id: str,
    document_id: str,
    payload: _EntryClassificationCommandRequest,
) -> str:
    command: dict[str, Any] = {
        "schema_version": "entry-classification-command/v1",
        "action": action,
        "project_id": project_id,
        "sor_document_id": document_id,
        "command_id": payload.command_id,
        "expected_revision": payload.expected_revision,
        "expected_snapshot_sha256": payload.expected_snapshot_sha256,
        "expected_source_sha256": payload.expected_source_sha256,
        "expected_working_tree_sha256": (
            payload.expected_working_tree_sha256
        ),
        "expected_effective_structure_id": (
            payload.expected_effective_structure_id
        ),
        "expected_policy_version": payload.expected_policy_version,
    }
    if isinstance(payload, EntryClassificationPatchRequest):
        command["overrides"] = [
            {
                "workflow_node_id": workflow_node_id,
                "entry_type": entry_type.value if entry_type is not None else None,
            }
            for workflow_node_id, entry_type in sorted(payload.overrides.items())
        ]
    else:
        command["accepted_by"] = _LOCAL_ACCEPTANCE_ACTOR
    return hashlib.sha256(
        json.dumps(
            command,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _classification_response(
    snapshot: EntryClassificationSnapshotV1,
    *,
    changed: bool,
) -> dict[str, Any]:
    counts = {
        "total": len(snapshot.entries),
        "technical": 0,
        "non_technical": 0,
        "information": 0,
        "unclassified": 0,
        "human_override": 0,
    }
    for entry in snapshot.entries:
        effective = (
            entry.effective_type
            or entry.human_override
            or entry.suggested_type
        )
        if effective is EntryClassificationType.TECHNICAL:
            counts["technical"] += 1
        elif effective is EntryClassificationType.NON_TECHNICAL:
            counts["non_technical"] += 1
        elif effective is EntryClassificationType.INFORMATION:
            counts["information"] += 1
        else:
            counts["unclassified"] += 1
        counts["human_override"] += int(entry.human_override is not None)
    complete = counts["unclassified"] == 0
    can_accept = (
        complete and snapshot.status is EntryClassificationStatus.WORKING
    )
    can_publish = (
        complete and snapshot.status is EntryClassificationStatus.ACCEPTED
    )
    return {
        "schema_version": "entry-classification-response/v1",
        "snapshot": snapshot.model_dump(mode="json"),
        "counts": counts,
        "can_accept": can_accept,
        "can_publish": can_publish,
        "actions": {
            "can_override": True,
            "can_accept": can_accept,
            "can_publish": can_publish,
        },
        "changed": changed,
        "serving_impact": "NONE",
    }


def _classification_conflict(code: str, message: str) -> ApplicationError:
    return ApplicationError(
        status_code=409,
        code=code,
        detail={"code": code, "message": message},
    )


def _set_response_headers(response: ResponseSink) -> None:
    response.headers.update(
        {
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "EntryClassificationAcceptanceRequest",
    "EntryClassificationPatchRequest",
    "accept_document_entry_classifications",
    "get_document_entry_classifications",
    "patch_document_entry_classifications",
]
