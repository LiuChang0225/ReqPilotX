"""Application use cases for the isolated single-document reading workspace."""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from reqpilotx_server.adapters.models.reading_provider import (
    build_reading_provider,
    get_reading_provider_readiness,
)
from reqpilotx_server.adapters.persistence.sql_reading import SqlReadingRepository
from reqpilotx_server.adapters.persistence.sql_source_capture import SqlSourceFactsReader
from reqpilotx_server.adapters.persistence.sql_source_visual_capture import (
    SqlSourceVisualCaptureRepository,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.reading_projection import (
    build_reading_projection,
    build_reading_source_page,
    source_slice_for_ref,
)
from reqpilotx_server.application.reading_retrieval import (
    EffectiveSourceIncomplete,
    FullDocumentTooLarge,
    assemble_reading_context,
)
from reqpilotx_server.application.source_capture import (
    capture_current_document_source,
    require_current_source_document,
)
from reqpilotx_server.application.source_visual_capture import build_capture_quality_manifest
from reqpilotx_server.domain.reading import (
    DocumentReadingOverviewV1,
    ReadingAIReadinessV1,
    ReadingActionRequestV1,
    ReadingArtifactV1,
    ReadingChatCitationV1,
    ReadingChatMessageV1,
    ReadingChatTaskType,
    ReadingChatTurnV1,
    ReadingConversationClearResultV1,
    ReadingConversationCreateRequestV1,
    ReadingConversationDeleteResultV1,
    ReadingConversationListV1,
    ReadingConversationStatus,
    ReadingConversationV1,
    ReadingContextV1,
    ReadingNoteCreateRequestV1,
    ReadingNoteListV1,
    ReadingNoteUpdateRequestV1,
    ReadingNoteV1,
    ReadingMessageCreateRequestV1,
    ReadingMessageListV1,
    ReadingMessageRole,
    ReadingPrepareRequestV1,
    ReadingSelectableNodeV1,
    ReadingSourcePageV1,
    ReadingStatus,
    ReadingStructureMapV1,
)
from reqpilotx_server.domain.source_visual_capture import (
    CaptureQualityManifestV1,
    CaptureQualityStatus,
    PageReadableMode,
    VisualPageCaptureV1,
)
from reqpilotx_server.ports.reading_provider import (
    FULL_EFFECTIVE_TEXT_DISCLOSURE,
    ReadingChatHistoryMessage,
    ReadingProviderFailure,
    ReadingProviderPort,
    ReadingProviderUnavailable,
)
from reqpilotx_server.ports.reading_repository import (
    ReadingRepositoryConflict,
    ReadingRepositoryPort,
    ReadingSessionRecord,
)
from reqpilotx_server.ports.source_capture_repository import (
    SourceCaptureRecord,
    SourceCaptureRepositoryConflict,
    SourceFactsReaderPort,
)
from reqpilotx_server.ports.source_visual_capture_repository import SourceVisualFactsReaderPort

logger = logging.getLogger(__name__)


_MAX_READING_ACTION_SOURCE_CHARACTERS = 12_000
_MAX_READING_CHAT_HISTORY_CHARACTERS = 16_000


class ReadingService:
    """Coordinate reading-only state without importing any formal extraction use case."""

    def __init__(
        self,
        *,
        repository: ReadingRepositoryPort,
        provider_factory: Callable[[], ReadingProviderPort],
        source_facts_reader: SourceFactsReaderPort | None = None,
        visual_facts_reader: SourceVisualFactsReaderPort | None = None,
        source_capture: Callable[..., SourceCaptureRecord] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._provider_factory = provider_factory
        self._source_facts_reader = source_facts_reader or SqlSourceFactsReader()
        self._visual_facts_reader = (
            visual_facts_reader or SqlSourceVisualCaptureRepository()
        )
        self._source_capture = source_capture or capture_current_document_source
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        request: ReadingPrepareRequestV1,
    ) -> ReadingContextV1:
        now = self._now()
        try:
            capture = self._source_capture(
                project_id=project_id,
                sor_document_id=sor_document_id,
                expected_source_sha256=request.expected_source_sha256,
                clock=lambda: now,
            )
        except SourceCaptureRepositoryConflict as exc:
            raise ApplicationError(
                status_code=409,
                code="reading_source_capture_conflict",
                detail={
                    "reason_code": "reading_source_capture_conflict",
                    "message": "The current source facts conflict with an existing reading capture.",
                },
            ) from exc
        self._repository.mark_other_document_versions_stale(
            project_id=project_id,
            sor_document_id=sor_document_id,
            current_source_sha256=capture.source_sha256,
            updated_at_utc=now,
        )
        manifest, _ = self._effective_source(capture)
        projection = build_reading_projection(
            capture.source_ir,
            native_structure_page_numbers=frozenset(
                page.page_number
                for page in manifest.pages
                if page.readable_mode is PageReadableMode.NATIVE
            ),
        )
        overview = DocumentReadingOverviewV1.model_validate(projection["overview"])
        structure_map = ReadingStructureMapV1.model_validate(
            projection["structure_map"]
        )
        selectable_nodes = tuple(
            ReadingSelectableNodeV1.model_validate(item)
            for item in projection["selectable_nodes"]
            if _native_projection_item_is_effective(item, manifest)
        )
        existing = self._repository.get_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=capture.source_sha256,
        )
        reading_session_id = _stable_id(
            "reading-session",
            project_id,
            sor_document_id,
            capture.source_sha256,
            capture.capture_id,
        )
        created_at = existing.created_at_utc if existing is not None else now
        if (
            existing is not None
            and existing.effective_bundle_id != manifest.effective_bundle_id
        ):
            self._repository.mark_session_bundle_stale(
                reading_session_id=existing.reading_session_id,
                updated_at_utc=now,
            )
        status = (
            ReadingStatus.READY
            if manifest.status is CaptureQualityStatus.READY
            and structure_map.status is ReadingStatus.READY
            else ReadingStatus.PARTIAL
        )
        try:
            stored = self._repository.upsert_session(
                ReadingSessionRecord(
                    reading_session_id=reading_session_id,
                    project_id=project_id,
                    sor_document_id=sor_document_id,
                    source_sha256=capture.source_sha256,
                    capture_id=capture.capture_id,
                    status=status,
                    overview=overview,
                    structure_map=structure_map,
                    selectable_nodes=selectable_nodes,
                    created_at_utc=created_at,
                    updated_at_utc=now,
                    effective_bundle_id=manifest.effective_bundle_id,
                    visual_generation_id=manifest.active_visual_generation_id,
                )
            )
        except ReadingRepositoryConflict as exc:
            raise _reading_context_bundle_changed() from exc
        return self._compose_context(stored, capture)

    def context(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        expected_source_sha256: str | None = None,
    ) -> ReadingContextV1:
        session, capture = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
            expected_source_sha256=expected_source_sha256,
        )
        return self._compose_context(session, capture)

    def source_page(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        page_number: int,
    ) -> ReadingSourcePageV1:
        session, capture = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        manifest, visual_pages = self._effective_source(capture)
        try:
            if page_number > manifest.page_count:
                raise ValueError("page_number exceeds the captured document")
            payload = build_reading_source_page(
                capture.source_ir,
                page_number=page_number,
            )
        except ValueError as exc:
            raise _invalid_source_ref(str(exc)) from exc
        quality_page = manifest.pages[page_number - 1]
        visual = next(
            (item for item in visual_pages if item.page_number == page_number),
            None,
        )
        if quality_page.readable_mode is PageReadableMode.VISUAL and visual is None:
            raise _effective_source_corrupt()
        return ReadingSourcePageV1.model_validate(
            {
                **payload,
                "project_id": project_id,
                "sor_document_id": sor_document_id,
                "reading_session_id": session.reading_session_id,
                "page_count": manifest.page_count,
                "effective_bundle_id": manifest.effective_bundle_id,
                "visual_generation_id": manifest.active_visual_generation_id,
                "readable_mode": quality_page.readable_mode,
                "nodes": (
                    payload["nodes"]
                    if quality_page.readable_mode is PageReadableMode.NATIVE
                    else ()
                ),
                "visual_runs": visual.runs if visual is not None else (),
            }
        )

    def create_action(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        request: ReadingActionRequestV1,
    ) -> ReadingArtifactV1:
        session, capture = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
            expected_source_sha256=request.source_sha256,
        )
        selected_text = self._source_slice(capture, request.source_ref)
        if len(selected_text) > _MAX_READING_ACTION_SOURCE_CHARACTERS:
            raise ApplicationError(
                status_code=422,
                code="reading_source_selection_too_large",
                detail={
                    "reason_code": "reading_source_selection_too_large",
                    "message": "The selected source range is too large for one reading action.",
                    "max_characters": _MAX_READING_ACTION_SOURCE_CHARACTERS,
                },
            )
        try:
            provider = self._provider_factory()
            result = provider.generate(
                action=request.action.value,
                source_text=selected_text,
                target_language=request.target_language,
            )
        except ReadingProviderUnavailable as exc:
            raise _reading_provider_unavailable(exc) from exc
        except ReadingProviderFailure as exc:
            raise _reading_provider_failed(exc) from exc
        if not result.output_text.strip() or len(result.output_text) > 20_000:
            raise ApplicationError(
                status_code=502,
                code="reading_ai_failed",
                detail={
                    "reason_code": "reading_ai_failed",
                    "message": "Codex Agent returned an invalid reading result.",
                },
            )
        self._assert_current_bundle(session, capture)
        now = self._now()
        artifact = ReadingArtifactV1(
            artifact_id=f"reading-artifact-{uuid.uuid4().hex}",
            reading_session_id=session.reading_session_id,
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=capture.source_sha256,
            action=request.action,
            source_ref=request.source_ref,
            output_text=result.output_text,
            provider_id=result.provider_id,
            model_name=result.model_name,
            prompt_version=result.prompt_version,
            created_at_utc=now,
        )
        try:
            return self._repository.append_artifact(
                artifact,
                expected_effective_bundle_id=session.effective_bundle_id,
            )
        except ReadingRepositoryConflict as exc:
            raise _reading_context_bundle_changed() from exc

    def list_notes(
        self,
        *,
        project_id: str,
        sor_document_id: str,
    ) -> ReadingNoteListV1:
        session, _ = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        return ReadingNoteListV1(
            items=self._repository.list_notes(session.reading_session_id)
        )

    def create_note(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        request: ReadingNoteCreateRequestV1,
    ) -> ReadingNoteV1:
        session, capture = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
            expected_source_sha256=request.source_sha256,
        )
        self._source_slice(capture, request.source_ref)
        self._assert_current_bundle(session, capture)
        now = self._now()
        note = ReadingNoteV1(
            note_id=f"reading-note-{uuid.uuid4().hex}",
            reading_session_id=session.reading_session_id,
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=capture.source_sha256,
            source_ref=request.source_ref,
            content=request.content,
            revision=1,
            created_at_utc=now,
            updated_at_utc=now,
        )
        try:
            return self._repository.append_note(
                note,
                expected_effective_bundle_id=session.effective_bundle_id,
            )
        except ReadingRepositoryConflict as exc:
            raise _reading_context_bundle_changed() from exc

    def update_note(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        note_id: str,
        request: ReadingNoteUpdateRequestV1,
    ) -> ReadingNoteV1:
        session, _ = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        current = self._note_for_session(session.reading_session_id, note_id)
        if current.revision != request.expected_revision:
            raise _stale_note_revision()
        updated = ReadingNoteV1.model_validate(
            current.model_copy(
                update={
                    "content": request.content,
                    "revision": current.revision + 1,
                    "updated_at_utc": self._now(),
                }
            ).model_dump(mode="python")
        )
        try:
            return self._repository.update_note(
                updated,
                expected_revision=request.expected_revision,
                expected_effective_bundle_id=session.effective_bundle_id,
            )
        except ReadingRepositoryConflict as exc:
            if "bundle" in str(exc):
                raise _reading_context_bundle_changed() from exc
            raise _stale_note_revision() from exc

    def delete_note(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        note_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        session, _ = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        self._note_for_session(session.reading_session_id, note_id)
        try:
            deleted = self._repository.delete_note(
                reading_session_id=session.reading_session_id,
                note_id=note_id,
                expected_revision=expected_revision,
                expected_effective_bundle_id=session.effective_bundle_id,
                updated_at_utc=self._now(),
            )
        except ReadingRepositoryConflict as exc:
            if "bundle" in str(exc):
                raise _reading_context_bundle_changed() from exc
            raise _stale_note_revision() from exc
        return {
            "schema_version": "reading-note-delete-result/v1",
            "note_id": deleted.note_id,
            "deleted": True,
            "revision": deleted.revision,
        }

    def ai_readiness(
        self,
        *,
        project_id: str,
        sor_document_id: str,
    ) -> ReadingAIReadinessV1:
        document = require_current_source_document(
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        readiness = get_reading_provider_readiness()
        capture = self._source_facts_reader.get_capture(
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=str(document["source_sha256"]),
        )
        manifest = self._effective_source(capture)[0] if capture is not None else None
        return ReadingAIReadinessV1(
            ready=readiness.ready,
            provider_id=readiness.provider_id,
            model_name=readiness.model_name,
            configuration_source=readiness.configuration_source,
            reason_code=readiness.reason_code,
            capture_ready=(
                manifest.status is CaptureQualityStatus.READY
                if manifest is not None
                else False
            ),
            capture_quality_status=(manifest.status.value if manifest else None),
            effective_bundle_id=(manifest.effective_bundle_id if manifest else None),
            visual_generation_id=(
                manifest.active_visual_generation_id if manifest else None
            ),
            visual_required_page_count=(
                manifest.visual_required_page_count if manifest else None
            ),
            visual_ready_page_count=(
                manifest.visual_ready_page_count if manifest else None
            ),
        )

    def create_conversation(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        request: ReadingConversationCreateRequestV1,
    ) -> ReadingConversationV1:
        session, _ = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        now = self._now()
        conversation = ReadingConversationV1(
            conversation_id=f"reading-conversation-{uuid.uuid4().hex}",
            reading_session_id=session.reading_session_id,
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=session.source_sha256,
            effective_bundle_id=session.effective_bundle_id,
            title=(request.title or "新对话").strip(),
            created_at_utc=now,
            updated_at_utc=now,
        )
        return self._repository.append_conversation(conversation)

    def list_conversations(
        self,
        *,
        project_id: str,
        sor_document_id: str,
    ) -> ReadingConversationListV1:
        session, _ = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        return ReadingConversationListV1(
            items=self._repository.list_conversations(session.reading_session_id)
        )

    def list_messages(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        conversation_id: str,
    ) -> ReadingMessageListV1:
        session, _ = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        conversation = self._conversation_for_session(
            session,
            conversation_id,
            allow_stale_read=True,
        )
        return ReadingMessageListV1(
            conversation_id=conversation.conversation_id,
            items=self._repository.list_messages(conversation.conversation_id),
        )

    def send_message(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        conversation_id: str,
        request: ReadingMessageCreateRequestV1,
    ) -> ReadingChatTurnV1:
        session, capture = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
            expected_source_sha256=request.source_sha256,
        )
        conversation = self._conversation_for_session(session, conversation_id)
        manifest, visual_pages = self._effective_source(capture)
        messages = self._repository.list_messages(conversation.conversation_id)
        recent_user_text = "\n".join(
            item.content
            for item in messages
            if item.role is ReadingMessageRole.USER
        )[-6_000:]
        try:
            retrieved = assemble_reading_context(
                capture.source_ir,
                question=request.content,
                recent_user_text=recent_user_text,
                source_ref=request.source_ref,
                source_refs=request.source_refs,
                coverage_intent=request.coverage_intent,
                capture_quality=manifest,
                visual_pages=visual_pages,
            )
        except EffectiveSourceIncomplete as exc:
            raise _reading_capture_incomplete(manifest) from exc
        except FullDocumentTooLarge as exc:
            raise ApplicationError(
                status_code=422,
                code="reading_full_document_too_large",
                detail={
                    "reason_code": "FULL_DOCUMENT_TOO_LARGE",
                    "message": (
                        "The complete current document exceeds the configured direct-reading "
                        "budget; no sampled answer was generated."
                    ),
                },
            ) from exc
        except ValueError as exc:
            raise _invalid_source_ref(str(exc)) from exc
        history = _bounded_chat_history(messages)
        try:
            provider = self._provider_factory()
            result = provider.generate_chat(
                question=_reading_task_instruction(
                    request.task_type,
                    request.content,
                    selected_context=(
                        retrieved.retrieval.coverage_mode == "SELECTED_CONTEXT"
                    ),
                    effective_visual_text=(
                        retrieved.retrieval.coverage_mode == "FULL_EFFECTIVE_TEXT"
                    ),
                ),
                history=history,
                context_chunks=retrieved.chunks,
                coverage_mode=retrieved.retrieval.coverage_mode,
            )
        except ReadingProviderUnavailable as exc:
            raise _reading_provider_unavailable(exc) from exc
        except ReadingProviderFailure as exc:
            raise _reading_provider_failed(exc) from exc

        if (
            retrieved.retrieval.coverage_mode == "FULL_EFFECTIVE_TEXT"
            and FULL_EFFECTIVE_TEXT_DISCLOSURE not in result.output_text
        ):
            raise _reading_provider_failed()

        chunks_by_id = {item.citation_id: item for item in retrieved.chunks}
        citations = tuple(
            ReadingChatCitationV1(
                citation_id=citation_id,
                source_node_id=chunks_by_id[citation_id].source_node_id,
                page_number=chunks_by_id[citation_id].page_number,
                start_offset=chunks_by_id[citation_id].start_offset,
                end_offset=chunks_by_id[citation_id].end_offset,
                quote=chunks_by_id[citation_id].text,
                locator_kind=chunks_by_id[citation_id].locator_kind,
                cell_id=chunks_by_id[citation_id].cell_id,
                visual_snapshot_id=chunks_by_id[citation_id].visual_snapshot_id,
                observation_id=chunks_by_id[citation_id].observation_id,
                bbox=chunks_by_id[citation_id].bbox,
                page_bbox=chunks_by_id[citation_id].page_bbox,
                page_image_sha256=chunks_by_id[citation_id].page_image_sha256,
                provider_id=chunks_by_id[citation_id].visual_provider_id,
                provider_version=chunks_by_id[citation_id].visual_provider_version,
                label=(
                    f"第 {chunks_by_id[citation_id].page_number} 页"
                    if chunks_by_id[citation_id].page_number is not None
                    else "原文位置"
                ),
            )
            for citation_id in result.citation_ids
        )
        self._assert_current_bundle(session, capture)
        now = self._now()
        next_sequence = messages[-1].sequence_number + 1 if messages else 1
        user_message = ReadingChatMessageV1(
            message_id=f"reading-message-{uuid.uuid4().hex}",
            conversation_id=conversation.conversation_id,
            reading_session_id=session.reading_session_id,
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=session.source_sha256,
            effective_bundle_id=session.effective_bundle_id,
            sequence_number=next_sequence,
            role=ReadingMessageRole.USER,
            content=request.content,
            source_ref=request.source_ref,
            source_refs=(
                request.source_refs
                or ((request.source_ref,) if request.source_ref is not None else ())
            ),
            task_type=request.task_type,
            created_at_utc=now,
        )
        assistant_message = ReadingChatMessageV1(
            message_id=f"reading-message-{uuid.uuid4().hex}",
            conversation_id=conversation.conversation_id,
            reading_session_id=session.reading_session_id,
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=session.source_sha256,
            effective_bundle_id=session.effective_bundle_id,
            sequence_number=next_sequence + 1,
            role=ReadingMessageRole.ASSISTANT,
            content=result.output_text,
            citations=citations,
            retrieval=retrieved.retrieval,
            provider_id=result.provider_id,
            model_name=result.model_name,
            prompt_version=result.prompt_version,
            created_at_utc=now,
        )
        title = conversation.title
        if not messages and title == "新对话":
            title = _conversation_title(request.content)
        updated_conversation = ReadingConversationV1.model_validate(
            conversation.model_copy(
                update={
                    "title": title,
                    "revision": conversation.revision + 1,
                    "updated_at_utc": now,
                    "last_message_at_utc": now,
                }
            ).model_dump(mode="python")
        )
        try:
            stored_conversation, stored_user, stored_assistant = (
                self._repository.append_chat_turn(
                    conversation=updated_conversation,
                    user_message=user_message,
                    assistant_message=assistant_message,
                )
            )
        except ReadingRepositoryConflict as exc:
            raise _conversation_changed() from exc
        return ReadingChatTurnV1(
            conversation=stored_conversation,
            user_message=stored_user,
            assistant_message=stored_assistant,
            retrieval=retrieved.retrieval,
        )

    def clear_conversation(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        conversation_id: str,
    ) -> ReadingConversationClearResultV1:
        session, _ = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        conversation = self._conversation_for_session(session, conversation_id)
        cleared = ReadingConversationV1.model_validate(
            conversation.model_copy(
                update={
                    "revision": conversation.revision + 1,
                    "updated_at_utc": self._now(),
                    "last_message_at_utc": None,
                }
            ).model_dump(mode="python")
        )
        try:
            count = self._repository.clear_conversation(conversation=cleared)
        except ReadingRepositoryConflict as exc:
            raise _conversation_changed() from exc
        return ReadingConversationClearResultV1(
            conversation_id=conversation.conversation_id,
            deleted_message_count=count,
        )

    def delete_conversation(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        conversation_id: str,
    ) -> ReadingConversationDeleteResultV1:
        session, _ = self._require_current_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        conversation = self._conversation_for_session(
            session,
            conversation_id,
            allow_stale_read=True,
        )
        if not self._repository.delete_conversation(conversation.conversation_id):
            raise _conversation_not_found()
        return ReadingConversationDeleteResultV1(
            conversation_id=conversation.conversation_id
        )

    def _require_current_session(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        expected_source_sha256: str | None = None,
    ) -> tuple[ReadingSessionRecord, SourceCaptureRecord]:
        document = require_current_source_document(
            project_id=project_id,
            sor_document_id=sor_document_id,
            expected_source_sha256=expected_source_sha256,
        )
        source_sha256 = str(document["source_sha256"])
        session = self._repository.get_session(
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=source_sha256,
        )
        capture = self._source_facts_reader.get_capture(
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=source_sha256,
        )
        if session is None or capture is None:
            raise ApplicationError(
                status_code=404,
                code="reading_context_not_prepared",
                detail={
                    "reason_code": "reading_context_not_prepared",
                    "message": "This document does not have a prepared reading context.",
                },
            )
        if session.capture_id != capture.capture_id:
            raise ApplicationError(
                status_code=409,
                code="reading_context_identity_mismatch",
                detail={
                    "reason_code": "reading_context_identity_mismatch",
                    "message": "The reading context no longer matches its source facts.",
                },
            )
        if session.status not in {ReadingStatus.READY, ReadingStatus.PARTIAL}:
            raise ApplicationError(
                status_code=409,
                code="reading_context_not_current",
                detail={
                    "reason_code": "reading_context_not_current",
                    "message": "The reading context is stale or failed and must be prepared again.",
                },
            )
        self._assert_current_bundle(session, capture)
        return session, capture

    def _source_slice(self, capture: SourceCaptureRecord, source_ref: Any) -> str:
        manifest, visual_pages = self._effective_source(capture)
        try:
            if source_ref.kind == "VISUAL_TEXT":
                found = next(
                    (
                        (page, run)
                        for page in visual_pages
                        for run in page.runs
                        if page.visual_snapshot_id == source_ref.visual_snapshot_id
                        and run.observation_id == source_ref.observation_id
                    ),
                    None,
                )
                if found is None:
                    raise ValueError("visual source reference is not in the active capture")
                page, run = found
                quality_page = manifest.pages[page.page_number - 1]
                if quality_page.readable_mode is not PageReadableMode.VISUAL:
                    raise ValueError("visual source reference is not effective on this page")
                if (
                    source_ref.page_number != page.page_number
                    or source_ref.bbox != run.bbox
                    or source_ref.page_bbox != page.page_bbox
                    or source_ref.page_image_sha256 != page.page_image_sha256
                    or source_ref.provider_id != page.provider_id
                    or source_ref.provider_version != page.provider_version
                ):
                    raise ValueError(
                        "visual source provenance does not match the active capture"
                    )
                if source_ref.end_offset > len(run.text):
                    raise ValueError("visual source offsets exceed current captured text")
                selected = run.text[source_ref.start_offset : source_ref.end_offset]
                if not selected.strip():
                    raise ValueError("visual source reference resolves only to whitespace")
                return selected
            node = next(
                (
                    item
                    for item in capture.source_ir.nodes
                    if item.source_node_id == source_ref.source_node_id
                ),
                None,
            )
            if node is not None and node.native_locator.page_number is not None:
                quality_page = manifest.pages[node.native_locator.page_number - 1]
                if quality_page.readable_mode is not PageReadableMode.NATIVE:
                    raise ValueError("native source reference is not effective on this page")
            return source_slice_for_ref(
                capture.source_ir,
                source_node_id=source_ref.source_node_id,
                start_offset=source_ref.start_offset,
                end_offset=source_ref.end_offset,
            )
        except ValueError as exc:
            raise _invalid_source_ref(str(exc)) from exc

    def _compose_context(
        self,
        session: ReadingSessionRecord,
        capture: SourceCaptureRecord,
    ) -> ReadingContextV1:
        return ReadingContextV1(
            reading_session_id=session.reading_session_id,
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            source_sha256=session.source_sha256,
            source_ir_id=capture.source_ir.source_ir_id,
            source_ir_document_id=capture.source_ir.document_id,
            effective_bundle_id=session.effective_bundle_id,
            visual_generation_id=session.visual_generation_id,
            status=session.status,
            overview=session.overview,
            structure_map=session.structure_map,
            selectable_nodes=session.selectable_nodes,
            artifacts=self._repository.list_artifacts(session.reading_session_id),
            notes=self._repository.list_notes(session.reading_session_id),
            created_at_utc=session.created_at_utc,
            updated_at_utc=session.updated_at_utc,
        )

    def _note_for_session(
        self,
        reading_session_id: str,
        note_id: str,
    ) -> ReadingNoteV1:
        note = next(
            (
                item
                for item in self._repository.list_notes(reading_session_id)
                if item.note_id == note_id
            ),
            None,
        )
        if note is None:
            raise ApplicationError(
                status_code=404,
                code="reading_note_not_found",
                detail="Reading note not found for the current document.",
            )
        return note

    def _conversation_for_session(
        self,
        session: ReadingSessionRecord,
        conversation_id: str,
        *,
        allow_stale_read: bool = False,
    ) -> ReadingConversationV1:
        conversation = self._repository.get_conversation(conversation_id)
        if conversation is None:
            raise _conversation_not_found()
        if (
            conversation.reading_session_id != session.reading_session_id
            or conversation.project_id != session.project_id
            or conversation.sor_document_id != session.sor_document_id
            or conversation.source_sha256 != session.source_sha256
        ):
            raise _conversation_not_found()
        if (
            allow_stale_read
            and conversation.status is ReadingConversationStatus.STALE
            and conversation.effective_bundle_id != session.effective_bundle_id
        ):
            return conversation
        if conversation.effective_bundle_id != session.effective_bundle_id:
            raise ApplicationError(
                status_code=409,
                code="reading_conversation_not_current",
                detail={
                    "reason_code": "reading_conversation_not_current",
                    "message": "This conversation is based on an older visual capture.",
                },
            )
        if conversation.status is not ReadingConversationStatus.ACTIVE:
            raise ApplicationError(
                status_code=409,
                code="reading_conversation_not_current",
                detail={
                    "reason_code": "reading_conversation_not_current",
                    "message": "This conversation belongs to an older document version.",
                },
            )
        return conversation

    def _effective_source(
        self,
        capture: SourceCaptureRecord,
    ) -> tuple[CaptureQualityManifestV1, tuple[VisualPageCaptureV1, ...]]:
        manifest = build_capture_quality_manifest(capture, self._visual_facts_reader)
        pages = (
            self._visual_facts_reader.list_pages(
                manifest.active_visual_generation_id
            )
            if manifest.active_visual_generation_id is not None
            else ()
        )
        return manifest, pages

    def _assert_current_bundle(
        self,
        session: ReadingSessionRecord,
        capture: SourceCaptureRecord,
    ) -> None:
        require_current_source_document(
            project_id=session.project_id,
            sor_document_id=session.sor_document_id,
            expected_source_sha256=capture.source_sha256,
        )
        manifest, _ = self._effective_source(capture)
        if session.effective_bundle_id != manifest.effective_bundle_id:
            raise _reading_context_bundle_changed()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reading clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)


