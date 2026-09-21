import { request } from "../../shared/api/client";
import type {
  ReadingActionRequestV1,
  ReadingAIReadinessV1,
  ReadingArtifactV1,
  ReadingChatMessageCreateRequestV1,
  ReadingChatTurnV1,
  ReadingConversationCreateRequestV1,
  ReadingConversationClearV1,
  ReadingConversationDeleteV1,
  ReadingConversationListV1,
  ReadingConversationV1,
  ReadingContextV1,
  ReadingDocument,
  ReadingDocumentCatalogV1,
  ReadingNoteCreateRequestV1,
  ReadingNoteDeleteV1,
  ReadingNoteListV1,
  ReadingNoteUpdateRequestV1,
  ReadingNoteV1,
  ReadingPrepareRequestV1,
  ReadingProject,
  ReadingProjectCatalogV1,
  ReadingMessageListV1,
  ReadingSourcePageV1,
} from "./contracts";

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function requiredText(value: unknown, label: string): string {
  const text = stringValue(value, label);
  if (!text.trim()) {
    throw new Error(`${label} must be a non-empty string.`);
  }
  return text;
}

function stringValue(value: unknown, label: string): string {
  if (typeof value !== "string") throw new Error(`${label} must be a string.`);
  return value;
}

function optionalText(value: unknown, label: string): string | undefined {
  if (value === null || value === undefined || value === "") return undefined;
  if (typeof value !== "string") throw new Error(`${label} must be a string.`);
  return value;
}

function requireExactKeys(
  value: Record<string, unknown>,
  allowedKeys: readonly string[],
  label: string,
): void {
  const allowed = new Set(allowedKeys);
  const unexpected = Object.keys(value).filter((key) => !allowed.has(key));
  if (unexpected.length) {
    throw new Error(`${label} contains unexpected fields: ${unexpected.join(", ")}.`);
  }
}

function projectCatalogItems(payload: unknown): ReadingProject[] {
  const catalog = asRecord(payload, "Reading project catalog");
  requireExactKeys(
    catalog,
    ["schema_version", "items"],
    "Reading project catalog",
  );
  if (catalog.schema_version !== "reading-project-catalog/v1") {
    throw new Error("Reading project catalog schema is not supported.");
  }
  if (!Array.isArray(catalog.items)) {
    throw new Error("Reading project catalog items must be an array.");
  }
  return catalog.items.map((value, index) => {
    const item = asRecord(value, `Reading project catalog item ${index}`);
    requireExactKeys(
      item,
      ["id", "name", "customer_name"],
      `Reading project catalog item ${index}`,
    );
    return {
      id: requiredText(item.id, `Reading project catalog item ${index} id`),
      name: requiredText(item.name, `Reading project catalog item ${index} name`),
      customer_name: stringValue(
        item.customer_name,
        `Reading project catalog item ${index} customer_name`,
      ),
    };
  });
}

function documentCatalogItems(
  payload: unknown,
  expectedProjectId: string,
): ReadingDocument[] {
  const catalog = asRecord(payload, "Reading document catalog");
  requireExactKeys(
    catalog,
    ["schema_version", "project_id", "items"],
    "Reading document catalog",
  );
  if (catalog.schema_version !== "reading-document-catalog/v1") {
    throw new Error("Reading document catalog schema is not supported.");
  }
  if (catalog.project_id !== expectedProjectId || !Array.isArray(catalog.items)) {
    throw new Error("Reading document catalog does not match the current project.");
  }
  return catalog.items.map((value, index) => {
    const item = asRecord(value, `Reading document catalog item ${index}`);
    requireExactKeys(
      item,
      [
        "id",
        "project_id",
        "file_name",
        "file_type",
        "detected_format",
        "source_sha256",
        "source_size",
        "created_at",
      ],
      `Reading document catalog item ${index}`,
    );
    const projectId = requiredText(
      item.project_id,
      `Reading document catalog item ${index} project_id`,
    );
    if (projectId !== expectedProjectId) {
      throw new Error("Reading document catalog contains another project.");
    }
    const sourceSize = Number(item.source_size);
    if (!Number.isSafeInteger(sourceSize) || sourceSize < 0) {
      throw new Error(
        `Reading document catalog item ${index} source_size must be non-negative.`,
      );
    }
    const document: ReadingDocument = {
      id: requiredText(item.id, `Reading document catalog item ${index} id`),
      project_id: projectId,
      file_name: requiredText(
        item.file_name,
        `Reading document catalog item ${index} file_name`,
      ),
      file_type: optionalText(
        item.file_type,
        `Reading document catalog item ${index} file_type`,
      ),
      detected_format: optionalText(
        item.detected_format,
        `Reading document catalog item ${index} detected_format`,
      ),
      source_sha256: optionalText(
        item.source_sha256,
        `Reading document catalog item ${index} source_sha256`,
      ),
      source_size: sourceSize,
      created_at: requiredText(
        item.created_at,
        `Reading document catalog item ${index} created_at`,
      ),
    };
    return document;
  });
}

