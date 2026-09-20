from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import unittest
import hashlib
import json
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db
from app.adapters.persistence.sql_reading import SqlReadingRepository
from app.adapters.persistence.sql_source_capture import SqlSourceCaptureRepository
from app.application.reading_projection import build_reading_projection
from app.domain.reading import (
    DocumentReadingOverviewV1,
    ReadingAction,
    ReadingArtifactV1,
    ReadingChatCitationV1,
    ReadingChatMessageV1,
    ReadingConversationV1,
    ReadingMessageRole,
    ReadingRetrievalV1,
    ReadingNoteV1,
    ReadingSelectableNodeV1,
    ReadingSourceRefV1,
    ReadingStatus,
    ReadingStructureMapV1,
)
from app.domain.source_visual_capture import stable_effective_bundle_id
from app.ports.reading_repository import (
    ReadingRepositoryConflict,
    ReadingSessionRecord,
)
from app.ports.source_capture_repository import SourceCaptureRecord

if __package__:
    from .test_reading_projection import SOURCE_SHA256, _source_ir
else:
    from test_reading_projection import SOURCE_SHA256, _source_ir

EFFECTIVE_BUNDLE_ID = stable_effective_bundle_id(
    base_capture_id="capture-reading-test",
    visual_generation_id=None,
    visual_snapshot_ids=(),
)


class ReadingRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        db.init_db()
        self.project = db.create_project("Reading project", "Customer")
        self.document = db.add_sor_document(
            self.project["id"],
            "source.pdf",
            "PDF",
            str(self.root / "source.pdf"),
            "",
            source_sha256=SOURCE_SHA256,
            source_size=123,
            declared_extension=".pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
        )
        self.repository = SqlReadingRepository()
        self.source_repository = SqlSourceCaptureRepository()
        self.now = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)
        self.source_ir = _source_ir()

    def tearDown(self) -> None:
        self.stack.close()

    def _capture(self) -> SourceCaptureRecord:
        return SourceCaptureRecord(
            capture_id="capture-reading-test",
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            source_sha256=SOURCE_SHA256,
            source_ir=self.source_ir,
            created_at_utc=self.now,
        )

    def _session(self) -> ReadingSessionRecord:
        projection = build_reading_projection(self.source_ir)
        return ReadingSessionRecord(
            reading_session_id="reading-session-test",
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            source_sha256=SOURCE_SHA256,
            capture_id="capture-reading-test",
            status=ReadingStatus.READY,
            overview=DocumentReadingOverviewV1.model_validate(projection["overview"]),
            structure_map=ReadingStructureMapV1.model_validate(
                projection["structure_map"]
            ),
            selectable_nodes=tuple(
                ReadingSelectableNodeV1.model_validate(item)
                for item in projection["selectable_nodes"]
            ),
            created_at_utc=self.now,
            updated_at_utc=self.now,
            effective_bundle_id=EFFECTIVE_BUNDLE_ID,
        )

    def test_capture_and_session_are_idempotent_and_hash_verified(self) -> None:
        first = self.source_repository.append_capture(self._capture())
        second = self.source_repository.append_capture(self._capture())
        self.assertEqual(first, second)
        self.assertNotEqual(first.sor_document_id, first.source_ir.document_id)

        session = self.repository.upsert_session(self._session())
        replay = self.repository.upsert_session(self._session())
        self.assertEqual(session, replay)
        self.assertEqual(session.source_sha256, SOURCE_SHA256)

        with db.get_conn() as connection:
            connection.execute(
                "UPDATE reading_sessions SET overview_sha256 = ? WHERE reading_session_id = ?",
                ("f" * 64, session.reading_session_id),
            )
        with self.assertRaisesRegex(RuntimeError, "overview payload hash mismatch"):
            self.repository.get_session(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                source_sha256=SOURCE_SHA256,
            )

    def test_artifact_and_source_bound_note_lifecycle(self) -> None:
        self.source_repository.append_capture(self._capture())
        session = self.repository.upsert_session(self._session())
        source_ref = ReadingSourceRefV1(
            source_node_id=self.source_ir.nodes[1].source_node_id,
            start_offset=0,
            end_offset=2,
        )
        artifact = ReadingArtifactV1(
            artifact_id="reading-artifact-test",
            reading_session_id=session.reading_session_id,
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            source_sha256=session.source_sha256,
            action=ReadingAction.EXPLANATION,
            source_ref=source_ref,
            output_text="说明泵组。",
            provider_id="fake",
            model_name="fake-model",
            prompt_version="reading-action/v1",
            created_at_utc=self.now,
        )
        self.assertEqual(
            self.repository.append_artifact(
                artifact,
                expected_effective_bundle_id=session.effective_bundle_id,
            ),
            artifact,
        )
        self.assertEqual(self.repository.list_artifacts(session.reading_session_id), (artifact,))

        note = ReadingNoteV1(
            note_id="reading-note-test",
            reading_session_id=session.reading_session_id,
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            source_sha256=session.source_sha256,
            source_ref=source_ref,
            content="核对额定工况。",
            revision=1,
            created_at_utc=self.now,
            updated_at_utc=self.now,
        )
        self.assertEqual(
            self.repository.append_note(
                note,
                expected_effective_bundle_id=session.effective_bundle_id,
            ),
            note,
        )
        updated = ReadingNoteV1.model_validate(
            note.model_copy(
                update={
                    "content": "核对额定工况和持续时间。",
                    "revision": 2,
                    "updated_at_utc": self.now + timedelta(minutes=1),
                }
            ).model_dump(mode="python")
        )
        self.assertEqual(
            self.repository.update_note(
                updated,
                expected_revision=1,
                expected_effective_bundle_id=session.effective_bundle_id,
            ),
            updated,
        )
        with self.assertRaises(ReadingRepositoryConflict):
            self.repository.update_note(
                updated,
                expected_revision=1,
                expected_effective_bundle_id=session.effective_bundle_id,
            )
        deleted = self.repository.delete_note(
            reading_session_id=session.reading_session_id,
            note_id=note.note_id,
            expected_revision=2,
            expected_effective_bundle_id=session.effective_bundle_id,
            updated_at_utc=self.now + timedelta(minutes=2),
        )
        self.assertEqual(deleted.status, "DELETED")
        self.assertEqual(self.repository.list_notes(session.reading_session_id), ())

    def test_purge_removes_reading_data_and_last_private_capture(self) -> None:
        self.source_repository.append_capture(self._capture())
        self.repository.upsert_session(self._session())
        self.repository.purge_document(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
        )
        self.source_repository.purge_document(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
        )
        with db.get_conn() as connection:
            counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
                        "count"
                    ]
                )
                for table in (
                    "source_capture_snapshots",
                    "source_capture_bindings",
                    "reading_sessions",
                    "reading_artifacts",
                    "reading_notes",
                    "reading_conversations",
                    "reading_chat_messages",
                )
            }
        self.assertEqual(counts, {key: 0 for key in counts})

    def test_conversation_turn_clear_delete_and_hash_verification(self) -> None:
        self.source_repository.append_capture(self._capture())
        session = self.repository.upsert_session(self._session())
        conversation = ReadingConversationV1(
            conversation_id="reading-conversation-test",
            reading_session_id=session.reading_session_id,
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            source_sha256=session.source_sha256,
            effective_bundle_id=session.effective_bundle_id,
            title="泵组连续运行",
            created_at_utc=self.now,
            updated_at_utc=self.now,
        )
        self.assertEqual(
            self.repository.append_conversation(conversation),
            conversation,
        )
        source_ref = ReadingSourceRefV1(
            source_node_id=self.source_ir.nodes[1].source_node_id,
            start_offset=0,
            end_offset=2,
        )
        turn_at = self.now + timedelta(minutes=1)
        user_message = ReadingChatMessageV1(
            message_id="reading-message-user",
            conversation_id=conversation.conversation_id,
            reading_session_id=session.reading_session_id,
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            source_sha256=session.source_sha256,
            effective_bundle_id=session.effective_bundle_id,
            sequence_number=1,
            role=ReadingMessageRole.USER,
            content="解释泵组",
            source_ref=source_ref,
            created_at_utc=turn_at,
        )
        citation = ReadingChatCitationV1(
            citation_id="S1",
            source_node_id=self.source_ir.nodes[1].source_node_id,
            page_number=1,
            start_offset=0,
            end_offset=2,
            quote="泵组",
            label="第 1 页",
        )
        assistant_message = ReadingChatMessageV1(
            message_id="reading-message-assistant",
            conversation_id=conversation.conversation_id,
            reading_session_id=session.reading_session_id,
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            source_sha256=session.source_sha256,
            effective_bundle_id=session.effective_bundle_id,
            sequence_number=2,
            role=ReadingMessageRole.ASSISTANT,
            content="原文描述泵组【S1】。",
            citations=(citation,),
            retrieval=ReadingRetrievalV1(
                coverage_mode="FULL_DOCUMENT",
                selected_node_count=1,
                selected_character_count=2,
                document_node_count=len(self.source_ir.nodes),
                covered_page_numbers=(1,),
            ),
            provider_id="fake",
            model_name="fake-model",
            prompt_version="reading-chat/v1",
            created_at_utc=turn_at,
        )
        updated = ReadingConversationV1.model_validate(
            conversation.model_copy(
                update={
                    "updated_at_utc": turn_at,
                    "last_message_at_utc": turn_at,
                    "revision": 2,
                }
            ).model_dump(mode="python")
        )
        stored_conversation, stored_user, stored_assistant = (
            self.repository.append_chat_turn(
                conversation=updated,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        )
        self.assertEqual(stored_conversation, updated)
        self.assertEqual(stored_user.source_ref, source_ref)
        self.assertEqual(stored_assistant.citations, (citation,))
        self.assertEqual(
            self.repository.list_messages(conversation.conversation_id),
            (user_message, assistant_message),
        )
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE reading_chat_messages SET payload_sha256 = ?
                WHERE message_id = ?
                """,
                ("e" * 64, assistant_message.message_id),
            )
        with self.assertRaisesRegex(RuntimeError, "chat message payload hash mismatch"):
            self.repository.list_messages(conversation.conversation_id)
        with db.get_conn() as connection:
            payload = connection.execute(
                """
                SELECT payload_json FROM reading_chat_messages
                WHERE message_id = ?
                """,
                (assistant_message.message_id,),
            ).fetchone()["payload_json"]
            connection.execute(
                """
                UPDATE reading_chat_messages SET payload_sha256 = ?
                WHERE message_id = ?
                """,
                (hashlib.sha256(str(payload).encode()).hexdigest(), assistant_message.message_id),
            )
        cleared = ReadingConversationV1.model_validate(
            updated.model_copy(
                update={
                    "updated_at_utc": turn_at + timedelta(minutes=1),
                    "last_message_at_utc": None,
                    "revision": 3,
                }
            ).model_dump(mode="python")
        )
        self.assertEqual(
            self.repository.clear_conversation(conversation=cleared),
            2,
        )
        self.assertEqual(self.repository.list_messages(conversation.conversation_id), ())
        self.assertTrue(self.repository.delete_conversation(conversation.conversation_id))
        self.assertIsNone(self.repository.get_conversation(conversation.conversation_id))

    def test_source_replacement_marks_old_reading_payloads_stale_and_read_only(self) -> None:
        self.source_repository.append_capture(self._capture())
        session = self.repository.upsert_session(self._session())
        source_ref = ReadingSourceRefV1(
            source_node_id=self.source_ir.nodes[1].source_node_id,
            start_offset=0,
            end_offset=2,
        )
        artifact = ReadingArtifactV1(
            artifact_id="artifact-old-version",
            reading_session_id=session.reading_session_id,
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            source_sha256=session.source_sha256,
            action=ReadingAction.EXPLANATION,
            source_ref=source_ref,
            output_text="旧版本解释",
            provider_id="fake",
            model_name="fake-model",
            prompt_version="reading-action/v1",
            created_at_utc=self.now,
        )
        note = ReadingNoteV1(
            note_id="note-old-version",
            reading_session_id=session.reading_session_id,
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            source_sha256=session.source_sha256,
            source_ref=source_ref,
            content="旧版本笔记",
            revision=1,
            created_at_utc=self.now,
            updated_at_utc=self.now,
        )
        self.repository.append_artifact(
            artifact,
            expected_effective_bundle_id=session.effective_bundle_id,
        )
        self.repository.append_note(
            note,
            expected_effective_bundle_id=session.effective_bundle_id,
        )
        conversation = ReadingConversationV1(
            conversation_id="conversation-old-version",
            reading_session_id=session.reading_session_id,
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            source_sha256=session.source_sha256,
            effective_bundle_id=session.effective_bundle_id,
            title="旧版本对话",
            created_at_utc=self.now,
            updated_at_utc=self.now,
        )
        self.repository.append_conversation(conversation)
        turn_at = self.now + timedelta(minutes=1)
        user_message = ReadingChatMessageV1(
            message_id="message-old-version-user",
            conversation_id=conversation.conversation_id,
            reading_session_id=session.reading_session_id,
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            source_sha256=session.source_sha256,
            effective_bundle_id=session.effective_bundle_id,
            sequence_number=1,
            role=ReadingMessageRole.USER,
            content="旧版本问题",
            created_at_utc=turn_at,
        )
        assistant_message = ReadingChatMessageV1(
            message_id="message-old-version-assistant",
            conversation_id=conversation.conversation_id,
            reading_session_id=session.reading_session_id,
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            source_sha256=session.source_sha256,
            effective_bundle_id=session.effective_bundle_id,
            sequence_number=2,
            role=ReadingMessageRole.ASSISTANT,
            content="原文未提供。",
            retrieval=ReadingRetrievalV1(
                coverage_mode="FULL_DOCUMENT",
                selected_node_count=1,
                selected_character_count=2,
                document_node_count=len(self.source_ir.nodes),
                covered_page_numbers=(1,),
            ),
            provider_id="fake",
            model_name="fake-model",
            prompt_version="reading-chat/v1",
            created_at_utc=turn_at,
        )
        self.repository.append_chat_turn(
            conversation=ReadingConversationV1.model_validate(
                conversation.model_copy(
                    update={
                        "updated_at_utc": turn_at,
                        "last_message_at_utc": turn_at,
                        "revision": 2,
                    }
                ).model_dump(mode="python")
            ),
            user_message=user_message,
            assistant_message=assistant_message,
        )
        replacement_sha256 = "c" * 64
        stale_at = self.now + timedelta(minutes=5)

        self.repository.mark_other_document_versions_stale(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            current_source_sha256=replacement_sha256,
            updated_at_utc=stale_at,
        )

        stale_session = self.repository.get_session(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            source_sha256=SOURCE_SHA256,
        )
        assert stale_session is not None
        self.assertEqual(stale_session.status, ReadingStatus.STALE)
        stale_artifact = self.repository.list_artifacts(session.reading_session_id)[0]
        stale_note = self.repository.list_notes(session.reading_session_id)[0]
        self.assertEqual(stale_artifact.status, "STALE")
        self.assertEqual(stale_note.status, "STALE")
        self.assertEqual(stale_note.revision, 1)
        stale_conversation = self.repository.get_conversation(conversation.conversation_id)
        assert stale_conversation is not None
        self.assertEqual(stale_conversation.status, "STALE")
        with db.get_conn() as connection:
            stale_message_rows = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM reading_chat_messages
                WHERE conversation_id = ? ORDER BY sequence_number
                """,
                (conversation.conversation_id,),
            ).fetchall()
        self.assertEqual(len(stale_message_rows), 2)
        self.assertTrue(
            all(
                json.loads(str(row["payload_json"]))["status"] == "STALE"
                for row in stale_message_rows
            )
        )
        with self.assertRaises(ReadingRepositoryConflict):
            self.repository.update_note(
                ReadingNoteV1.model_validate(
                    stale_note.model_copy(
                        update={
                            "content": "不能编辑",
                            "revision": 2,
                            "updated_at_utc": stale_at + timedelta(minutes=1),
                        }
                    ).model_dump(mode="python")
                ),
                expected_revision=1,
                expected_effective_bundle_id=session.effective_bundle_id,
            )
        with db.get_conn() as connection:
            artifact_row = connection.execute(
                "SELECT payload_json, payload_sha256 FROM reading_artifacts WHERE artifact_id = ?",
                (artifact.artifact_id,),
            ).fetchone()
            note_row = connection.execute(
                "SELECT payload_json, payload_sha256 FROM reading_notes WHERE note_id = ?",
                (note.note_id,),
            ).fetchone()
        assert artifact_row is not None and note_row is not None
        self.assertEqual(
            hashlib.sha256(str(artifact_row["payload_json"]).encode()).hexdigest(),
            str(artifact_row["payload_sha256"]),
        )
        self.assertEqual(
            hashlib.sha256(str(note_row["payload_json"]).encode()).hexdigest(),
            str(note_row["payload_sha256"]),
        )


if __name__ == "__main__":
    unittest.main()