def prepare_reading_context(
    project_id: str,
    document_id: str,
    payload: ReadingPrepareRequestV1,
) -> dict[str, Any]:
    return _default_service().prepare(
        project_id=project_id,
        sor_document_id=document_id,
        request=payload,
    ).model_dump(mode="json")


def get_reading_context(project_id: str, document_id: str) -> dict[str, Any]:
    return _default_service().context(
        project_id=project_id,
        sor_document_id=document_id,
    ).model_dump(mode="json")


def get_reading_source_page(
    project_id: str,
    document_id: str,
    page_number: int,
) -> dict[str, Any]:
    return _default_service().source_page(
        project_id=project_id,
        sor_document_id=document_id,
        page_number=page_number,
    ).model_dump(mode="json")


def create_reading_action(
    project_id: str,
    document_id: str,
    payload: ReadingActionRequestV1,
) -> dict[str, Any]:
    return _default_service().create_action(
        project_id=project_id,
        sor_document_id=document_id,
        request=payload,
    ).model_dump(mode="json")


def list_reading_notes(project_id: str, document_id: str) -> dict[str, Any]:
    return _default_service().list_notes(
        project_id=project_id,
        sor_document_id=document_id,
    ).model_dump(mode="json")


def create_reading_note(
    project_id: str,
    document_id: str,
    payload: ReadingNoteCreateRequestV1,
) -> dict[str, Any]:
    return _default_service().create_note(
        project_id=project_id,
        sor_document_id=document_id,
        request=payload,
    ).model_dump(mode="json")