function documentRoot(projectId: string, documentId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}`;
}

function readingRoot(projectId: string, documentId: string): string {
  return `${documentRoot(projectId, documentId)}/reading`;
}

export const readingApi = {
  projects: async () =>
    projectCatalogItems(
      await request<ReadingProjectCatalogV1>("/api/reading/projects"),
    ),
  documents: async (projectId: string) =>
    documentCatalogItems(
      await request<ReadingDocumentCatalogV1>(
        `/api/reading/projects/${encodeURIComponent(projectId)}/documents`,
      ),
      projectId,
    ),
  fileUrl: (projectId: string, documentId: string) =>
    `${documentRoot(projectId, documentId)}/file`,
  context: (projectId: string, documentId: string, signal?: AbortSignal) =>
    request<ReadingContextV1>(
      `${readingRoot(projectId, documentId)}/context`,
      { signal },
    ),
  sourcePage: (
    projectId: string,
    documentId: string,
    pageNumber: number,
    signal?: AbortSignal,
  ) =>
    request<ReadingSourcePageV1>(
      `${readingRoot(projectId, documentId)}/source-pages/${encodeURIComponent(String(pageNumber))}`,
      { signal },
    ),
  prepare: (
    projectId: string,
    documentId: string,
    payload: ReadingPrepareRequestV1,
    signal?: AbortSignal,
  ) =>
    request<ReadingContextV1>(
      `${readingRoot(projectId, documentId)}/prepare`,
      { method: "POST", body: JSON.stringify(payload), signal },
    ),
  createArtifact: (
    projectId: string,
    documentId: string,
    payload: ReadingActionRequestV1,
    signal?: AbortSignal,
  ) =>
    request<ReadingArtifactV1>(
      `${readingRoot(projectId, documentId)}/actions`,
      { method: "POST", body: JSON.stringify(payload), signal },
    ),
  aiReadiness: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<ReadingAIReadinessV1>(
      `${readingRoot(projectId, documentId)}/ai-readiness`,
      { signal },
    ),
  conversations: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<ReadingConversationListV1>(
      `${readingRoot(projectId, documentId)}/conversations`,
      { signal },
    ),
  createConversation: (
    projectId: string,
    documentId: string,
    payload: ReadingConversationCreateRequestV1,
    signal?: AbortSignal,
  ) =>
    request<ReadingConversationV1>(
      `${readingRoot(projectId, documentId)}/conversations`,
      { method: "POST", body: JSON.stringify(payload), signal },
    ),
  conversationMessages: (
    projectId: string,
    documentId: string,
    conversationId: string,
    signal?: AbortSignal,
  ) =>
    request<ReadingMessageListV1>(
      `${readingRoot(projectId, documentId)}/conversations/${encodeURIComponent(conversationId)}/messages`,
      { signal },
    ),
  sendMessage: (
    projectId: string,
    documentId: string,
    conversationId: string,
    payload: ReadingChatMessageCreateRequestV1,
    signal?: AbortSignal,
  ) =>
    request<ReadingChatTurnV1>(
      `${readingRoot(projectId, documentId)}/conversations/${encodeURIComponent(conversationId)}/messages`,
      { method: "POST", body: JSON.stringify(payload), signal },
    ),
  clearConversation: (
    projectId: string,
    documentId: string,
    conversationId: string,
    signal?: AbortSignal,
  ) =>
    request<ReadingConversationClearV1>(
      `${readingRoot(projectId, documentId)}/conversations/${encodeURIComponent(conversationId)}/messages`,
      { method: "DELETE", signal },
    ),
  deleteConversation: (
    projectId: string,
    documentId: string,
    conversationId: string,
    signal?: AbortSignal,
  ) =>
    request<ReadingConversationDeleteV1>(
      `${readingRoot(projectId, documentId)}/conversations/${encodeURIComponent(conversationId)}`,
      { method: "DELETE", signal },
    ),
  notes: (projectId: string, documentId: string, signal?: AbortSignal) =>
    request<ReadingNoteListV1>(
      `${readingRoot(projectId, documentId)}/notes`,
      { signal },
    ),
  createNote: (
    projectId: string,
    documentId: string,
    payload: ReadingNoteCreateRequestV1,
    signal?: AbortSignal,
  ) =>
    request<ReadingNoteV1>(`${readingRoot(projectId, documentId)}/notes`, {
      method: "POST",
      body: JSON.stringify(payload),
      signal,
    }),
  updateNote: (
    projectId: string,
    documentId: string,
    noteId: string,
    payload: ReadingNoteUpdateRequestV1,
    signal?: AbortSignal,
  ) =>
    request<ReadingNoteV1>(
      `${readingRoot(projectId, documentId)}/notes/${encodeURIComponent(noteId)}`,
      { method: "PATCH", body: JSON.stringify(payload), signal },
    ),
  deleteNote: (
    projectId: string,
    documentId: string,
    noteId: string,
    expectedRevision: number,
    signal?: AbortSignal,
  ) =>
    request<ReadingNoteDeleteV1>(
      `${readingRoot(projectId, documentId)}/notes/${encodeURIComponent(noteId)}?expected_revision=${encodeURIComponent(String(expectedRevision))}`,
      { method: "DELETE", signal },
    ),
};
