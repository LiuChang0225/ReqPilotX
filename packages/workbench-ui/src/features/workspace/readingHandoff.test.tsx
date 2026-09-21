import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { sourceFeedbackDraftKey } from "../extraction/sourceItemDrafts";
import type { ReadingChatMessageV1, ReadingContextV1, ReadingNoteV1 } from "../reading/contracts";
import { rememberExtractionCheckpoint, rememberViewedWork } from "./documentWorkSelection";
import { buildReadingFindingDraft, consumeReadingHandoffIntent, readReadingHandoffTarget, storeReadingHandoffIntent, useReadingHandoff, validateReadingHandoffTarget } from "./readingHandoff";
import type { PluginState } from "./documentWorkProvider";
vi.mock("../../shared/api/client", () => ({ request: vi.fn() }));
const source = "a".repeat(64), revision = "b".repeat(64), newest = "c".repeat(64), session = `plugin-${"d".repeat(32)}`;
let context: ReadingContextV1;
const state = (): PluginState => ({ project_id: context.project_id, document_id: "doc", source_sha256: source, session_id: session, revision_id: newest, revision_number: 3, stages: [], map_confirmation_required: false, feedback: [], versions: [{ revision_id: revision, number: 2, stages: [] }, { revision_id: newest, number: 3, stages: [] }] });
const message = () => ({ role: "ASSISTANT", status: "ACTIVE", project_id: context.project_id, sor_document_id: "doc", source_sha256: source, reading_session_id: "reading-session", effective_bundle_id: "bundle", content: "检查这个未决条件，不改写客户原文。", message_id: "message", conversation_id: "conversation", citations: [{ citation_id: "c1", source_node_id: "n1", start_offset: 0, end_offset: 3, page_number: 2, quote: "TBD" }] }) as ReadingChatMessageV1;
beforeEach(() => {
  for (const key of ["localStorage", "sessionStorage"]) {
    const values = new Map<string, string>();
    const storage: Storage = { get length() { return values.size; }, clear: () => values.clear(), getItem: key => values.get(key) ?? null, setItem: (key, value) => { values.set(key, value); }, removeItem: key => { values.delete(key); }, key: index => [...values.keys()][index] ?? null };
    vi.stubGlobal(key, storage); Object.defineProperty(window, key, { configurable: true, value: storage });
  }
  localStorage.clear(); sessionStorage.clear(); vi.clearAllMocks();
  context = { project_id: `project-${crypto.randomUUID()}`, sor_document_id: "doc", source_sha256: source, reading_session_id: "reading-session", effective_bundle_id: "bundle" } as ReadingContextV1;
  rememberViewedWork(context.project_id, "doc", source, { producer: "EXTERNAL_PLUGIN", sessionId: session, revisionId: revision });
  vi.mocked(request).mockResolvedValue(state());
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("returns to the remembered historical version and never replaces it with the newer head", () => {
  rememberExtractionCheckpoint(context.project_id, "doc", source, { producer: "EXTERNAL_PLUGIN", sessionId: session, revisionId: revision }, "structure");
  const target = readReadingHandoffTarget(context.project_id, "doc", source)!;
  expect(target.url).toContain(`resultRevision=${revision}`); expect(target.url).toContain("checkpoint=structure");
  expect(validateReadingHandoffTarget(target, state())).toEqual({ number: 2, historical: true });
  expect(readReadingHandoffTarget(context.project_id, "doc", "e".repeat(64))).toBeNull();
  expect(() => validateReadingHandoffTarget(target, { ...state(), source_sha256: newest })).toThrow("不一致");
});
it("prepares a source-bound message in the existing document draft without any POST or overwrite", async () => {
  const hook = renderHook(() => useReadingHandoff(context)); await waitFor(() => expect(hook.result.current.reason).toBe(""));
  let text = ""; act(() => { text = hook.result.current.prepare({ kind: "message", value: message() }); });
  const key = sourceFeedbackDraftKey(context.project_id, "doc", `external:${session}:document`, revision);
  expect(JSON.parse(localStorage.getItem(key)!).text).toBe(text);
  for (const part of ["待核对的阅读发现", source, revision, '"message_id": "message"', '"source_node_id": "n1"']) expect(text).toContain(part);
  expect(() => hook.result.current.prepare({ kind: "message", value: { ...message(), content: "另一份发现" } })).toThrow("未覆盖");
  expect(JSON.parse(localStorage.getItem(key)!).text).toBe(text);
  expect(vi.mocked(request).mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
  expect(sessionStorage.length).toBe(0);
});
it("preserves a draft written in another surface before handoff", async () => {
  const hook = renderHook(() => useReadingHandoff(context)); await waitFor(() => expect(hook.result.current.reason).toBe(""));
  const key = sourceFeedbackDraftKey(context.project_id, "doc", `external:${session}:document`, revision);
  localStorage.setItem(key, JSON.stringify({ schema: 1, text: "原有草稿" }));
  expect(() => hook.result.current.prepare({ kind: "message", value: message() })).toThrow("未覆盖");
  expect(JSON.parse(localStorage.getItem(key)!).text).toBe("原有草稿");
});
it("includes note revision and locator and rejects old captures or oversized content without truncation", () => {
  const target = readReadingHandoffTarget(context.project_id, "doc", source)!;
  const note = { ...message(), note_id: "note", revision: 4, source_ref: { source_node_id: "n2", start_offset: 3, end_offset: 9 } } as unknown as ReadingNoteV1;
  expect(buildReadingFindingDraft(context, { kind: "note", value: note }, target)).toContain('"note_revision": 4');
  expect(() => buildReadingFindingDraft(context, { kind: "message", value: { ...message(), effective_bundle_id: "old-bundle" } }, target)).toThrow("捕获版本");
  expect(() => buildReadingFindingDraft(context, { kind: "note", value: { ...note, status: "STALE" } }, target)).toThrow("有效阅读结果");
  expect(() => buildReadingFindingDraft(context, { kind: "message", value: { ...message(), content: "a".repeat(12000) } }, target)).toThrow("未截断");
});
it("consumes the open intent only once for the exact source, session and viewed version", () => {
  const target = readReadingHandoffTarget(context.project_id, "doc", source)!;
  const identity = { projectId: context.project_id, documentId: "doc", sourceSha256: source, sessionId: session, revisionId: revision };
  storeReadingHandoffIntent(target);
  expect(consumeReadingHandoffIntent({ ...identity, revisionId: newest })).toBeNull();
  expect(consumeReadingHandoffIntent({ ...identity, sourceSha256: newest })).toBeNull();
  expect(consumeReadingHandoffIntent(identity)?.scope).toBe("document");
  expect(consumeReadingHandoffIntent(identity)).toBeNull();
});
it("keeps native return available but refuses to guess its task or baseline for writes", () => {
  rememberViewedWork(context.project_id, "doc", source, { producer: "NATIVE" });
  const hook = renderHook(() => useReadingHandoff(context));
  expect(hook.result.current.target?.url).toContain("task=extraction");
  expect(hook.result.current.reason).toContain("暂不写入原生任务草稿");
  expect(() => hook.result.current.prepare({ kind: "message", value: message() })).toThrow("暂不写入"); expect(request).not.toHaveBeenCalled();
});