def update_reading_note(
    project_id: str,
    document_id: str,
    note_id: str,
    payload: ReadingNoteUpdateRequestV1,
) -> dict[str, Any]:
    return _default_service().update_note(
        project_id=project_id,
        sor_document_id=document_id,
        note_id=note_id,
        request=payload,
    ).model_dump(mode="json")


def delete_reading_note(
    project_id: str,
    document_id: str,
    note_id: str,
    expected_revision: int,
) -> dict[str, Any]:
    return _default_service().delete_note(
        project_id=project_id,
        sor_document_id=document_id,
        note_id=note_id,
        expected_revision=expected_revision,
    )


def get_reading_ai_readiness(project_id: str, document_id: str) -> dict[str, Any]:
    return _default_service().ai_readiness(
        project_id=project_id,
        sor_document_id=document_id,
    ).model_dump(mode="json")


def list_reading_conversations(project_id: str, document_id: str) -> dict[str, Any]:
    return _default_service().list_conversations(
        project_id=project_id,
        sor_document_id=document_id,
    ).model_dump(mode="json")


def create_reading_conversation(
    project_id: str,
    document_id: str,
    payload: ReadingConversationCreateRequestV1,
) -> dict[str, Any]:
    return _default_service().create_conversation(
        project_id=project_id,
        sor_document_id=document_id,
        request=payload,
    ).model_dump(mode="json")


