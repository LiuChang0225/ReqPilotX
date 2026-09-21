from reqpilotx_server.application import reading, reading_catalog
from reqpilotx_server.domain.reading_catalog import (
    ReadingDocumentCatalogV1,
    ReadingProjectCatalogV1,
)
from reqpilotx_server.domain.reading import (
    ReadingAIReadinessV1,
    ReadingChatTurnV1,
    ReadingConversationClearResultV1,
    ReadingConversationDeleteResultV1,
    ReadingConversationListV1,
    ReadingConversationV1,
    ReadingMessageListV1,
)

from ._factory import RouteSpec, build_router


DOCUMENT = "/api/projects/{project_id}/sor-documents/{document_id}"
READING = f"{DOCUMENT}/reading"
READING_CATALOG = "/api/reading/projects"

SPECS = (
    RouteSpec(
        READING_CATALOG,
        reading_catalog.list_reading_projects,
        "GET",
        response_model=ReadingProjectCatalogV1,
    ),
    RouteSpec(
        f"{READING_CATALOG}/{{project_id}}/documents",
        reading_catalog.list_reading_documents,
        "GET",
        response_model=ReadingDocumentCatalogV1,
    ),
    RouteSpec(f"{READING}/context", reading.get_reading_context, "GET"),
    RouteSpec(f"{READING}/prepare", reading.prepare_reading_context, "POST"),
    RouteSpec(
        f"{READING}/source-pages/{{page_number}}",
        reading.get_reading_source_page,
        "GET",
    ),
    RouteSpec(f"{READING}/actions", reading.create_reading_action, "POST"),
    RouteSpec(f"{READING}/notes", reading.list_reading_notes, "GET"),
    RouteSpec(f"{READING}/notes", reading.create_reading_note, "POST"),
    RouteSpec(
        f"{READING}/notes/{{note_id}}",
        reading.update_reading_note,
        "PATCH",
    ),
    RouteSpec(
        f"{READING}/notes/{{note_id}}",
        reading.delete_reading_note,
        "DELETE",
    ),
    RouteSpec(
        f"{READING}/ai-readiness",
        reading.get_reading_ai_readiness,
        "GET",
        response_model=ReadingAIReadinessV1,
    ),
    RouteSpec(
        f"{READING}/conversations",
        reading.list_reading_conversations,
        "GET",
        response_model=ReadingConversationListV1,
    ),
    RouteSpec(
        f"{READING}/conversations",
        reading.create_reading_conversation,
        "POST",
        response_model=ReadingConversationV1,
    ),
    RouteSpec(
        f"{READING}/conversations/{{conversation_id}}/messages",
        reading.list_reading_messages,
        "GET",
        response_model=ReadingMessageListV1,
    ),
    RouteSpec(
        f"{READING}/conversations/{{conversation_id}}/messages",
        reading.create_reading_message,
        "POST",
        response_model=ReadingChatTurnV1,
    ),
    RouteSpec(
        f"{READING}/conversations/{{conversation_id}}/messages",
        reading.clear_reading_conversation,
        "DELETE",
        response_model=ReadingConversationClearResultV1,
    ),
    RouteSpec(
        f"{READING}/conversations/{{conversation_id}}",
        reading.delete_reading_conversation,
        "DELETE",
        response_model=ReadingConversationDeleteResultV1,
    ),
)


def build():
    return build_router(tag="reading", specs=SPECS)


__all__ = ["build"]
