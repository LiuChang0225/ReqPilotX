"""SQL persistence for neutral captures and reading-only records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app import db
from app.domain.reading import (
    DocumentReadingOverviewV1,
    ReadingArtifactV1,
    ReadingChatMessageV1,
    ReadingConversationStatus,
    ReadingConversationV1,
    ReadingMessageRole,
    ReadingNoteV1,
    ReadingSelectableNodeV1,
    ReadingStatus,
    ReadingStructureMapV1,
)
from app.domain.source_visual_capture import stable_effective_bundle_id
from app.ports.reading_repository import (
    ReadingRepositoryConflict,
    ReadingRepositoryCorrupt,
    ReadingSessionRecord,
)


class SqlReadingRepository:
    """Reading-owned persistence; it has no source-fact write capability."""

    def __init__(self) -> None:
        if not db.is_postgresql_runtime():
            _ensure_sqlite_chat_schema()

    def mark_session_bundle_stale(
        self,
        *,
        reading_session_id: str,
        updated_at_utc: datetime,
    ) -> None:
        _require_non_blank(reading_session_id, "reading_session_id")
        updated_at = _utc_iso(updated_at_utc)
        with db.get_conn() as connection:
            message_rows = connection.execute(
                """
                SELECT message_id, payload_json, payload_sha256
                FROM reading_chat_messages
                WHERE reading_session_id = ? AND status = 'ACTIVE'
                """,
                (reading_session_id,),
            ).fetchall()
            for row in message_rows:
                message = _decode_model_row(
                    row, ReadingChatMessageV1, "reading chat message"
                )
                stale = ReadingChatMessageV1.model_validate(
                    message.model_copy(
                        update={"status": ReadingConversationStatus.STALE}
                    ).model_dump(mode="python")
                )
                payload = _canonical_model_json(stale)
                connection.execute(
                    """
                    UPDATE reading_chat_messages
                    SET status = 'STALE', payload_json = ?, payload_sha256 = ?
                    WHERE message_id = ? AND status = 'ACTIVE'
                    """,
                    (payload, _sha256(payload), message.message_id),
                )
            conversation_rows = connection.execute(
                """
                SELECT conversation_id, payload_json, payload_sha256
                FROM reading_conversations
                WHERE reading_session_id = ? AND status = 'ACTIVE'
                """,
                (reading_session_id,),
            ).fetchall()
            for row in conversation_rows:
                conversation = _decode_model_row(
                    row, ReadingConversationV1, "reading conversation"
                )
                stale = ReadingConversationV1.model_validate(
                    conversation.model_copy(
                        update={
                            "status": ReadingConversationStatus.STALE,
                            "updated_at_utc": updated_at_utc,
                        }
                    ).model_dump(mode="python")
                )
                payload = _canonical_model_json(stale)
                connection.execute(
                    """
                    UPDATE reading_conversations
                    SET status = 'STALE', updated_at_utc = ?,
                        payload_json = ?, payload_sha256 = ?
                    WHERE conversation_id = ? AND status = 'ACTIVE'
                    """,
                    (
                        updated_at,
                        payload,
                        _sha256(payload),
                        conversation.conversation_id,
                    ),
                )
            artifact_rows = connection.execute(
                """
                SELECT artifact_id, payload_json, payload_sha256
                FROM reading_artifacts
                WHERE reading_session_id = ? AND status <> 'STALE'
                """,
                (reading_session_id,),
            ).fetchall()
            for row in artifact_rows:
                artifact = _decode_model_row(
                    row, ReadingArtifactV1, "reading artifact"
                )
                stale = ReadingArtifactV1.model_validate(
                    artifact.model_copy(update={"status": "STALE"}).model_dump(
                        mode="python"
                    )
                )
                payload = _canonical_model_json(stale)
                connection.execute(
                    """
                    UPDATE reading_artifacts
                    SET status = 'STALE', payload_json = ?, payload_sha256 = ?
                    WHERE artifact_id = ? AND status <> 'STALE'
                    """,
                    (payload, _sha256(payload), artifact.artifact_id),
                )
            note_rows = connection.execute(
                """
                SELECT note_id, payload_json, payload_sha256
                FROM reading_notes
                WHERE reading_session_id = ? AND status = 'ACTIVE'
                """,
                (reading_session_id,),
            ).fetchall()
            for row in note_rows:
                note = _decode_model_row(row, ReadingNoteV1, "reading note")
                stale = ReadingNoteV1.model_validate(
                    note.model_copy(
                        update={"status": "STALE", "updated_at_utc": updated_at_utc}
                    ).model_dump(mode="python")
                )
                payload = _canonical_model_json(stale)
                connection.execute(
                    """
                    UPDATE reading_notes
                    SET status = 'STALE', payload_json = ?, payload_sha256 = ?,
                        updated_at_utc = ?
                    WHERE note_id = ? AND status = 'ACTIVE'
                    """,
                    (payload, _sha256(payload), updated_at, note.note_id),
                )
            connection.execute(
                """
                UPDATE reading_sessions
                SET status = 'STALE', updated_at_utc = ?
                WHERE reading_session_id = ? AND status <> 'STALE'
                """,
                (updated_at, reading_session_id),
            )

    def mark_other_document_versions_stale(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        current_source_sha256: str,
        updated_at_utc: datetime,
    ) -> None:
        _require_scope(project_id, sor_document_id, current_source_sha256)
        updated_at = _utc_iso(updated_at_utc)
        with db.get_conn() as connection:
            message_rows = connection.execute(
                """
                SELECT message_id, payload_json, payload_sha256
                FROM reading_chat_messages
                WHERE project_id = ? AND sor_document_id = ?
                  AND source_sha256 <> ? AND status = 'ACTIVE'
                """,
                (project_id, sor_document_id, current_source_sha256),
            ).fetchall()
            for row in message_rows:
                message = _decode_model_row(
                    row,
                    ReadingChatMessageV1,
                    "reading chat message",
                )
                stale_message = ReadingChatMessageV1.model_validate(
                    message.model_copy(
                        update={"status": ReadingConversationStatus.STALE}
                    ).model_dump(
                        mode="python"
                    )
                )
                payload_json = _canonical_model_json(stale_message)
                connection.execute(
                    """
                    UPDATE reading_chat_messages
                    SET status = 'STALE', payload_json = ?, payload_sha256 = ?
                    WHERE message_id = ? AND status = 'ACTIVE'
                    """,
                    (payload_json, _sha256(payload_json), message.message_id),
                )

            conversation_rows = connection.execute(
                """
                SELECT conversation_id, payload_json, payload_sha256
                FROM reading_conversations
                WHERE project_id = ? AND sor_document_id = ?
                  AND source_sha256 <> ? AND status = 'ACTIVE'
                """,
                (project_id, sor_document_id, current_source_sha256),
            ).fetchall()
            for row in conversation_rows:
                conversation = _decode_model_row(
                    row,
                    ReadingConversationV1,
                    "reading conversation",
                )
                stale_conversation = ReadingConversationV1.model_validate(
                    conversation.model_copy(
                        update={
                            "status": ReadingConversationStatus.STALE,
                            "updated_at_utc": updated_at_utc,
                        }
                    ).model_dump(mode="python")
                )
                payload_json = _canonical_model_json(stale_conversation)
                connection.execute(
                    """
                    UPDATE reading_conversations
                    SET status = 'STALE', updated_at_utc = ?,
                        payload_json = ?, payload_sha256 = ?
                    WHERE conversation_id = ? AND status = 'ACTIVE'
                    """,
                    (
                        updated_at,
                        payload_json,
                        _sha256(payload_json),
                        conversation.conversation_id,
                    ),
                )

            artifact_rows = connection.execute(
                """
                SELECT artifact_id, payload_json, payload_sha256
                FROM reading_artifacts
                WHERE project_id = ? AND sor_document_id = ?
                  AND source_sha256 <> ? AND status <> 'STALE'
                """,
                (project_id, sor_document_id, current_source_sha256),
            ).fetchall()
            for row in artifact_rows:
                artifact = _decode_model_row(
                    row,
                    ReadingArtifactV1,
                    "reading artifact",
                )
                stale = ReadingArtifactV1.model_validate(
                    artifact.model_copy(update={"status": "STALE"}).model_dump(
                        mode="python"
                    )
                )
                payload_json = _canonical_model_json(stale)
                connection.execute(
                    """
                    UPDATE reading_artifacts
                    SET status = 'STALE', payload_json = ?, payload_sha256 = ?
                    WHERE artifact_id = ? AND status <> 'STALE'
                    """,
                    (payload_json, _sha256(payload_json), artifact.artifact_id),
                )

            note_rows = connection.execute(
                """
                SELECT note_id, payload_json, payload_sha256
                FROM reading_notes
                WHERE project_id = ? AND sor_document_id = ?
                  AND source_sha256 <> ? AND status = 'ACTIVE'
                """,
                (project_id, sor_document_id, current_source_sha256),
            ).fetchall()
            for row in note_rows:
                note = _decode_model_row(row, ReadingNoteV1, "reading note")
                stale = ReadingNoteV1.model_validate(
                    note.model_copy(
                        update={"status": "STALE", "updated_at_utc": updated_at_utc}
                    ).model_dump(mode="python")
                )
                payload_json = _canonical_model_json(stale)
                connection.execute(
                    """
                    UPDATE reading_notes
                    SET status = 'STALE', payload_json = ?, payload_sha256 = ?,
                        updated_at_utc = ?
                    WHERE note_id = ? AND status = 'ACTIVE'
                    """,
                    (
                        payload_json,
                        _sha256(payload_json),
                        updated_at,
                        note.note_id,
                    ),
                )

            connection.execute(
                """
                UPDATE reading_sessions
                SET status = 'STALE', updated_at_utc = ?
                WHERE project_id = ? AND sor_document_id = ?
                  AND source_sha256 <> ? AND status <> 'STALE'
                """,
                (
                    updated_at,
                    project_id,
                    sor_document_id,
                    current_source_sha256,
                ),
            )

    def get_session(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        source_sha256: str,
    ) -> ReadingSessionRecord | None:
        _require_scope(project_id, sor_document_id, source_sha256)
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM reading_sessions
                WHERE project_id = ? AND sor_document_id = ? AND source_sha256 = ?
                """,
                (project_id, sor_document_id, source_sha256),
            ).fetchone()
        return _decode_session(row) if row is not None else None

    def upsert_session(self, record: ReadingSessionRecord) -> ReadingSessionRecord:
        _validate_session_record(record)
        overview_json = _canonical_model_json(record.overview)
        structure_map_json = _canonical_model_json(record.structure_map)
        selectable_json = _canonical_json(
            [item.model_dump(mode="json") for item in record.selectable_nodes]
        )
        created_at = _utc_iso(record.created_at_utc)
        updated_at = _utc_iso(record.updated_at_utc)
        with db.get_conn() as connection:
            _require_current_effective_bundle(
                connection,
                project_id=record.project_id,
                sor_document_id=record.sor_document_id,
                source_sha256=record.source_sha256,
                expected_effective_bundle_id=record.effective_bundle_id,
                expected_capture_id=record.capture_id,
            )
            existing = connection.execute(
                """
                SELECT reading_session_id, capture_id
                FROM reading_sessions
                WHERE project_id = ? AND sor_document_id = ? AND source_sha256 = ?
                """,
                (record.project_id, record.sor_document_id, record.source_sha256),
            ).fetchone()
            if existing is not None and (
                str(existing["reading_session_id"]) != record.reading_session_id
                or str(existing["capture_id"]) != record.capture_id
            ):
                raise ReadingRepositoryConflict(
                    "current document version already has another reading session"
                )
            binding = connection.execute(
                """
                SELECT capture_id
                FROM source_capture_bindings
                WHERE project_id = ? AND sor_document_id = ? AND source_sha256 = ?
                """,
                (record.project_id, record.sor_document_id, record.source_sha256),
            ).fetchone()
            if binding is None or str(binding["capture_id"]) != record.capture_id:
                raise ReadingRepositoryConflict(
                    "reading session capture is not bound to the current document version"
                )
            connection.execute(
                """
                INSERT INTO reading_sessions (
                    reading_session_id, project_id, sor_document_id,
                    source_sha256, capture_id, status,
                    overview_json, overview_sha256,
                    structure_map_json, structure_map_sha256,
                    selectable_nodes_json, selectable_nodes_sha256,
                    effective_bundle_id, visual_generation_id,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, sor_document_id, source_sha256) DO UPDATE SET
                    status = excluded.status,
                    overview_json = excluded.overview_json,
                    overview_sha256 = excluded.overview_sha256,
                    structure_map_json = excluded.structure_map_json,
                    structure_map_sha256 = excluded.structure_map_sha256,
                    selectable_nodes_json = excluded.selectable_nodes_json,
                    selectable_nodes_sha256 = excluded.selectable_nodes_sha256,
                    effective_bundle_id = excluded.effective_bundle_id,
                    visual_generation_id = excluded.visual_generation_id,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    record.reading_session_id,
                    record.project_id,
                    record.sor_document_id,
                    record.source_sha256,
                    record.capture_id,
                    record.status.value,
                    overview_json,
                    _sha256(overview_json),
                    structure_map_json,
                    _sha256(structure_map_json),
                    selectable_json,
                    _sha256(selectable_json),
                    record.effective_bundle_id,
                    record.visual_generation_id or "",
                    created_at,
                    updated_at,
                ),
            )
        stored = self.get_session(
            project_id=record.project_id,
            sor_document_id=record.sor_document_id,
            source_sha256=record.source_sha256,
        )
        if stored is None:
            raise ReadingRepositoryCorrupt("upserted reading session could not be read")
        return stored

    def append_artifact(
        self,
        artifact: ReadingArtifactV1,
        *,
        expected_effective_bundle_id: str,
    ) -> ReadingArtifactV1:
        payload_json = _canonical_model_json(artifact)
        payload_sha256 = _sha256(payload_json)
        source_ref_json = _canonical_model_json(artifact.source_ref)
        with db.get_conn() as connection:
            _require_current_effective_bundle(
                connection,
                project_id=artifact.project_id,
                sor_document_id=artifact.sor_document_id,
                source_sha256=artifact.source_sha256,
                expected_effective_bundle_id=expected_effective_bundle_id,
                expected_reading_session_id=artifact.reading_session_id,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO reading_artifacts (
                    artifact_id, reading_session_id, project_id, sor_document_id,
                    source_sha256, action, source_ref_json, source_ref_sha256,
                    output_text, provider_id, model_name, prompt_version, status,
                    payload_json, payload_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.reading_session_id,
                    artifact.project_id,
                    artifact.sor_document_id,
                    artifact.source_sha256,
                    artifact.action.value,
                    source_ref_json,
                    _sha256(source_ref_json),
                    artifact.output_text,
                    artifact.provider_id,
                    artifact.model_name,
                    artifact.prompt_version,
                    artifact.status,
                    payload_json,
                    payload_sha256,
                    _utc_iso(artifact.created_at_utc),
                ),
            )
            row = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM reading_artifacts
                WHERE artifact_id = ?
                """,
                (artifact.artifact_id,),
            ).fetchone()
        stored = _decode_model_row(row, ReadingArtifactV1, "reading artifact")
        if stored != artifact:
            raise ReadingRepositoryConflict(
                "reading artifact identity already contains another payload"
            )
        return stored

    def list_artifacts(self, reading_session_id: str) -> tuple[ReadingArtifactV1, ...]:
        _require_non_blank(reading_session_id, "reading_session_id")
        with db.get_conn() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM reading_artifacts
                WHERE reading_session_id = ?
                ORDER BY created_at_utc DESC, artifact_id DESC
                """,
                (reading_session_id,),
            ).fetchall()
        return tuple(
            _decode_model_row(row, ReadingArtifactV1, "reading artifact")
            for row in rows
        )

    def append_note(
        self,
        note: ReadingNoteV1,
        *,
        expected_effective_bundle_id: str,
    ) -> ReadingNoteV1:
        payload_json = _canonical_model_json(note)
        payload_sha256 = _sha256(payload_json)
        source_ref_json = _canonical_model_json(note.source_ref)
        with db.get_conn() as connection:
            _require_current_effective_bundle(
                connection,
                project_id=note.project_id,
                sor_document_id=note.sor_document_id,
                source_sha256=note.source_sha256,
                expected_effective_bundle_id=expected_effective_bundle_id,
                expected_reading_session_id=note.reading_session_id,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO reading_notes (
                    note_id, reading_session_id, project_id, sor_document_id,
                    source_sha256, source_ref_json, source_ref_sha256,
                    content, revision, status, payload_json, payload_sha256,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note.note_id,
                    note.reading_session_id,
                    note.project_id,
                    note.sor_document_id,
                    note.source_sha256,
                    source_ref_json,
                    _sha256(source_ref_json),
                    note.content,
                    note.revision,
                    note.status,
                    payload_json,
                    payload_sha256,
                    _utc_iso(note.created_at_utc),
                    _utc_iso(note.updated_at_utc),
                ),
            )
            row = connection.execute(
                "SELECT payload_json, payload_sha256 FROM reading_notes WHERE note_id = ?",
                (note.note_id,),
            ).fetchone()
        stored = _decode_model_row(row, ReadingNoteV1, "reading note")
        if stored != note:
            raise ReadingRepositoryConflict(
                "reading note identity already contains another payload"
            )
        return stored

    def list_notes(self, reading_session_id: str) -> tuple[ReadingNoteV1, ...]:
        _require_non_blank(reading_session_id, "reading_session_id")
        with db.get_conn() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM reading_notes
                WHERE reading_session_id = ? AND status <> 'DELETED'
                ORDER BY updated_at_utc DESC, note_id DESC
                """,
                (reading_session_id,),
            ).fetchall()
        return tuple(
            _decode_model_row(row, ReadingNoteV1, "reading note") for row in rows
        )

    def update_note(
        self,
        note: ReadingNoteV1,
        *,
        expected_revision: int,
        expected_effective_bundle_id: str,
    ) -> ReadingNoteV1:
        if note.revision != expected_revision + 1:
            raise ValueError("updated note revision must increment expected_revision")
        payload_json = _canonical_model_json(note)
        payload_sha256 = _sha256(payload_json)
        with db.get_conn() as connection:
            _require_current_effective_bundle(
                connection,
                project_id=note.project_id,
                sor_document_id=note.sor_document_id,
                source_sha256=note.source_sha256,
                expected_effective_bundle_id=expected_effective_bundle_id,
                expected_reading_session_id=note.reading_session_id,
            )
            current_row = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM reading_notes
                WHERE note_id = ? AND reading_session_id = ?
                """,
                (note.note_id, note.reading_session_id),
            ).fetchone()
            current = _decode_model_row(
                current_row,
                ReadingNoteV1,
                "reading note",
            )
            if (
                current.status != "ACTIVE"
                or current.revision != expected_revision
                or _immutable_note_scope(current) != _immutable_note_scope(note)
            ):
                raise ReadingRepositoryConflict(
                    "reading note scope or revision is stale"
                )
            cursor = connection.execute(
                """
                UPDATE reading_notes
                SET content = ?, revision = ?, status = ?,
                    payload_json = ?, payload_sha256 = ?, updated_at_utc = ?
                WHERE note_id = ? AND reading_session_id = ?
                  AND revision = ? AND status = 'ACTIVE'
                """,
                (
                    note.content,
                    note.revision,
                    note.status,
                    payload_json,
                    payload_sha256,
                    _utc_iso(note.updated_at_utc),
                    note.note_id,
                    note.reading_session_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ReadingRepositoryConflict("reading note revision is stale")
        return note

    def delete_note(
        self,
        *,
        reading_session_id: str,
        note_id: str,
        expected_revision: int,
        expected_effective_bundle_id: str,
        updated_at_utc: datetime,
    ) -> ReadingNoteV1:
        _require_non_blank(reading_session_id, "reading_session_id")
        _require_non_blank(note_id, "note_id")
        with db.get_conn() as connection:
            if not db.is_postgresql_runtime():
                connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM reading_notes
                WHERE note_id = ? AND reading_session_id = ?
                """,
                (note_id, reading_session_id),
            ).fetchone()
            current = _decode_model_row(row, ReadingNoteV1, "reading note")
            _require_current_effective_bundle(
                connection,
                project_id=current.project_id,
                sor_document_id=current.sor_document_id,
                source_sha256=current.source_sha256,
                expected_effective_bundle_id=expected_effective_bundle_id,
                expected_reading_session_id=current.reading_session_id,
                transaction_started=True,
            )
            if current.status != "ACTIVE" or current.revision != expected_revision:
                raise ReadingRepositoryConflict("reading note revision is stale")
            deleted = current.model_copy(
                update={
                    "status": "DELETED",
                    "revision": current.revision + 1,
                    "updated_at_utc": updated_at_utc,
                }
            )
            deleted = ReadingNoteV1.model_validate(deleted.model_dump(mode="python"))
            payload_json = _canonical_model_json(deleted)
            cursor = connection.execute(
                """
                UPDATE reading_notes
                SET revision = ?, status = 'DELETED', payload_json = ?,
                    payload_sha256 = ?, updated_at_utc = ?
                WHERE note_id = ? AND reading_session_id = ?
                  AND revision = ? AND status = 'ACTIVE'
                """,
                (
                    deleted.revision,
                    payload_json,
                    _sha256(payload_json),
                    _utc_iso(updated_at_utc),
                    note_id,
                    reading_session_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ReadingRepositoryConflict("reading note revision is stale")
        return deleted

    def append_conversation(
        self,
        conversation: ReadingConversationV1,
    ) -> ReadingConversationV1:
        payload_json = _canonical_model_json(conversation)
        with db.get_conn() as connection:
            _require_current_effective_bundle(
                connection,
                project_id=conversation.project_id,
                sor_document_id=conversation.sor_document_id,
                source_sha256=conversation.source_sha256,
                expected_effective_bundle_id=(
                    conversation.effective_bundle_id or ""
                ),
                expected_reading_session_id=conversation.reading_session_id,
            )
            session = connection.execute(
                """
                SELECT reading_session_id, status, effective_bundle_id
                FROM reading_sessions
                WHERE reading_session_id = ? AND project_id = ?
                  AND sor_document_id = ? AND source_sha256 = ?
                """,
                (
                    conversation.reading_session_id,
                    conversation.project_id,
                    conversation.sor_document_id,
                    conversation.source_sha256,
                ),
            ).fetchone()
            if (
                session is None
                or str(session["status"]) not in {"READY", "PARTIAL"}
                or not conversation.effective_bundle_id
                or str(session["effective_bundle_id"])
                != conversation.effective_bundle_id
            ):
                raise ReadingRepositoryConflict(
                    "reading conversation does not bind a current reading session"
                )
            document = connection.execute(
                """
                SELECT id
                FROM sor_documents
                WHERE id = ? AND project_id = ? AND source_sha256 = ?
                """,
                (
                    conversation.sor_document_id,
                    conversation.project_id,
                    conversation.source_sha256,
                ),
            ).fetchone()
            if document is None:
                raise ReadingRepositoryConflict(
                    "reading conversation source identity is no longer current"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO reading_conversations (
                    conversation_id, reading_session_id, project_id,
                    sor_document_id, source_sha256, title, status,
                    payload_json, payload_sha256, created_at_utc,
                    updated_at_utc, last_message_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation.conversation_id,
                    conversation.reading_session_id,
                    conversation.project_id,
                    conversation.sor_document_id,
                    conversation.source_sha256,
                    conversation.title,
                    conversation.status.value,
                    payload_json,
                    _sha256(payload_json),
                    _utc_iso(conversation.created_at_utc),
                    _utc_iso(conversation.updated_at_utc),
                    (
                        _utc_iso(conversation.last_message_at_utc)
                        if conversation.last_message_at_utc is not None
                        else None
                    ),
                ),
            )
        stored = self.get_conversation(conversation.conversation_id)
        if stored is None:
            raise ReadingRepositoryCorrupt(
                "inserted reading conversation could not be read"
            )
        if stored != conversation:
            raise ReadingRepositoryConflict(
                "reading conversation identity already contains another payload"
            )
        return stored

    def get_conversation(self, conversation_id: str) -> ReadingConversationV1 | None:
        _require_non_blank(conversation_id, "conversation_id")
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM reading_conversations
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return _decode_model_row(row, ReadingConversationV1, "reading conversation")

    def list_conversations(
        self,
        reading_session_id: str,
    ) -> tuple[ReadingConversationV1, ...]:
        _require_non_blank(reading_session_id, "reading_session_id")
        with db.get_conn() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM reading_conversations
                WHERE reading_session_id = ?
                ORDER BY updated_at_utc DESC, conversation_id DESC
                """,
                (reading_session_id,),
            ).fetchall()
        return tuple(
            _decode_model_row(row, ReadingConversationV1, "reading conversation")
            for row in rows
        )

    def list_messages(
        self,
        conversation_id: str,
    ) -> tuple[ReadingChatMessageV1, ...]:
        _require_non_blank(conversation_id, "conversation_id")
        with db.get_conn() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM reading_chat_messages
                WHERE conversation_id = ?
                ORDER BY sequence_number ASC, message_id ASC
                """,
                (conversation_id,),
            ).fetchall()
        return tuple(
            _decode_model_row(row, ReadingChatMessageV1, "reading chat message")
            for row in rows
        )

    def append_chat_turn(
        self,
        *,
        conversation: ReadingConversationV1,
        user_message: ReadingChatMessageV1,
        assistant_message: ReadingChatMessageV1,
    ) -> tuple[ReadingConversationV1, ReadingChatMessageV1, ReadingChatMessageV1]:
        _validate_chat_turn(conversation, user_message, assistant_message)
        conversation_payload = _canonical_model_json(conversation)
        with db.get_conn() as connection:
            _require_current_effective_bundle(
                connection,
                project_id=conversation.project_id,
                sor_document_id=conversation.sor_document_id,
                source_sha256=conversation.source_sha256,
                expected_effective_bundle_id=(
                    conversation.effective_bundle_id or ""
                ),
                expected_reading_session_id=conversation.reading_session_id,
            )
            lock_suffix = " FOR UPDATE" if db.is_postgresql_runtime() else ""
            session = connection.execute(
                f"""
                SELECT status, effective_bundle_id
                FROM reading_sessions
                WHERE reading_session_id = ? AND project_id = ?
                  AND sor_document_id = ? AND source_sha256 = ?{lock_suffix}
                """,
                (
                    conversation.reading_session_id,
                    conversation.project_id,
                    conversation.sor_document_id,
                    conversation.source_sha256,
                ),
            ).fetchone()
            if (
                session is None
                or str(session["status"]) not in {"READY", "PARTIAL"}
                or str(session["effective_bundle_id"])
                != conversation.effective_bundle_id
            ):
                raise ReadingRepositoryConflict(
                    "reading conversation effective-source bundle is stale"
                )
            row = connection.execute(
                f"""
                SELECT payload_json, payload_sha256
                FROM reading_conversations
                WHERE conversation_id = ?{lock_suffix}
                """,
                (conversation.conversation_id,),
            ).fetchone()
            if row is None:
                raise ReadingRepositoryConflict(
                    "reading conversation was deleted while the answer was generated"
                )
            current = _decode_model_row(
                row,
                ReadingConversationV1,
                "reading conversation",
            )
            if (
                current.status is not ReadingConversationStatus.ACTIVE
                or _immutable_conversation_scope(current)
                != _immutable_conversation_scope(conversation)
                or conversation.revision != current.revision + 1
            ):
                raise ReadingRepositoryConflict(
                    "reading conversation scope is stale"
                )
            sequence_row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence_number), 0) AS last_sequence
                FROM reading_chat_messages
                WHERE conversation_id = ?
                """,
                (conversation.conversation_id,),
            ).fetchone()
            expected_sequence = int(sequence_row["last_sequence"] or 0) + 1
            if user_message.sequence_number != expected_sequence:
                raise ReadingRepositoryConflict(
                    "reading conversation changed while the answer was generated"
                )
            _insert_chat_message(connection, user_message)
            _insert_chat_message(connection, assistant_message)
            cursor = connection.execute(
                """
                UPDATE reading_conversations
                SET title = ?, status = ?, payload_json = ?, payload_sha256 = ?,
                    updated_at_utc = ?, last_message_at_utc = ?
                WHERE conversation_id = ? AND status = 'ACTIVE'
                """,
                (
                    conversation.title,
                    conversation.status.value,
                    conversation_payload,
                    _sha256(conversation_payload),
                    _utc_iso(conversation.updated_at_utc),
                    _utc_iso(conversation.last_message_at_utc),
                    conversation.conversation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ReadingRepositoryConflict("reading conversation is stale")
        stored_conversation = self.get_conversation(conversation.conversation_id)
        if stored_conversation is None:
            raise ReadingRepositoryCorrupt("stored reading conversation disappeared")
        messages = self.list_messages(conversation.conversation_id)
        stored_by_id = {item.message_id: item for item in messages}
        try:
            stored_user = stored_by_id[user_message.message_id]
            stored_assistant = stored_by_id[assistant_message.message_id]
        except KeyError as exc:
            raise ReadingRepositoryCorrupt("stored reading chat turn is incomplete") from exc
        return stored_conversation, stored_user, stored_assistant

    def clear_conversation(
        self,
        *,
        conversation: ReadingConversationV1,
    ) -> int:
        payload_json = _canonical_model_json(conversation)
        with db.get_conn() as connection:
            if not db.is_postgresql_runtime():
                connection.execute("BEGIN IMMEDIATE")
            lock_suffix = " FOR UPDATE" if db.is_postgresql_runtime() else ""
            row = connection.execute(
                f"""
                SELECT payload_json, payload_sha256
                FROM reading_conversations
                WHERE conversation_id = ?{lock_suffix}
                """,
                (conversation.conversation_id,),
            ).fetchone()
            if row is None:
                raise ReadingRepositoryConflict("reading conversation no longer exists")
            current = _decode_model_row(
                row,
                ReadingConversationV1,
                "reading conversation",
            )
            if (
                current.status is not ReadingConversationStatus.ACTIVE
                or _immutable_conversation_scope(current)
                != _immutable_conversation_scope(conversation)
                or conversation.revision != current.revision + 1
            ):
                raise ReadingRepositoryConflict("reading conversation is stale")
            deleted = connection.execute(
                "DELETE FROM reading_chat_messages WHERE conversation_id = ?",
                (conversation.conversation_id,),
            ).rowcount
            cursor = connection.execute(
                """
                UPDATE reading_conversations
                SET payload_json = ?, payload_sha256 = ?, updated_at_utc = ?,
                    last_message_at_utc = NULL
                WHERE conversation_id = ? AND status = 'ACTIVE'
                """,
                (
                    payload_json,
                    _sha256(payload_json),
                    _utc_iso(conversation.updated_at_utc),
                    conversation.conversation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ReadingRepositoryConflict("reading conversation is stale")
        return max(0, int(deleted))

    def delete_conversation(self, conversation_id: str) -> bool:
        _require_non_blank(conversation_id, "conversation_id")
        with db.get_conn() as connection:
            connection.execute(
                "DELETE FROM reading_chat_messages WHERE conversation_id = ?",
                (conversation_id,),
            )
            cursor = connection.execute(
                "DELETE FROM reading_conversations WHERE conversation_id = ?",
                (conversation_id,),
            )
        return cursor.rowcount == 1

    def purge_document(self, *, project_id: str, sor_document_id: str) -> None:
        _require_non_blank(project_id, "project_id")
        _require_non_blank(sor_document_id, "sor_document_id")
        with db.get_conn() as connection:
            connection.execute(
                "DELETE FROM reading_chat_messages WHERE project_id = ? AND sor_document_id = ?",
                (project_id, sor_document_id),
            )
            connection.execute(
                "DELETE FROM reading_conversations WHERE project_id = ? AND sor_document_id = ?",
                (project_id, sor_document_id),
            )
            connection.execute(
                "DELETE FROM reading_artifacts WHERE project_id = ? AND sor_document_id = ?",
                (project_id, sor_document_id),
            )
            connection.execute(
                "DELETE FROM reading_notes WHERE project_id = ? AND sor_document_id = ?",
                (project_id, sor_document_id),
            )
            connection.execute(
                "DELETE FROM reading_sessions WHERE project_id = ? AND sor_document_id = ?",
                (project_id, sor_document_id),
            )


def _ensure_sqlite_chat_schema() -> None:
    with db.get_conn() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS reading_conversations (
                conversation_id TEXT PRIMARY KEY,
                reading_session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                sor_document_id TEXT NOT NULL,
                source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'STALE')),
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                last_message_at_utc TEXT,
                UNIQUE(conversation_id, reading_session_id, project_id,
                       sor_document_id, source_sha256),
                FOREIGN KEY(reading_session_id, project_id, sor_document_id, source_sha256)
                    REFERENCES reading_sessions(
                        reading_session_id, project_id, sor_document_id, source_sha256
                    ) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_reading_conversations_session
                ON reading_conversations(
                    reading_session_id, status, updated_at_utc DESC
                );

            CREATE TABLE IF NOT EXISTS reading_chat_messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                reading_session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                sor_document_id TEXT NOT NULL,
                source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
                sequence_number INTEGER NOT NULL CHECK(sequence_number >= 1),
                role TEXT NOT NULL CHECK(role IN ('USER', 'ASSISTANT')),
                content TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'STALE')),
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
                created_at_utc TEXT NOT NULL,
                UNIQUE(conversation_id, sequence_number),
                FOREIGN KEY(conversation_id, reading_session_id, project_id,
                            sor_document_id, source_sha256)
                    REFERENCES reading_conversations(
                        conversation_id, reading_session_id, project_id,
                        sor_document_id, source_sha256
                    ) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_reading_chat_messages_conversation
                ON reading_chat_messages(
                    conversation_id, status, sequence_number ASC
                );
            """
        )