def list_reading_messages(
    project_id: str,
    document_id: str,
    conversation_id: str,
) -> dict[str, Any]:
    return _default_service().list_messages(
        project_id=project_id,
        sor_document_id=document_id,
        conversation_id=conversation_id,
    ).model_dump(mode="json")


def create_reading_message(
    project_id: str,
    document_id: str,
    conversation_id: str,
    payload: ReadingMessageCreateRequestV1,
) -> dict[str, Any]:
    return _default_service().send_message(
        project_id=project_id,
        sor_document_id=document_id,
        conversation_id=conversation_id,
        request=payload,
    ).model_dump(mode="json")


def clear_reading_conversation(
    project_id: str,
    document_id: str,
    conversation_id: str,
) -> dict[str, Any]:
    return _default_service().clear_conversation(
        project_id=project_id,
        sor_document_id=document_id,
        conversation_id=conversation_id,
    ).model_dump(mode="json")


def delete_reading_conversation(
    project_id: str,
    document_id: str,
    conversation_id: str,
) -> dict[str, Any]:
    return _default_service().delete_conversation(
        project_id=project_id,
        sor_document_id=document_id,
        conversation_id=conversation_id,
    ).model_dump(mode="json")


def _default_service() -> ReadingService:
    return ReadingService(
        repository=SqlReadingRepository(),
        provider_factory=build_reading_provider,
        source_facts_reader=SqlSourceFactsReader(),
    )


