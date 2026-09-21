"""Persistence port for neutral source capture and isolated reading data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from reqpilotx_server.domain.reading import (
    DocumentReadingOverviewV1,
    ReadingArtifactV1,
    ReadingChatMessageV1,
    ReadingConversationV1,
    ReadingNoteV1,
    ReadingSelectableNodeV1,
    ReadingStatus,
    ReadingStructureMapV1,
)


class ReadingRepositoryError(RuntimeError):
    pass


class ReadingRepositoryConflict(ReadingRepositoryError):
    pass


class ReadingRepositoryCorrupt(ReadingRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class ReadingSessionRecord:
    reading_session_id: str
    project_id: str
    sor_document_id: str
    source_sha256: str
    capture_id: str
    status: ReadingStatus
    overview: DocumentReadingOverviewV1
    structure_map: ReadingStructureMapV1
    selectable_nodes: tuple[ReadingSelectableNodeV1, ...]
    created_at_utc: datetime
    updated_at_utc: datetime
    effective_bundle_id: str = ""
    visual_generation_id: str | None = None


@runtime_checkable
class ReadingRepositoryPort(Protocol):
    def mark_session_bundle_stale(
        self,
        *,
        reading_session_id: str,
        updated_at_utc: datetime,
    ) -> None: ...

    def mark_other_document_versions_stale(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        current_source_sha256: str,
        updated_at_utc: datetime,
    ) -> None: ...

    def get_session(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        source_sha256: str,
    ) -> ReadingSessionRecord | None: ...

    def upsert_session(self, record: ReadingSessionRecord) -> ReadingSessionRecord: ...

    def append_artifact(
        self,
        artifact: ReadingArtifactV1,
        *,
        expected_effective_bundle_id: str,
    ) -> ReadingArtifactV1: ...

    def list_artifacts(self, reading_session_id: str) -> tuple[ReadingArtifactV1, ...]: ...

    def append_note(
        self,
        note: ReadingNoteV1,
        *,
        expected_effective_bundle_id: str,
    ) -> ReadingNoteV1: ...

    def list_notes(self, reading_session_id: str) -> tuple[ReadingNoteV1, ...]: ...

    def update_note(
        self,
        note: ReadingNoteV1,
        *,
        expected_revision: int,
        expected_effective_bundle_id: str,
    ) -> ReadingNoteV1: ...

    def delete_note(
        self,
        *,
        reading_session_id: str,
        note_id: str,
        expected_revision: int,
        expected_effective_bundle_id: str,
        updated_at_utc: datetime,
    ) -> ReadingNoteV1: ...

    def append_conversation(
        self,
        conversation: ReadingConversationV1,
    ) -> ReadingConversationV1: ...

    def get_conversation(self, conversation_id: str) -> ReadingConversationV1 | None: ...

    def list_conversations(
        self,
        reading_session_id: str,
    ) -> tuple[ReadingConversationV1, ...]: ...

    def list_messages(
        self,
        conversation_id: str,
    ) -> tuple[ReadingChatMessageV1, ...]: ...

    def append_chat_turn(
        self,
        *,
        conversation: ReadingConversationV1,
        user_message: ReadingChatMessageV1,
        assistant_message: ReadingChatMessageV1,
    ) -> tuple[ReadingConversationV1, ReadingChatMessageV1, ReadingChatMessageV1]: ...

    def clear_conversation(
        self,
        *,
        conversation: ReadingConversationV1,
    ) -> int: ...

    def delete_conversation(self, conversation_id: str) -> bool: ...

    def purge_document(
        self,
        *,
        project_id: str,
        sor_document_id: str,
    ) -> None: ...

__all__ = [
    "ReadingRepositoryConflict",
    "ReadingRepositoryCorrupt",
    "ReadingRepositoryError",
    "ReadingRepositoryPort",
    "ReadingSessionRecord",
]
