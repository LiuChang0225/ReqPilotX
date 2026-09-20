import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../shared/api/client";
import { readingApi } from "./readingApi";

function installFetchSpy() {
  const spy = vi.fn(
    async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      let payload: unknown = { items: [] };
      if (url === "/api/reading/projects") {
        payload = {
          schema_version: "reading-project-catalog/v1",
          items: [],
        };
      } else if (/^\/api\/reading\/projects\/[^/]+\/documents$/.test(url)) {
        payload = {
          schema_version: "reading-document-catalog/v1",
          project_id: decodeURIComponent(url.split("/")[4] || ""),
          items: [],
        };
      }
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

afterEach(() => vi.unstubAllGlobals());

describe("reading API isolation", () => {
  it("rejects forbidden catalog fields before they can reach reading state", async () => {
    const otherBody = "OTHER_DOCUMENT_BODY_MUST_NOT_REACH_READING_STATE";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const payload =
          url === "/api/reading/projects"
            ? {
                schema_version: "reading-project-catalog/v1",
                items: [
                  {
                    id: "p",
                    name: "Project",
                    customer_name: "Customer",
                    processing_profile_json: otherBody,
                    knowledge_base_version: 19,
                  },
                ],
              }
            : {
                schema_version: "reading-document-catalog/v1",
                project_id: "p",
                items: [
                  {
                    id: "d-1",
                    project_id: "p",
                    file_name: "current.pdf",
                    file_type: "PDF",
                    detected_format: "pdf",
                    source_sha256: "a".repeat(64),
                    source_size: 123,
                    created_at: "2026-08-13T00:00:00+00:00",
                    extracted_text: "CURRENT_DOCUMENT_BODY_MUST_NOT_REACH_STATE",
                    workflow_stage: "EXTRACTION_LOCKED",
                    active_requirement_set_id: "formal-set",
                  },
                  {
                    id: "d-2",
                    project_id: "p",
                    file_name: "other.pdf",
                    file_type: "PDF",
                    detected_format: "pdf",
                    source_sha256: "b".repeat(64),
                    source_size: 456,
                    created_at: "2026-08-13T00:01:00+00:00",
                    extracted_text: otherBody,
                    ai_status: "READY",
                  },
                ],
              };
        return new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );

    await expect(readingApi.projects()).rejects.toThrow(
      /unexpected fields.*processing_profile_json/i,
    );
    await expect(readingApi.documents("p")).rejects.toThrow(
      /unexpected fields.*extracted_text/i,
    );
  });

  it("keeps every reading request outside formal and generic catalog endpoints", async () => {
    const fetchSpy = installFetchSpy();
    const sourceRef = {
      source_node_id: "node-1",
      start_offset: 0,
      end_offset: 2,
    };

    await readingApi.projects();
    await readingApi.documents("project a");
    await readingApi.context("project a", "document/1");
    await readingApi.sourcePage("project a", "document/1", 2);
    await readingApi.prepare("project a", "document/1", {
      schema_version: "reading-prepare-request/v1",
    });
    await readingApi.createArtifact("project a", "document/1", {
      schema_version: "reading-action-request/v1",
      action: "EXPLANATION",
      source_sha256: "a".repeat(64),
      source_ref: sourceRef,
    });
    await readingApi.aiReadiness("project a", "document/1");
    await readingApi.conversations("project a", "document/1");
    await readingApi.createConversation("project a", "document/1", {
      schema_version: "reading-conversation-create-request/v1",
      title: "Current document chat",
    });
    await readingApi.conversationMessages(
      "project a",
      "document/1",
      "conversation/1",
    );
    await readingApi.sendMessage(
      "project a",
      "document/1",
      "conversation/1",
      {
        schema_version: "reading-message-create-request/v1",
        source_sha256: "a".repeat(64),
        content: "Summarize this document",
        source_ref: sourceRef,
        coverage_intent: "DOCUMENT_OVERVIEW",
      },
    );
    await readingApi.clearConversation(
      "project a",
      "document/1",
      "conversation/1",
    );
    await readingApi.deleteConversation(
      "project a",
      "document/1",
      "conversation/1",
    );
    await readingApi.notes("project a", "document/1");
    await readingApi.createNote("project a", "document/1", {
      schema_version: "reading-note-create-request/v1",
      source_sha256: "a".repeat(64),
      source_ref: sourceRef,
      content: "note",
    });
    await readingApi.updateNote("project a", "document/1", "note/1", {
      schema_version: "reading-note-update-request/v1",
      expected_revision: 1,
      content: "updated note",
    });
    await readingApi.deleteNote("project a", "document/1", "note/1", 2);

    const urls = fetchSpy.mock.calls.map(([input]) => String(input));
    expect(urls.slice(0, 2)).toEqual([
      "/api/reading/projects",
      "/api/reading/projects/project%20a/documents",
    ]);
    expect(urls.every((url) =>
      /^\/api\/reading\/projects(?:\/[^/]+\/documents)?$/.test(url) ||
      /^\/api\/projects\/[^/]+\/sor-documents\/[^/]+\/reading\//.test(url),
    )).toBe(true);
    expect(urls.join(" ")).not.toMatch(
      /requirement-sets|\/extraction|\/workspace|\/matrix|\/assessment|\/knowledge|\/export|\/run-ai-analysis/,
    );
    expect(urls).not.toContain("/api/projects");
    expect(
      urls.some((url) =>
        /^\/api\/projects\/[^/]+\/sor-documents(?:[?#]|$)/.test(url),
      ),
    ).toBe(false);
  });

  it("uses only the current document reading namespace for reading writes", async () => {
    const fetchSpy = installFetchSpy();

    await readingApi.prepare("project a", "document/1", {
      schema_version: "reading-prepare-request/v1",
      expected_source_sha256: "a".repeat(64),
    });
    await readingApi.createArtifact("project a", "document/1", {
      schema_version: "reading-action-request/v1",
      action: "EXPLANATION",
      source_sha256: "a".repeat(64),
      source_ref: {
        source_node_id: "node/1",
        start_offset: 0,
        end_offset: 12,
      },
    });

    const urls = fetchSpy.mock.calls.map(([input]) => String(input));
    expect(urls).toEqual([
      "/api/projects/project%20a/sor-documents/document%2F1/reading/prepare",
      "/api/projects/project%20a/sor-documents/document%2F1/reading/actions",
    ]);
    expect(urls.join(" ")).not.toMatch(
      /\/extraction|\/workspace|requirement-sets/,
    );
  });

  it("matches the frozen synchronous conversation contract exactly", async () => {
    const fetchSpy = installFetchSpy();

    await readingApi.conversations("project a", "document/1");
    await readingApi.createConversation("project a", "document/1", {
      schema_version: "reading-conversation-create-request/v1",
      title: "Explain the test plan",
    });
    await readingApi.conversationMessages(
      "project a",
      "document/1",
      "chat/1",
    );
    await readingApi.sendMessage(
      "project a",
      "document/1",
      "chat/1",
      {
        schema_version: "reading-message-create-request/v1",
        source_sha256: "a".repeat(64),
        content: "What is the acceptance threshold?",
        coverage_intent: "AUTO",
        source_ref: {
          source_node_id: "node-1",
          start_offset: 2,
          end_offset: 11,
        },
      },
    );
    await readingApi.clearConversation("project a", "document/1", "chat/1");
    await readingApi.deleteConversation(
      "project a",
      "document/1",
      "chat/1",
    );

    expect(fetchSpy.mock.calls.map(([input]) => String(input))).toEqual([
      "/api/projects/project%20a/sor-documents/document%2F1/reading/conversations",
      "/api/projects/project%20a/sor-documents/document%2F1/reading/conversations",
      "/api/projects/project%20a/sor-documents/document%2F1/reading/conversations/chat%2F1/messages",
      "/api/projects/project%20a/sor-documents/document%2F1/reading/conversations/chat%2F1/messages",
      "/api/projects/project%20a/sor-documents/document%2F1/reading/conversations/chat%2F1/messages",
      "/api/projects/project%20a/sor-documents/document%2F1/reading/conversations/chat%2F1",
    ]);
    expect(fetchSpy.mock.calls[1]?.[1]).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(fetchSpy.mock.calls[1]?.[1]?.body))).toEqual({
      schema_version: "reading-conversation-create-request/v1",
      title: "Explain the test plan",
    });
    expect(fetchSpy.mock.calls[2]?.[1]?.method).toBeUndefined();
    expect(fetchSpy.mock.calls[3]?.[1]).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(fetchSpy.mock.calls[3]?.[1]?.body))).toEqual({
      schema_version: "reading-message-create-request/v1",
      source_sha256: "a".repeat(64),
      content: "What is the acceptance threshold?",
      coverage_intent: "AUTO",
      source_ref: {
        source_node_id: "node-1",
        start_offset: 2,
        end_offset: 11,
      },
    });
    expect(fetchSpy.mock.calls[4]?.[1]).toMatchObject({ method: "DELETE" });
    expect(fetchSpy.mock.calls[5]?.[1]).toMatchObject({ method: "DELETE" });
  });

  it("keeps note revisions explicit", async () => {
    const fetchSpy = installFetchSpy();

    await readingApi.createNote("p", "d", {
      schema_version: "reading-note-create-request/v1",
      source_sha256: "a".repeat(64),
      source_ref: {
        source_node_id: "node-1",
        start_offset: 2,
        end_offset: 8,
      },
      content: "记录当前原文理解",
    });
    await readingApi.updateNote("p", "d", "note/1", {
      schema_version: "reading-note-update-request/v1",
      expected_revision: 4,
      content: "复核图纸标注",
    });
    await readingApi.deleteNote("p", "d", "note/1", 5);

    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/p/sor-documents/d/reading/notes",
    );
    expect(fetchSpy.mock.calls[0]?.[1]?.method).toBe("POST");
    expect(JSON.parse(String(fetchSpy.mock.calls[0]?.[1]?.body))).toEqual({
      schema_version: "reading-note-create-request/v1",
      source_sha256: "a".repeat(64),
      source_ref: {
        source_node_id: "node-1",
        start_offset: 2,
        end_offset: 8,
      },
      content: "记录当前原文理解",
    });
    expect(fetchSpy.mock.calls[1]?.[0]).toBe(
      "/api/projects/p/sor-documents/d/reading/notes/note%2F1",
    );
    expect(fetchSpy.mock.calls[1]?.[1]?.method).toBe("PATCH");
    expect(JSON.parse(String(fetchSpy.mock.calls[1]?.[1]?.body))).toEqual({
      schema_version: "reading-note-update-request/v1",
      expected_revision: 4,
      content: "复核图纸标注",
    });
    expect(fetchSpy.mock.calls[2]?.[0]).toBe(
      "/api/projects/p/sor-documents/d/reading/notes/note%2F1?expected_revision=5",
    );
    expect(fetchSpy.mock.calls[2]?.[1]?.method).toBe("DELETE");
  });

  it("loads exact source text one page at a time", async () => {
    const fetchSpy = installFetchSpy();

    await readingApi.sourcePage("p", "d", 23);

    expect(fetchSpy).toHaveBeenCalledOnce();
    expect(fetchSpy.mock.calls[0]?.[0]).toBe(
      "/api/projects/p/sor-documents/d/reading/source-pages/23",
    );
  });

  it("preserves nested reading error messages and status codes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            detail: {
              reason_code: "reading_context_not_prepared",
              message: "This document does not have a prepared reading context.",
            },
          }),
          {
            status: 404,
            headers: { "content-type": "application/json" },
          },
        ),
      ),
    );

    const error = await readingApi.context("p", "d").catch((cause) => cause);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 404,
      message: "This document does not have a prepared reading context.",
      payload: {
        detail: { reason_code: "reading_context_not_prepared" },
      },
    });
  });
});