def _invalid_source_ref(message: str) -> ApplicationError:
    return ApplicationError(
        status_code=422,
        code="reading_source_ref_invalid",
        detail={
            "reason_code": "reading_source_ref_invalid",
            "message": "The source reference is not valid for the current document.",
            "source_detail": message,
        },
    )


def _stale_note_revision() -> ApplicationError:
    return ApplicationError(
        status_code=409,
        code="reading_note_revision_stale",
        detail={
            "reason_code": "reading_note_revision_stale",
            "message": "The reading note changed; reload it before editing again.",
        },
    )


def _reading_provider_unavailable(exc: ReadingProviderUnavailable) -> ApplicationError:
    reason_code = getattr(exc, "reason_code", "reading_ai_not_configured")
    messages = {
        "reading_codex_auth_required": (
            "Codex Agent requires ChatGPT login before document reading can start."
        ),
        "reading_codex_model_unavailable": (
            "The configured Codex model is not available for document reading."
        ),
        "reading_codex_usage_limited": (
            "Codex Agent is temporarily usage-limited; retry later."
        ),
        "reading_codex_unavailable": (
            "Codex Agent is temporarily unavailable for document reading."
        ),
        "reading_codex_not_configured": (
            "Codex Agent is not installed or configured for document reading."
        ),
    }
    return ApplicationError(
        status_code=409,
        code=reason_code,
        detail={
            "reason_code": reason_code,
            "message": messages.get(
                reason_code,
                "Codex Agent is not ready for document reading.",
            ),
        },
    )


