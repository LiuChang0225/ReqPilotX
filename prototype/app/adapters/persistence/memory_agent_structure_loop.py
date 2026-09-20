from __future__ import annotations

from threading import RLock

from app.application.agent_workflow.activity import AgentActivityEventV2
from app.application.agent_workflow.structure_loop_persistence import (
    AgentStructureLoopActivityDraftV1,
    materialize_structure_loop_activity,
    stable_structure_loop_run_id,
)
from app.application.agent_workflow.structure_tool_loop import (
    NativeStructureHandoffAttemptReceiptV1,
    NativeStructureHandoffAttemptRequestV1,
    active_structure_projection_attempt,
    canonical_structure_projection_attempts_sha256,
)
from app.application.agent_workflow.native_structure_working_revision import (
    NativeStructureWorkingRevisionHeadV1,
    NativeStructureWorkingRevisionObjectV1,
    NativeStructureWorkingRevisionV1,
    build_native_structure_working_revision_object,
)
from app.ports.agent_structure_loop_repository import AgentStructureLoopConflict


class InMemoryAgentStructureLoopRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self._attempts: dict[str, NativeStructureHandoffAttemptReceiptV1] = {}
        self._attempt_scopes: dict[str, tuple[str, str, str, str]] = {}
        self._attempt_requests: dict[str, NativeStructureHandoffAttemptRequestV1] = {}
        self._attempt_commands: dict[tuple[str, str], tuple[str, str]] = {}
        self._activity: dict[str, AgentActivityEventV2] = {}
        self._activity_scopes: dict[str, tuple[str, str, str, str, str]] = {}
        self._activity_commands: dict[tuple[str, str], tuple[str, str]] = {}
        self._working_revision_objects: dict[
            tuple[str, str, str, int], NativeStructureWorkingRevisionObjectV1
        ] = {}
        self._working_revision_heads: dict[
            tuple[str, str, str, str, str], NativeStructureWorkingRevisionHeadV1
        ] = {}
        self._working_revision_head_publication_order: dict[
            tuple[str, str, str, str, str], int
        ] = {}
        self._working_revision_publication_sequence = 0

    def append_working_revision(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        base_artifact_id: str,
        base_projection_sha256: str,
        revision: NativeStructureWorkingRevisionV1,
        object_key: str,
        object_sha256: str,
        object_size: int,
        media_type: str,
        expected_head_revision_sha256: str | None,
    ) -> tuple[NativeStructureWorkingRevisionHeadV1, bool]:
        verified = NativeStructureWorkingRevisionV1.model_validate(
            revision.model_dump(mode="python")
        )
        if (
            verified.source_sha256 != source_sha256
            or verified.base_artifact_id != base_artifact_id
            or verified.base_projection_sha256 != base_projection_sha256
        ):
            raise AgentStructureLoopConflict(
                "working revision persistence identity drifted"
            )
        record = build_native_structure_working_revision_object(
            project_id=project_id,
            document_id=document_id,
            revision=verified,
            object_key=object_key,
            object_sha256=object_sha256,
            object_size=object_size,
            media_type=media_type,
        )
        record_key = (project_id, document_id, verified.run_id, verified.revision)
        head_key = (
            project_id,
            document_id,
            source_sha256,
            base_artifact_id,
            base_projection_sha256,
        )
        with self._lock:
            replay = self._working_revision_objects.get(record_key)
            if replay is not None:
                if replay != record:
                    raise AgentStructureLoopConflict(
                        "working revision replay changed payload"
                    )
                return replay.head(), False
            head = self._working_revision_heads.get(head_key)
            if verified.revision == 1:
                if head is not None or expected_head_revision_sha256 is not None:
                    raise AgentStructureLoopConflict(
                        "working revision head changed"
                    )
            elif (
                head is None
                or head.run_id != verified.run_id
                or head.revision != verified.revision - 1
                or head.revision_sha256 != verified.previous_revision_sha256
                or head.revision_sha256 != expected_head_revision_sha256
                or head.state == "ACCEPTED"
            ):
                raise AgentStructureLoopConflict("working revision head changed")
            if any(
                item.revision_id == record.revision_id and item != record
                for item in self._working_revision_objects.values()
            ):
                raise AgentStructureLoopConflict(
                    "working revision identity owns another payload"
                )
            self._working_revision_objects[record_key] = record
            self._working_revision_heads[head_key] = record.head()
            self._working_revision_publication_sequence += 1
            self._working_revision_head_publication_order[head_key] = (
                self._working_revision_publication_sequence
            )
            return record.head(), True

    def get_working_revision_head(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        base_artifact_id: str | None = None,
        base_projection_sha256: str | None = None,
    ) -> NativeStructureWorkingRevisionHeadV1 | None:
        with self._lock:
            values = tuple(
                item
                for key, item in self._working_revision_heads.items()
                if key[:3] == (project_id, document_id, source_sha256)
                and (base_artifact_id is None or key[3] == base_artifact_id)
                and (
                    base_projection_sha256 is None
                    or key[4] == base_projection_sha256
                )
            )
        if not values:
            return None
        order_by_revision = {
            item.revision_id: self._working_revision_head_publication_order[key]
            for key, item in self._working_revision_heads.items()
        }
        return max(values, key=lambda item: order_by_revision[item.revision_id])

    def get_working_revision_object(
        self,
        *,
        project_id: str,
        document_id: str,
        run_id: str,
        revision: int,
    ) -> NativeStructureWorkingRevisionObjectV1 | None:
        with self._lock:
            return self._working_revision_objects.get(
                (project_id, document_id, run_id, revision)
            )

    def append_attempt(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        artifact_id: str,
        request: NativeStructureHandoffAttemptRequestV1,
        receipt: NativeStructureHandoffAttemptReceiptV1,
        expected_global_head_attempt_id: str | None = None,
        expected_global_projection_attempts_sha256: str | None = None,
    ) -> tuple[NativeStructureHandoffAttemptReceiptV1, bool]:
        _validate_attempt_binding(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
            request=request,
            receipt=receipt,
        )
        stream = _stream_key(
            project_id,
            document_id,
            source_sha256,
            artifact_id,
        )
        command_key = ("\x1f".join(stream), request.command_id)
        identity = (request.model_dump_json(), receipt.model_dump_json())
        with self._lock:
            replay = self._attempt_commands.get(command_key)
            if replay is not None:
                if replay != identity:
                    raise AgentStructureLoopConflict(
                        "attempt command replay changed payload"
                    )
                return self._attempts[receipt.attempt_id], False
            document_attempts = tuple(
                item
                for attempt_id, item in self._attempts.items()
                if self._attempt_scopes.get(attempt_id) == stream
            )
            if expected_global_head_attempt_id is not None:
                global_head = active_structure_projection_attempt(document_attempts)
                if (
                    global_head is None
                    or global_head.attempt_id != expected_global_head_attempt_id
                ):
                    raise AgentStructureLoopConflict(
                        "global projection head changed"
                    )
            if expected_global_projection_attempts_sha256 is not None:
                current_attempts_sha256 = (
                    canonical_structure_projection_attempts_sha256(
                        document_attempts
                    )
                )
                if (
                    current_attempts_sha256
                    != expected_global_projection_attempts_sha256
                ):
                    raise AgentStructureLoopConflict(
                        "global projection attempt set changed"
                    )
            lineage_attempts = tuple(
                sorted(
                    (
                        item
                        for item in document_attempts
                        if item.handoff_id == request.handoff_id
                    ),
                    key=lambda item: item.attempt_ordinal,
                )
            )
            lineage_head = lineage_attempts[-1] if lineage_attempts else None
            expected_ordinal = (
                1 if lineage_head is None else lineage_head.attempt_ordinal + 1
            )
            expected_previous = (
                None if lineage_head is None else lineage_head.receipt_sha256
            )
            if (
                request.attempt_ordinal != expected_ordinal
                or request.previous_attempt_receipt_sha256 != expected_previous
            ):
                raise AgentStructureLoopConflict(
                    "attempt does not continue the durable stream head"
                )
            existing = self._attempts.get(receipt.attempt_id)
            if existing is not None and existing != receipt:
                raise AgentStructureLoopConflict(
                    "attempt identity owns another immutable payload"
                )
            matching = [
                item
                for item in self._attempts.values()
                if item.source_sha256 == source_sha256
                and item.artifact_id == artifact_id
                and item.handoff_id == receipt.handoff_id
                and item.attempt_ordinal == receipt.attempt_ordinal
            ]
            if matching and matching[-1] != receipt:
                raise AgentStructureLoopConflict("attempt ordinal already exists")
            self._attempts[receipt.attempt_id] = receipt
            self._attempt_requests[receipt.attempt_id] = request
            self._attempt_scopes[receipt.attempt_id] = stream
            self._attempt_commands[command_key] = identity
            return receipt, existing is None

    def list_attempts(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        artifact_id: str,
        handoff_id: str | None = None,
    ) -> tuple[NativeStructureHandoffAttemptReceiptV1, ...]:
        stream = _stream_key(project_id, document_id, source_sha256, artifact_id)
        with self._lock:
            values = tuple(
                item
                for attempt_id, item in self._attempts.items()
                if self._attempt_scopes.get(attempt_id) == stream
            )
        return tuple(
            sorted(
                (
                    item
                    for item in values
                    if item.source_sha256 == source_sha256
                    and item.artifact_id == artifact_id
                    and (handoff_id is None or item.handoff_id == handoff_id)
                ),
                key=lambda item: (
                    item.handoff_id,
                    item.attempt_ordinal,
                    item.created_at_utc,
                ),
            )
        )

    def get_attempt_request(
        self,
        *,
        project_id: str,
        document_id: str,
        attempt_id: str,
    ) -> NativeStructureHandoffAttemptRequestV1 | None:
        with self._lock:
            scope = self._attempt_scopes.get(attempt_id)
            request = self._attempt_requests.get(attempt_id)
        if scope is None or scope[:2] != (project_id, document_id):
            return None
        return request

    def append_activity(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        artifact_id: str,
        handoff_id: str,
        command_id: str,
        draft: AgentStructureLoopActivityDraftV1,
    ) -> tuple[AgentActivityEventV2, bool]:
        stream = _stream_key(
            project_id,
            document_id,
            source_sha256,
            artifact_id,
        )
        command_key = ("\x1f".join((*stream, handoff_id)), command_id)
        draft_json = draft.model_dump_json()
        with self._lock:
            replay = self._activity_commands.get(command_key)
            if replay is not None:
                if replay[0] != draft_json:
                    raise AgentStructureLoopConflict(
                        "activity command replay changed payload"
                    )
                return self._activity[replay[1]], False
            current = [
                item
                for item in self._activity.values()
                if item.run_id
                == stable_structure_loop_run_id(
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=source_sha256,
                    artifact_id=artifact_id,
                    handoff_id=handoff_id,
                )
            ]
            event = materialize_structure_loop_activity(
                draft,
                run_id=stable_structure_loop_run_id(
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=source_sha256,
                    artifact_id=artifact_id,
                    handoff_id=handoff_id,
                ),
                source_sha256=source_sha256,
                sequence=len(current) + 1,
            )
            self._activity[event.event_id] = event
            self._activity_scopes[event.event_id] = (*stream, handoff_id)
            self._activity_commands[command_key] = (draft_json, event.event_id)
            return event, True

    def list_activity(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        artifact_id: str,
        handoff_id: str | None = None,
        after_sequence: int = 0,
    ) -> tuple[AgentActivityEventV2, ...]:
        stream = _stream_key(project_id, document_id, source_sha256, artifact_id)
        with self._lock:
            values = tuple(
                item
                for event_id, item in self._activity.items()
                if self._activity_scopes.get(event_id, ())[:4] == stream
                and (
                    handoff_id is None
                    or self._activity_scopes.get(event_id, ("", "", "", "", ""))[4]
                    == handoff_id
                )
            )
        return tuple(
            sorted(
                (
                    item
                    for item in values
                    if item.source_sha256 == source_sha256
                    and item.sequence > after_sequence
                ),
                key=lambda item: (item.occurred_at_utc, item.run_id, item.sequence),
            )
        )


def _stream_key(
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
) -> tuple[str, str, str, str]:
    values = (project_id, document_id, source_sha256, artifact_id)
    if any(not value for value in values):
        raise ValueError("structure loop persistence identity cannot be empty")
    return values


def _validate_attempt_binding(
    *,
    source_sha256: str,
    artifact_id: str,
    request: NativeStructureHandoffAttemptRequestV1,
    receipt: NativeStructureHandoffAttemptReceiptV1,
) -> None:
    if (
        request.expected_source_sha256 != source_sha256
        or receipt.source_sha256 != source_sha256
        or request.expected_artifact_id != artifact_id
        or receipt.artifact_id != artifact_id
        or request.handoff_id != receipt.handoff_id
        or request.command_id != receipt.command_id
        or request.attempt_ordinal != receipt.attempt_ordinal
        or request.previous_attempt_receipt_sha256
        != receipt.previous_attempt_receipt_sha256
    ):
        raise AgentStructureLoopConflict("attempt persistence identity drifted")


__all__ = ["InMemoryAgentStructureLoopRepository"]
