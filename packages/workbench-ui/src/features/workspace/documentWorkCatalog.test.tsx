import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { DocumentWorkChoices } from "./DocumentWorkPicker";
import { selectedPluginWork, useDocumentWorkCatalog, validateDocumentWorkCatalog, type DocumentWorkCatalog, type PluginWorkSummary } from "./documentWorkCatalog";
import { readViewedWork, rememberViewedWork } from "./documentWorkSelection";
vi.mock("../../shared/api/client", () => ({ request: vi.fn() }));
const source = "c".repeat(64), a = `plugin-${"a".repeat(32)}`, b = `plugin-${"b".repeat(32)}`;
const revision = "1".repeat(64), latest = "2".repeat(64);
function item(id = a, noArtifacts = false): PluginWorkSummary {
  return { session_id: id, project_id: "p", document_id: "d", source_sha256: source, revision_id: noArtifacts ? null : latest,
    revision_number: noArtifacts ? 0 : 2, stages: noArtifacts ? [] : [{ stage: "MAP", artifact_sha256: "map", count: 3 }],
    title: id === a ? "工作 A" : "工作 B", availability: noArtifacts ? "NO_ARTIFACTS" : "AVAILABLE" };
}
function catalog(): DocumentWorkCatalog { return { schema_version: "reqpilot-plugin-workbench-sessions/v1", project_id: "p", document_id: "d", source_sha256: source, sessions: [item(), item(b, true)], unavailable: [] }; }
beforeEach(() => { localStorage.clear(); vi.mocked(request).mockReset(); vi.mocked(request).mockResolvedValue(catalog()); });
afterEach(cleanup);
it("lists independent tasks including source-only work without electing a winner", async () => {
  const select = vi.fn();
  render(<DocumentWorkChoices projectId="p" documentId="d" sourceSha256={source} summary={null} onSelect={select} />);
  await screen.findByText("工作 A");
  expect(screen.getByText("仅有来源 · 尚无阶段成果")).toBeVisible();
  expect(select).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { pressed: true })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /工作 B/ }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ session_id: b, revision_id: null }));
  expect(vi.mocked(request).mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
});
it("preserves each task's reviewed version when switching through source-only and native work", () => {
  rememberViewedWork("p", "d", source, { producer: "EXTERNAL_PLUGIN", sessionId: a, revisionId: revision });
  rememberViewedWork("p", "d", source, { producer: "EXTERNAL_PLUGIN", sessionId: b });
  expect(readViewedWork("p", "d", source)).toMatchObject({ producer: "EXTERNAL_PLUGIN", sessionId: b });
  rememberViewedWork("p", "d", source, { producer: "NATIVE" });
  expect(selectedPluginWork("p", "d", source, item()).work.revisionId).toBe(revision);
  expect(selectedPluginWork("p", "other", source, item()).work.revisionId).toBe(latest);
  expect(selectedPluginWork("p", "d", source, item(b, true)).work.revisionId).toBeUndefined();
});
it("keeps source-mismatched and duplicate entries unavailable without hiding healthy tasks", () => {
  const value = catalog(); value.sessions.push({ ...item(`plugin-${"d".repeat(32)}`), source_sha256: "other" });
  const valid = validateDocumentWorkCatalog(value, "p", "d", source);
  expect(valid.sessions).toHaveLength(2); expect(valid.unavailable).toHaveLength(1);
  value.sessions.push(item());
  expect(validateDocumentWorkCatalog(value, "p", "d", source).sessions.map(work => work.session_id)).toEqual([b]);
  expect(() => validateDocumentWorkCatalog({ ...catalog(), document_id: "elsewhere" }, "p", "d", source)).toThrow("不一致");
});
it("shows unavailable entries as explanations without clickable replacement actions", async () => {
  vi.mocked(request).mockResolvedValue({ ...catalog(), unavailable: [{ session_id: "plugin-old", reason: "旧原件版本不能自动替换来源" }] });
  render(<DocumentWorkChoices projectId="p" documentId="d" sourceSha256={source} summary={null} onSelect={vi.fn()} />);
  await screen.findByText(/暂不可用的工作/);
  fireEvent.click(screen.getByText(/暂不可用的工作/));
  expect(screen.getByText(/旧原件版本不能自动替换来源/)).toBeVisible();
  expect(screen.queryByRole("button", { name: /旧原件/ })).not.toBeInTheDocument();
});
it("ignores a late catalog from a document that has been left", async () => {
  let finish!: (value: DocumentWorkCatalog) => void;
  vi.mocked(request).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const hook = renderHook(({ document }) => useDocumentWorkCatalog("p", document, source), { initialProps: { document: "d" } });
  vi.mocked(request).mockResolvedValue({ ...catalog(), document_id: "next", sessions: [] });
  hook.rerender({ document: "next" });
  await waitFor(() => expect(hook.result.current.value?.document_id).toBe("next"));
  await act(async () => finish(catalog()));
  expect(hook.result.current.value?.document_id).toBe("next"); expect(hook.result.current.value?.sessions).toHaveLength(0);
});
it("keeps a known catalog visible if synchronization fails", async () => {
  const hook = renderHook(() => useDocumentWorkCatalog("p", "d", source));
  await waitFor(() => expect(hook.result.current.value?.sessions).toHaveLength(2));
  vi.mocked(request).mockRejectedValue(new Error("连接中断")); act(() => hook.result.current.refresh());
  await waitFor(() => expect(hook.result.current.error).toBe("连接中断"));
  expect(hook.result.current.value?.sessions).toHaveLength(2);
});