def _reading_provider_failed(
    exc: ReadingProviderFailure | None = None,
) -> ApplicationError:
    if exc is not None:
        cause = exc.__cause__
        logger.warning(
            "Reading provider failed. reason_code=%s cause_type=%s cause_reason=%s",
            getattr(exc, "reason_code", "reading_provider_failed"),
            type(cause).__name__ if cause is not None else "none",
            getattr(cause, "reason_code", "none"),
        )
    return ApplicationError(
        status_code=502,
        code="reading_ai_failed",
        detail={
            "reason_code": "reading_ai_failed",
            "message": "Codex Agent could not complete this reading answer.",
        },
    )


def _conversation_not_found() -> ApplicationError:
    return ApplicationError(
        status_code=404,
        code="reading_conversation_not_found",
        detail={
            "reason_code": "reading_conversation_not_found",
            "message": "Reading conversation not found for the current document.",
        },
    )


def _conversation_changed() -> ApplicationError:
    return ApplicationError(
        status_code=409,
        code="reading_conversation_changed",
        detail={
            "reason_code": "reading_conversation_changed",
            "message": "The conversation changed while this answer was generated; reload and retry.",
        },
    )


def _reading_context_bundle_changed() -> ApplicationError:
    return ApplicationError(
        status_code=409,
        code="reading_context_bundle_changed",
        detail={
            "reason_code": "reading_context_bundle_changed",
            "message": (
                "The document's visual capture changed. Prepare the reading context "
                "again before using old conversations or notes."
            ),
        },
    )


