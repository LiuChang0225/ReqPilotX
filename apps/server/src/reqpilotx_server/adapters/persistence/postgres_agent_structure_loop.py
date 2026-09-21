from __future__ import annotations

import hashlib
import json
from datetime import UTC
from typing import Any, Mapping

from reqpilotx_server.application.agent_workflow.activity import AgentActivityEventV2
from reqpilotx_server.application.agent_workflow.structure_loop_persistence import (
    AgentStructureLoopActivityDraftV1,
    materialize_structure_loop_activity,
    stable_structure_loop_run_id,
)
from reqpilotx_server.application.agent_workflow.structure_tool_loop import (
    NativeStructureHandoffAttemptReceiptV1,
    NativeStructureHandoffAttemptRequestV1,
    active_structure_projection_attempt,
    canonical_structure_projection_attempts_sha256,
)
from reqpilotx_server.application.agent_workflow.native_structure_working_revision import (
    NativeStructureWorkingRevisionHeadV1,
    NativeStructureWorkingRevisionObjectV1,
    NativeStructureWorkingRevisionV1,
    build_native_structure_working_revision_object,
)
from reqpilotx_server.infrastructure.postgres import PostgresDatabase
from reqpilotx_server.ports.agent_structure_loop_repository import AgentStructureLoopConflict


class PostgresAgentStructureLoopRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

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
        head_scope = (
            project_id,
            document_id,
            source_sha256,
            base_artifact_id,
            base_projection_sha256,
        )
        with self._database.connection() as connection:
            # All working lineages for the same verified document source share one
            # transaction lock. A different base projection therefore cannot race
            # an older head into visibility.
            _lock_stream(connection, (project_id, document_id, source_sha256))
            # The shared source lock also serializes engineer edits and source
            # replacement. Keep the row locked until this Map head is visible.
            document = connection.execute(
                "SELECT source_sha256 FROM sor_documents WHERE id=%s AND project_id=%s FOR UPDATE",
                (document_id, project_id),
            ).fetchone()
            if document is None or document["source_sha256"] != source_sha256:
                raise AgentStructureLoopConflict("working revision source changed before publication")
            replay_row = connection.execute(
                """
                SELECT *
                FROM agent_structure_working_revisions
                WHERE project_id = %s
                  AND sor_document_id = %s
                  AND run_id = %s
                  AND revision = %s
                """,
                (project_id, document_id, verified.run_id, verified.revision),
            ).fetchone()
            if replay_row is not None:
                replay = _working_revision_object(replay_row)
                if replay is None or replay != record:
                    raise AgentStructureLoopConflict(
                        "working revision replay changed payload"
                    )
                return replay.head(), False
            head_row = connection.execute(
                """
                SELECT *
                FROM agent_structure_working_revision_heads
                WHERE project_id = %s
                  AND sor_document_id = %s
                  AND source_sha256 = %s
                  AND base_artifact_id = %s
                  AND base_projection_sha256 = %s
                FOR UPDATE
                """,
                head_scope,
            ).fetchone()
            if verified.revision == 1:
                if head_row is not None or expected_head_revision_sha256 is not None:
                    raise AgentStructureLoopConflict(
                        "working revision head changed"
                    )
            else:
                head_revision_sha256 = (
                    None if head_row is None else str(head_row["revision_sha256"])
                )
                if (
                    head_row is None
                    or str(head_row["run_id"]) != verified.run_id
                    or int(head_row["revision"]) != verified.revision - 1
                    or str(head_row["state"]) == "ACCEPTED"
                    or head_revision_sha256 != verified.previous_revision_sha256
                    or head_revision_sha256 != expected_head_revision_sha256
                ):
                    raise AgentStructureLoopConflict(
                        "working revision head changed"
                    )
            collision = connection.execute(
                """
                SELECT *
                FROM agent_structure_working_revisions
                WHERE revision_id = %s
                   OR (
                        project_id = %s
                    AND sor_document_id = %s
                    AND revision_sha256 = %s
                   )
                   OR object_key = %s
                """,
                (
                    record.revision_id,
                    project_id,
                    document_id,
                    record.revision_sha256,
                    record.object_key,
                ),
            ).fetchone()
            if collision is not None:
                raise AgentStructureLoopConflict(
                    "working revision identity owns another payload"
                )
            connection.execute(
                """
                INSERT INTO agent_structure_working_revisions (
                    revision_id, project_id, sor_document_id, run_id,
                    handoff_id, source_sha256, base_artifact_id,
                    base_projection_sha256, evidence_artifact_id, revision,
                    previous_revision_sha256, revision_sha256, validator_kind,
                    validator_receipt_sha256, d2_compiler_receipt_sha256,
                    validation_status, state,
                    quality_claim, object_key, object_sha256, object_size,
                    media_type, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    record.revision_id,
                    record.project_id,
                    record.document_id,
                    record.run_id,
                    record.handoff_id,
                    record.source_sha256,
                    record.base_artifact_id,
                    record.base_projection_sha256,
                    record.evidence_artifact_id,
                    record.revision,
                    record.previous_revision_sha256,
                    record.revision_sha256,
                    record.validator_kind,
                    record.validator_receipt_sha256,
                    record.d2_compiler_receipt_sha256,
                    "PASS",
                    record.state,
                    record.quality_claim,
                    record.object_key,
                    record.object_sha256,
                    record.object_size,
                    record.media_type,
                    record.created_at_utc,
                ),
            )
            connection.execute(
                """
                INSERT INTO agent_structure_working_revision_heads (
                    project_id, sor_document_id, source_sha256,
                    base_artifact_id, base_projection_sha256, revision_id,
                    run_id, revision, revision_sha256, state, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
                )
                ON CONFLICT (
                    project_id, sor_document_id, source_sha256,
                    base_artifact_id, base_projection_sha256
                ) DO UPDATE SET
                    revision_id = EXCLUDED.revision_id,
                    run_id = EXCLUDED.run_id,
                    revision = EXCLUDED.revision,
                    revision_sha256 = EXCLUDED.revision_sha256,
                    state = EXCLUDED.state,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    *head_scope,
                    record.revision_id,
                    record.run_id,
                    record.revision,
                    record.revision_sha256,
                    record.state,
                ),
            )
            from reqpilotx_server.application.agent_workflow.source_item_manual_publication import (
                invalidate_manual_for_unbound_map_in_transaction,
            )
            invalidate_manual_for_unbound_map_in_transaction(
                connection, project_id=project_id, document_id=document_id,
                source_sha256=source_sha256, publication_id=record.revision_id,
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
        clauses = ""
        values: tuple[object, ...] = (project_id, document_id, source_sha256)
        if base_artifact_id is not None:
            clauses += " AND head.base_artifact_id = %s"
            values += (base_artifact_id,)
        if base_projection_sha256 is not None:
            clauses += " AND head.base_projection_sha256 = %s"
            values += (base_projection_sha256,)
        with self._database.connection() as connection:
            row = connection.execute(
                f"""
                SELECT revision.*
                FROM agent_structure_working_revision_heads AS head
                JOIN agent_structure_working_revisions AS revision
                  ON revision.revision_id = head.revision_id
                WHERE head.project_id = %s
                  AND head.sor_document_id = %s
                  AND head.source_sha256 = %s
                  {clauses}
                ORDER BY head.updated_at DESC, head.revision DESC
                LIMIT 1
                """,
                values,
            ).fetchone()
        record = _working_revision_object(row)
        return None if record is None else record.head()

    def get_working_revision_object(
        self,
        *,
        project_id: str,
        document_id: str,
        run_id: str,
        revision: int,
    ) -> NativeStructureWorkingRevisionObjectV1 | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM agent_structure_working_revisions
                WHERE project_id = %s
                  AND sor_document_id = %s
                  AND run_id = %s
                  AND revision = %s
                """,
                (project_id, document_id, run_id, revision),
            ).fetchone()
        return _working_revision_object(row)

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
        request_payload = request.model_dump(mode="json")
        receipt_payload = receipt.model_dump(mode="json")
        request_payload_sha256 = _canonical_sha256(request_payload)
        receipt_payload_sha256 = _canonical_sha256(
            {
                key: value
                for key, value in receipt_payload.items()
                if key not in {"receipt_sha256", "created_at_utc"}
            }
        )
        if request_payload_sha256 != receipt.request_sha256:
            raise AgentStructureLoopConflict("attempt request SHA drifted")
        if receipt_payload_sha256 != receipt.receipt_sha256:
            raise AgentStructureLoopConflict("attempt receipt SHA drifted")
        scope = (project_id, document_id, source_sha256, artifact_id)
        with self._database.connection() as connection:
            _lock_stream(connection, scope)
            replay = connection.execute(
                """
                SELECT *
                FROM agent_structure_handoff_attempts
                WHERE project_id = %s
                  AND sor_document_id = %s
                  AND source_sha256 = %s
                  AND artifact_id = %s
                  AND handoff_id = %s
                  AND command_id = %s
                """,
                (*scope, request.handoff_id, request.command_id),
            ).fetchone()
            if replay is not None:
                persisted = _attempt(replay)
                if (
                    persisted is None
                    or persisted != receipt
                    or _mapping(replay["request_payload"]) != request_payload
                ):
                    raise AgentStructureLoopConflict(
                        "attempt command replay changed payload"
                    )
                return persisted, False
            if (
                expected_global_head_attempt_id is not None
                or expected_global_projection_attempts_sha256 is not None
            ):
                projection_rows = connection.execute(
                    """
                    SELECT receipt_payload
                    FROM agent_structure_handoff_attempts
                    WHERE project_id = %s
                      AND sor_document_id = %s
                      AND source_sha256 = %s
                      AND artifact_id = %s
                    ORDER BY created_at, handoff_id, attempt_ordinal, attempt_id
                    """,
                    scope,
                ).fetchall()
                projection_attempts = tuple(
                    NativeStructureHandoffAttemptReceiptV1.model_validate(
                        _mapping(row["receipt_payload"])
                    )
                    for row in projection_rows
                )
                if expected_global_head_attempt_id is not None:
                    global_head = active_structure_projection_attempt(
                        projection_attempts
                    )
                    if (
                        global_head is None
                        or global_head.attempt_id
                        != expected_global_head_attempt_id
                    ):
                        raise AgentStructureLoopConflict(
                            "global projection head changed"
                        )
                if expected_global_projection_attempts_sha256 is not None:
                    current_attempts_sha256 = (
                        canonical_structure_projection_attempts_sha256(
                            projection_attempts
                        )
                    )
                    if (
                        current_attempts_sha256
                        != expected_global_projection_attempts_sha256
                    ):
                        raise AgentStructureLoopConflict(
                            "global projection attempt set changed"
                        )
            ordinal = connection.execute(
                """
                SELECT attempt_id, receipt_sha256, attempt_ordinal
                FROM agent_structure_handoff_attempts
                WHERE project_id = %s
                  AND sor_document_id = %s
                  AND source_sha256 = %s
                  AND artifact_id = %s
                  AND handoff_id = %s
                ORDER BY attempt_ordinal DESC
                LIMIT 1
                """,
                (*scope, request.handoff_id),
            ).fetchone()
            expected_ordinal = (
                1 if ordinal is None else int(ordinal["attempt_ordinal"]) + 1
            )
            expected_previous = (
                None if ordinal is None else str(ordinal["receipt_sha256"])
            )
            if (
                request.attempt_ordinal != expected_ordinal
                or request.previous_attempt_receipt_sha256 != expected_previous
            ):
                raise AgentStructureLoopConflict(
                    "attempt does not continue the durable stream head"
                )
            collision = connection.execute(
                """
                SELECT *
                FROM agent_structure_handoff_attempts
                WHERE attempt_id = %s
                   OR (
                        project_id = %s
                    AND sor_document_id = %s
                    AND source_sha256 = %s
                    AND artifact_id = %s
                    AND handoff_id = %s
                    AND attempt_ordinal = %s
                   )
                """,
                (
                    receipt.attempt_id,
                    *scope,
                    request.handoff_id,
                    request.attempt_ordinal,
                ),
            ).fetchone()
            if collision is not None:
                raise AgentStructureLoopConflict(
                    "attempt identity or ordinal already owns another payload"
                )
            connection.execute(
                """
                INSERT INTO agent_structure_handoff_attempts (
                    attempt_id, project_id, sor_document_id, source_sha256,
                    artifact_id, handoff_id, command_id, attempt_ordinal,
                    previous_attempt_receipt_sha256, status, request_sha256,
                    receipt_sha256, request_payload, receipt_payload, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb, %s::jsonb, %s
                )
                """,
                (
                    receipt.attempt_id,
                    *scope,
                    request.handoff_id,
                    request.command_id,
                    request.attempt_ordinal,
                    request.previous_attempt_receipt_sha256,
                    receipt.status.value,
                    receipt.request_sha256,
                    receipt.receipt_sha256,
                    _json(request_payload),
                    _json(receipt_payload),
                    receipt.created_at_utc,
                ),
            )
            return receipt, True

    def list_attempts(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        artifact_id: str,
        handoff_id: str | None = None,
    ) -> tuple[NativeStructureHandoffAttemptReceiptV1, ...]:
        clauses = "" if handoff_id is None else " AND handoff_id = %s"
        values: tuple[object, ...] = (
            project_id,
            document_id,
            source_sha256,
            artifact_id,
        ) + ((handoff_id,) if handoff_id is not None else ())
        with self._database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM agent_structure_handoff_attempts
                WHERE project_id = %s
                  AND sor_document_id = %s
                  AND source_sha256 = %s
                  AND artifact_id = %s
                  {clauses}
                ORDER BY handoff_id, attempt_ordinal
                """,  # noqa: S608 -- optional clause is internal, never user text.
                values,
            ).fetchall()
        return tuple(item for row in rows if (item := _attempt(row)) is not None)

    def get_attempt_request(
        self,
        *,
        project_id: str,
        document_id: str,
        attempt_id: str,
    ) -> NativeStructureHandoffAttemptRequestV1 | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT request_payload
                FROM agent_structure_handoff_attempts
                WHERE attempt_id = %s
                  AND project_id = %s
                  AND sor_document_id = %s
                """,
                (attempt_id, project_id, document_id),
            ).fetchone()
        if row is None:
            return None
        return NativeStructureHandoffAttemptRequestV1.model_validate(
            _mapping(row["request_payload"])
        )

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
        scope = (project_id, document_id, source_sha256, artifact_id, handoff_id)
        draft_payload = draft.model_dump(mode="json")
        command_payload_sha256 = _canonical_sha256(draft_payload)
        run_id = stable_structure_loop_run_id(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_id=artifact_id,
            handoff_id=handoff_id,
        )
        with self._database.connection() as connection:
            _lock_stream(connection, scope)
            replay = connection.execute(
                """
                SELECT *
                FROM agent_structure_activity_events
                WHERE project_id = %s
                  AND sor_document_id = %s
                  AND source_sha256 = %s
                  AND artifact_id = %s
                  AND handoff_id = %s
                  AND command_id = %s
                """,
                (*scope, command_id),
            ).fetchone()
            if replay is not None:
                event = _activity(replay)
                if (
                    event is None
                    or str(replay["command_payload_sha256"]) != command_payload_sha256
                ):
                    raise AgentStructureLoopConflict(
                        "activity command replay changed payload"
                    )
                return event, False
            previous = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) AS sequence
                FROM agent_structure_activity_events
                WHERE project_id = %s
                  AND sor_document_id = %s
                  AND source_sha256 = %s
                  AND artifact_id = %s
                  AND handoff_id = %s
                """,
                scope,
            ).fetchone()
            sequence = int((previous or {}).get("sequence") or 0) + 1
            event = materialize_structure_loop_activity(
                draft,
                run_id=run_id,
                source_sha256=source_sha256,
                sequence=sequence,
            )
            event_payload = event.model_dump(mode="json")
            connection.execute(
                """
                INSERT INTO agent_structure_activity_events (
                    event_id, project_id, sor_document_id, source_sha256,
                    artifact_id, handoff_id, run_id, sequence, command_id,
                    command_payload_sha256, event_payload_sha256, event_payload,
                    occurred_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s
                )
                """,
                (
                    event.event_id,
                    *scope,
                    event.run_id,
                    event.sequence,
                    command_id,
                    command_payload_sha256,
                    _canonical_sha256(event_payload),
                    _json(event_payload),
                    event.occurred_at_utc,
                ),
            )
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
        clauses = "" if handoff_id is None else " AND handoff_id = %s"
        values: tuple[object, ...] = (
            (
                project_id,
                document_id,
                source_sha256,
                artifact_id,
            )
            + ((handoff_id,) if handoff_id is not None else ())
            + (after_sequence,)
        )
        with self._database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM agent_structure_activity_events
                WHERE project_id = %s
                  AND sor_document_id = %s
                  AND source_sha256 = %s
                  AND artifact_id = %s
                  {clauses}
                  AND sequence > %s
                ORDER BY occurred_at, run_id, sequence
                """,  # noqa: S608 -- optional clause is internal, never user text.
                values,
            ).fetchall()
        return tuple(item for row in rows if (item := _activity(row)) is not None)


def _lock_stream(connection: Any, values: tuple[str, ...]) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("\x1f".join(("agent-structure-loop/v1", *values)),),
    )


def _attempt(
    row: Mapping[str, Any] | None,
) -> NativeStructureHandoffAttemptReceiptV1 | None:
    if row is None:
        return None
    return NativeStructureHandoffAttemptReceiptV1.model_validate(
        _mapping(row["receipt_payload"])
    )


def _activity(row: Mapping[str, Any] | None) -> AgentActivityEventV2 | None:
    if row is None:
        return None
    return AgentActivityEventV2.model_validate(_mapping(row["event_payload"]))


def _working_revision_object(
    row: Mapping[str, Any] | None,
) -> NativeStructureWorkingRevisionObjectV1 | None:
    if row is None:
        return None
    return NativeStructureWorkingRevisionObjectV1(
        project_id=str(row["project_id"]),
        document_id=str(row["sor_document_id"]),
        run_id=str(row["run_id"]),
        handoff_id=(
            None if row.get("handoff_id") is None else str(row["handoff_id"])
        ),
        source_sha256=str(row["source_sha256"]),
        base_artifact_id=str(row["base_artifact_id"]),
        base_projection_sha256=str(row["base_projection_sha256"]),
        evidence_artifact_id=str(row["evidence_artifact_id"]),
        revision_id=str(row["revision_id"]),
        revision=int(row["revision"]),
        previous_revision_sha256=(
            None
            if row.get("previous_revision_sha256") is None
            else str(row["previous_revision_sha256"])
        ),
        revision_sha256=str(row["revision_sha256"]),
        validator_kind=str(row["validator_kind"]),
        validator_receipt_sha256=str(row["validator_receipt_sha256"]),
        d2_compiler_receipt_sha256=(
            None
            if row.get("d2_compiler_receipt_sha256") is None
            else str(row["d2_compiler_receipt_sha256"])
        ),
        state=str(row["state"]),
        quality_claim=str(row["quality_claim"]),
        object_key=str(row["object_key"]),
        object_sha256=str(row["object_sha256"]),
        object_size=int(row["object_size"]),
        media_type=str(row["media_type"]),
        created_at_utc=row["created_at"].astimezone(UTC),
    )


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


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): nested for key, nested in value.items()}
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return {str(key): nested for key, nested in parsed.items()}
    raise AgentStructureLoopConflict("durable structure-loop payload is invalid")


__all__ = ["PostgresAgentStructureLoopRepository"]