def _insert_chat_message(connection: Any, message: ReadingChatMessageV1) -> None:
    payload_json = _canonical_model_json(message)
    try:
        connection.execute(
            """
            INSERT INTO reading_chat_messages (
                message_id, conversation_id, reading_session_id, project_id,
                sor_document_id, source_sha256, sequence_number, role,
                content, status, payload_json, payload_sha256, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.message_id,
                message.conversation_id,
                message.reading_session_id,
                message.project_id,
                message.sor_document_id,
                message.source_sha256,
                message.sequence_number,
                message.role.value,
                message.content,
                message.status.value,
                payload_json,
                _sha256(payload_json),
                _utc_iso(message.created_at_utc),
            ),
        )
    except Exception as exc:
        raise ReadingRepositoryConflict(
            "reading chat message identity or sequence is stale"
        ) from exc


def _validate_chat_turn(
    conversation: ReadingConversationV1,
    user_message: ReadingChatMessageV1,
    assistant_message: ReadingChatMessageV1,
) -> None:
    scope = _immutable_conversation_scope(conversation)
    for message in (user_message, assistant_message):
        message_scope = (
            message.conversation_id,
            message.reading_session_id,
            message.project_id,
            message.sor_document_id,
            message.source_sha256,
            message.effective_bundle_id,
        )
        if message_scope != scope:
            raise ValueError("chat turn message scope must match conversation")
    if user_message.role is not ReadingMessageRole.USER:
        raise ValueError("chat turn requires a USER message first")
    if assistant_message.role is not ReadingMessageRole.ASSISTANT:
        raise ValueError("chat turn requires an ASSISTANT message second")
    if assistant_message.sequence_number != user_message.sequence_number + 1:
        raise ValueError("chat turn sequences must be consecutive")
    if conversation.status is not ReadingConversationStatus.ACTIVE:
        raise ValueError("chat turn conversation must be active")
    if conversation.last_message_at_utc != assistant_message.created_at_utc:
        raise ValueError("conversation last message timestamp must match assistant turn")


def _immutable_conversation_scope(
    conversation: ReadingConversationV1,
) -> tuple[str, str, str, str, str, str | None]:
    return (
        conversation.conversation_id,
        conversation.reading_session_id,
        conversation.project_id,
        conversation.sor_document_id,
        conversation.source_sha256,
        conversation.effective_bundle_id,
    )


def _require_current_effective_bundle(
    connection: Any,
    *,
    project_id: str,
    sor_document_id: str,
    source_sha256: str,
    expected_effective_bundle_id: str,
    expected_capture_id: str | None = None,
    expected_reading_session_id: str | None = None,
    transaction_started: bool = False,
) -> None:
    """Lock one document and compare a bundle rebuilt from neutral sidecars."""

    _require_scope(project_id, sor_document_id, source_sha256)
    _require_non_blank(expected_effective_bundle_id, "expected_effective_bundle_id")
    if not db.is_postgresql_runtime() and not transaction_started:
        connection.execute("BEGIN IMMEDIATE")
    lock_suffix = " FOR UPDATE" if db.is_postgresql_runtime() else ""
    document = connection.execute(
        f"""
        SELECT id FROM sor_documents
        WHERE id = ? AND project_id = ? AND source_sha256 = ?{lock_suffix}
        """,
        (sor_document_id, project_id, source_sha256),
    ).fetchone()
    if document is None:
        raise ReadingRepositoryConflict(
            "reading source identity is no longer current"
        )
    capture = connection.execute(
        """
        SELECT capture_id FROM source_capture_bindings
        WHERE project_id = ? AND sor_document_id = ? AND source_sha256 = ?
        """,
        (project_id, sor_document_id, source_sha256),
    ).fetchone()
    if capture is None:
        raise ReadingRepositoryConflict("current neutral source capture is missing")
    capture_id = str(capture["capture_id"])
    if expected_capture_id is not None and capture_id != expected_capture_id:
        raise ReadingRepositoryConflict("reading base capture is stale")
    visual_binding = connection.execute(
        """
        SELECT generation_id FROM source_visual_capture_bindings
        WHERE project_id = ? AND sor_document_id = ? AND source_sha256 = ?
        """,
        (project_id, sor_document_id, source_sha256),
    ).fetchone()
    visual_generation_id = (
        str(visual_binding["generation_id"])
        if visual_binding is not None
        else None
    )
    visual_snapshot_ids: tuple[str, ...] = ()
    if visual_generation_id is not None:
        snapshot_rows = connection.execute(
            """
            SELECT visual_snapshot_id FROM source_visual_page_snapshots
            WHERE generation_id = ? ORDER BY page_number
            """,
            (visual_generation_id,),
        ).fetchall()
        visual_snapshot_ids = tuple(
            str(row["visual_snapshot_id"]) for row in snapshot_rows
        )
    current_bundle_id = stable_effective_bundle_id(
        base_capture_id=capture_id,
        visual_generation_id=visual_generation_id,
        visual_snapshot_ids=visual_snapshot_ids,
    )
    if current_bundle_id != expected_effective_bundle_id:
        raise ReadingRepositoryConflict("reading effective-source bundle is stale")
    if expected_reading_session_id is not None:
        session = connection.execute(
            """
            SELECT status, effective_bundle_id FROM reading_sessions
            WHERE reading_session_id = ? AND project_id = ?
              AND sor_document_id = ? AND source_sha256 = ?
            """,
            (
                expected_reading_session_id,
                project_id,
                sor_document_id,
                source_sha256,
            ),
        ).fetchone()
        if (
            session is None
            or str(session["status"]) not in {"READY", "PARTIAL"}
            or str(session["effective_bundle_id"]) != expected_effective_bundle_id
        ):
            raise ReadingRepositoryConflict("reading session bundle is stale")

def _validate_session_record(record: ReadingSessionRecord) -> None:
    _require_scope(record.project_id, record.sor_document_id, record.source_sha256)
    _require_non_blank(record.reading_session_id, "reading_session_id")
    _require_non_blank(record.capture_id, "capture_id")
    _require_non_blank(record.effective_bundle_id, "effective_bundle_id")
    if record.updated_at_utc < record.created_at_utc:
        raise ValueError("session updated_at must not precede created_at")
    _utc_iso(record.created_at_utc)
    _utc_iso(record.updated_at_utc)


def _decode_session(row: Any) -> ReadingSessionRecord:
    overview_json = _verified_json(row, "overview")
    structure_map_json = _verified_json(row, "structure_map")
    selectable_json = _verified_json(row, "selectable_nodes")
    try:
        selectable_payload = json.loads(selectable_json)
    except json.JSONDecodeError as exc:
        raise ReadingRepositoryCorrupt("selectable nodes JSON is invalid") from exc
    if not isinstance(selectable_payload, list):
        raise ReadingRepositoryCorrupt("selectable nodes payload must be a list")
    try:
        return ReadingSessionRecord(
            reading_session_id=str(row["reading_session_id"]),
            project_id=str(row["project_id"]),
            sor_document_id=str(row["sor_document_id"]),
            source_sha256=str(row["source_sha256"]),
            capture_id=str(row["capture_id"]),
            status=ReadingStatus(str(row["status"])),
            overview=DocumentReadingOverviewV1.model_validate_json(overview_json),
            structure_map=ReadingStructureMapV1.model_validate_json(structure_map_json),
            selectable_nodes=tuple(
                ReadingSelectableNodeV1.model_validate(item)
                for item in selectable_payload
            ),
            effective_bundle_id=str(row["effective_bundle_id"]),
            visual_generation_id=(
                str(row["visual_generation_id"])
                if str(row["visual_generation_id"] or "").strip()
                else None
            ),
            created_at_utc=_parse_datetime(row["created_at_utc"]),
            updated_at_utc=_parse_datetime(row["updated_at_utc"]),
        )
    except (TypeError, ValueError) as exc:
        raise ReadingRepositoryCorrupt("reading session payload is invalid") from exc


def _decode_model_row(row: Any, model: Any, label: str):
    if row is None:
        raise ReadingRepositoryCorrupt(f"{label} does not exist")
    payload_json = str(row["payload_json"])
    if _sha256(payload_json) != str(row["payload_sha256"]):
        raise ReadingRepositoryCorrupt(f"{label} payload hash mismatch")
    try:
        return model.model_validate_json(payload_json)
    except (TypeError, ValueError) as exc:
        raise ReadingRepositoryCorrupt(f"{label} payload is invalid") from exc


def _immutable_note_scope(note: ReadingNoteV1) -> tuple[Any, ...]:
    return (
        note.note_id,
        note.reading_session_id,
        note.project_id,
        note.sor_document_id,
        note.source_sha256,
        note.source_ref,
        note.created_at_utc,
    )


def _verified_json(row: Any, prefix: str) -> str:
    payload = str(row[f"{prefix}_json"])
    if _sha256(payload) != str(row[f"{prefix}_sha256"]):
        raise ReadingRepositoryCorrupt(f"{prefix} payload hash mismatch")
    return payload


def _canonical_model_json(model: Any) -> str:
    return _canonical_json(model.model_dump(mode="json"))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_scope(project_id: str, document_id: str, source_sha256: str) -> None:
    _require_non_blank(project_id, "project_id")
    _require_non_blank(document_id, "sor_document_id")
    if len(source_sha256) != 64 or any(char not in "0123456789abcdef" for char in source_sha256):
        raise ValueError("source_sha256 must be 64 lowercase hexadecimal characters")


def _require_non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ReadingRepositoryCorrupt("stored timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReadingRepositoryCorrupt("stored timestamp must be timezone-aware")
    return parsed


__all__ = ["SqlReadingRepository"]