def _reading_capture_incomplete(
    manifest: CaptureQualityManifestV1,
) -> ApplicationError:
    unreadable_pages = tuple(
        item.page_number
        for item in manifest.pages
        if item.readable_mode is PageReadableMode.NONE
    )
    return ApplicationError(
        status_code=409,
        code="reading_capture_incomplete",
        detail={
            "reason_code": "reading_capture_incomplete",
            "message": (
                "Finish visual capture before asking a whole-document question, or "
                "select a verified readable passage."
            ),
            "capture_quality_status": manifest.status.value,
            "unreadable_page_numbers": unreadable_pages,
            "resume_action": "CAPTURE_REQUIRED_VISUAL_PAGES",
        },
    )


def _effective_source_corrupt() -> ApplicationError:
    return ApplicationError(
        status_code=500,
        code="reading_effective_source_corrupt",
        detail={
            "reason_code": "reading_effective_source_corrupt",
            "message": "The current effective source manifest and page layer disagree.",
        },
    )


def _bounded_chat_history(
    messages: tuple[ReadingChatMessageV1, ...],
) -> tuple[ReadingChatHistoryMessage, ...]:
    selected: list[ReadingChatHistoryMessage] = []
    characters = 0
    for message in reversed(messages):
        remaining = _MAX_READING_CHAT_HISTORY_CHARACTERS - characters
        if remaining <= 0:
            break
        content = _neutralize_historical_citation_ids(message.content)
        if len(content) > remaining:
            content = content[-remaining:]
        if not content.strip():
            continue
        selected.append(
            ReadingChatHistoryMessage(
                role=(
                    "user"
                    if message.role is ReadingMessageRole.USER
                    else "assistant"
                ),
                content=content,
            )
        )
        characters += len(content)
        if len(selected) >= 16:
            break
    selected.reverse()
    return tuple(selected)


def _conversation_title(question: str) -> str:
    compact = " ".join(str(question or "").split())
    return compact[:60] or "新对话"


def _neutralize_historical_citation_ids(content: str) -> str:
    """Prevent prior turn-local source IDs from rebinding to the next full context."""

    return re.sub(r"【(?:R[1-9][0-9]{0,2}|S[1-9][0-9]{0,5})】", "【上一轮原文引用】", content)


def _reading_task_instruction(
    task_type: ReadingChatTaskType,
    content: str,
    *,
    selected_context: bool = False,
    effective_visual_text: bool = False,
) -> str:
    templates = {
        ReadingChatTaskType.QUICK_READ: (
            "快速读懂当前完整文档：先用一句话说明目的和适用范围，再按正文结构概括主要章节，"
            "提炼关键技术条件、试验与验收要求、例外和容易遗漏的限制。重要结论须引用原文。"
        ),
        ReadingChatTaskType.TECHNICAL_CONDITIONS: (
            "从当前完整文档提炼工程师需要关注的技术条件。按对象、参数/单位、工作模式、"
            "环境与边界条件、测试方法、判定标准组织；区分原文明示与推断，并逐项引用。"
        ),
        ReadingChatTaskType.SECTION_GUIDE: (
            "按当前完整文档的实际章节顺序制作导读。说明每章解决什么问题、章节之间如何关联，"
            "以及首次阅读时应重点核对的表格和条款；不要用修订历史代替正文导读。"
        ),
        ReadingChatTaskType.MISSED_CONDITIONS: (
            "通读当前完整文档，找出容易遗漏但可能改变理解或执行结果的前提、例外、交叉引用、"
            "时间要求、工作模式、报告义务和判定条件。按重要性分组，每项给出原文引用。"
        ),
    }
    instruction = templates.get(task_type)
    if instruction is None:
        instruction = str(content or "").strip()
        if selected_context:
            return (
                "仅依据用户所选内容回答；文档仍有未识别页，不得声称已通读或概括完整文档。\n"
                + instruction
            )
        if effective_visual_text:
            return (
                f"回答中必须原样披露“{FULL_EFFECTIVE_TEXT_DISCLOSURE}”，"
                "不得声称已理解无文字图像语义。\n"
                + instruction
            )
        return instruction
    if selected_context:
        instruction = (
            "仅依据用户所选内容回答；文档仍有未识别页，不得声称已通读或概括完整文档。"
            + instruction.replace("当前完整文档", "所选内容")
        )
    elif effective_visual_text:
        instruction = (
            f"回答中必须原样披露“{FULL_EFFECTIVE_TEXT_DISCLOSURE}”，"
            "不得声称已理解无文字图像语义。"
            + instruction.replace("当前完整文档", "当前文档的全部可读文字层")
        )
    extra = str(content or "").strip()
    return f"{instruction}\n用户补充：{extra}" if extra else instruction


def _native_projection_item_is_effective(
    item: dict[str, Any],
    manifest: CaptureQualityManifestV1,
) -> bool:
    page_number = item.get("page_number")
    if page_number is None:
        return True
    if not isinstance(page_number, int) or not (1 <= page_number <= manifest.page_count):
        return False
    return manifest.pages[page_number - 1].readable_mode is PageReadableMode.NATIVE


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\0".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:32]}"


__all__ = [
    "ReadingService",
    "clear_reading_conversation",
    "create_reading_action",
    "create_reading_conversation",
    "create_reading_message",
    "create_reading_note",
    "delete_reading_note",
    "delete_reading_conversation",
    "get_reading_ai_readiness",
    "get_reading_context",
    "get_reading_source_page",
    "list_reading_notes",
    "list_reading_conversations",
    "list_reading_messages",
    "prepare_reading_context",
    "update_reading_note",
]
