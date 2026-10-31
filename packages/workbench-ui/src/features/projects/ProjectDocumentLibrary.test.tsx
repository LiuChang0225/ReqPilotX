import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ProjectDocumentLibrary } from "./ProjectDocumentLibrary";
import { readDocumentWorkSummary } from "../workspace/documentWorkSummary";
import { WorkspaceProvider, type WorkspaceContextValue } from "../workspace/WorkspaceContext";
import { WorkbenchHostContext } from "../workspace/WorkbenchHostContext";
import * as client from "../../shared/api/client";
import { readViewedWork } from "../workspace/documentWorkSelection";

vi.mock("../workspace/documentWorkSummary", async original => ({ ...await original<object>(), readDocumentWorkSummary: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
beforeEach(() => { vi.mocked(readDocumentWorkSummary).mockImplementation(async (_project, document) => ({ sourceSha256: document, deliveryError: "", taskError: "",
  delivery: document === "a" ? { baseline_sha256: "basis", baseline_revision: 14, source_item_count: 448, requirement_count: 328 } : null,
  task: document === "b" ? { task_id: "t", job_id: "j", job_status: "RUNNING" } : null }) as never); });
function workspaceValue() {
  return { route: { projectId: "p", documentId: "a" }, updateRoute: vi.fn(), refreshProject: vi.fn().mockResolvedValue(undefined),
    selectedProject: { id: "p", name: "项目", customer_name: "客户" }, projects: [{ id: "p", name: "项目", customer_name: "客户" }],
    documents: [{ id: "a", file_name: "已保存.pdf", source_sha256: "a", created_at: "2026-09-10", file_size: 100 }, { id: "b", file_name: "运行中.pdf", source_sha256: "b", created_at: "2026-09-10", file_size: 100 }], knowledge: [] } as unknown as WorkspaceContextValue;
}
function mount(value = workspaceValue()) {
  return { value, ...render(<WorkspaceProvider value={value}><ProjectDocumentLibrary onRemove={vi.fn()} /></WorkspaceProvider>) };
}
it("opens the saved native ledger and filters reviewable outcomes", async () => {
  const { value } = mount();
  await screen.findByText("原文工作版 v14");
  fireEvent.click(screen.getByRole("button", { name: "打开成果" }));
  expect(value.updateRoute).toHaveBeenCalledWith(expect.objectContaining({ documentId: "a", checkpoint: "list", requirementSetId: "" }));
  fireEvent.click(screen.getByRole("button", { name: "可审阅" }));
  expect(screen.getByText("已保存.pdf")).toBeInTheDocument();
  expect(screen.queryByText("运行中.pdf")).not.toBeInTheDocument();
});
it("shows missing source information as actionable instead of permanently synchronizing", async () => {
  const value = workspaceValue(); value.documents = [{ ...value.documents[0], source_sha256: "" }];
  mount(value);
  expect(screen.getByText("来源信息未就绪")).toBeInTheDocument();
  expect(screen.getByText("请同步文档信息")).toBeInTheDocument();
  expect(screen.queryByText("正在同步…")).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("button", { name: "需处理" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "需处理" }));
  expect(screen.getByText("已保存.pdf")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "同步文档成果" }));
  expect(value.refreshProject).toHaveBeenCalled();
});
it("settles a failed summary read and provides a retry", async () => {
  vi.mocked(readDocumentWorkSummary).mockRejectedValue(new Error("offline"));
  const value = workspaceValue(); value.documents = value.documents.slice(0, 1); mount(value);
  await screen.findByText("任务状态暂未同步");
  expect(screen.getByText("成果状态待同步")).toBeInTheDocument();
  expect(screen.queryByText("正在同步…")).not.toBeInTheDocument();
  vi.mocked(readDocumentWorkSummary).mockResolvedValue({ sourceSha256: "a", delivery: null, task: null, deliveryError: "", taskError: "" });
  fireEvent.click(screen.getByRole("button", { name: "同步文档成果" }));
  await screen.findByText("尚无原文工作版");
});
it("retains visible known results while refreshing the same source", async () => {
  mount(); await screen.findByText("原文工作版 v14");
  vi.mocked(readDocumentWorkSummary).mockImplementation(() => new Promise(() => {}));
  fireEvent.click(screen.getByRole("button", { name: "同步文档成果" }));
  expect(screen.getByText("原文工作版 v14")).toBeInTheDocument();
  expect(screen.getByText("处理中", { selector: ".document-status-chip" })).toBeInTheDocument();
  expect(screen.getByText(/正在同步已有成果/)).toBeInTheDocument();
});
it("clears document search when changing source type", async () => {
  const value = workspaceValue(); value.knowledge = [{ id: "k", file_name: "知识依据.pdf" } as never]; mount(value);
  fireEvent.change(screen.getByRole("searchbox", { name: "搜索文档" }), { target: { value: "已保存" } });
  fireEvent.click(screen.getByRole("tab", { name: "内部知识 1" }));
  expect(screen.getByRole("searchbox", { name: "搜索文档" })).toHaveValue("");
  expect(screen.getByText("知识依据.pdf")).toBeInTheDocument();
});
it("dismisses removed document previews without leaving a dead file link", () => {
  const view = mount();
  fireEvent.click(screen.getByRole("button", { name: "预览 已保存.pdf" }));
  expect(screen.getByTitle("客户文档预览")).toBeInTheDocument();
  view.rerender(<WorkspaceProvider value={{ ...view.value, documents: [] }}><ProjectDocumentLibrary onRemove={vi.fn()} /></WorkspaceProvider>);
  expect(screen.queryByRole("region", { name: "文档预览" })).not.toBeInTheDocument();
});

it("opens the exact project evidence file from its knowledge preview", () => {
  const value = workspaceValue(); value.knowledge = [{ id: "k", file_name: "知识依据.pdf" } as never]; mount(value);
  fireEvent.click(screen.getByRole("tab", { name: "内部知识 1" }));
  fireEvent.click(screen.getByRole("button", { name: "预览 知识依据.pdf" }));
  fireEvent.click(screen.getByRole("button", { name: "查看这份项目依据" }));
  expect(value.updateRoute).toHaveBeenCalledWith({ task: "knowledge", knowledgeSpace: "project", knowledgePage: "k" });
});
it("gives empty project search feedback and closes the picker after selection", () => {
  mount(); const picker = screen.getByText("项目", { selector: "summary" }); fireEvent.click(picker);
  fireEvent.change(screen.getByRole("searchbox", { name: "搜索项目" }), { target: { value: "不存在" } });
  expect(screen.getByText("没有匹配的项目，请调整搜索。")).toBeInTheDocument();
  fireEvent.change(screen.getByRole("searchbox", { name: "搜索项目" }), { target: { value: " 客户 " } });
  fireEvent.click(screen.getByRole("option", { name: "项目 客户" }));
  expect(picker.closest("details")).not.toHaveAttribute("open");
});
it("never shows the previous source's late summary after switching projects", async () => {
  let finish!: (value: never) => void;
  vi.mocked(readDocumentWorkSummary).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const value = workspaceValue(); value.documents = value.documents.slice(0, 1); const view = mount(value);
  const other = { ...value, route: { ...value.route, projectId: "q" }, documents: [] };
  view.rerender(<WorkspaceProvider value={other}><ProjectDocumentLibrary onRemove={vi.fn()} /></WorkspaceProvider>);
  await act(async () => { finish({ sourceSha256: "a", delivery: { baseline_revision: 999 }, task: null, deliveryError: "", taskError: "" } as never); });
  expect(screen.queryByText("原文工作版 v999")).not.toBeInTheDocument();
});
it.each([
  ["FAILED", undefined, "本轮未完成"],
  ["CANCELLED", undefined, "已暂停"],
  ["SUCCEEDED", "INTERRUPTED", "已暂停"],
])("keeps saved outcomes reviewable when the latest task is %s / %s", async (job_status, runtime_status, label) => {
  const value = workspaceValue(); value.documents = value.documents.slice(0, 1);
  vi.mocked(readDocumentWorkSummary).mockResolvedValue({ sourceSha256: "a", deliveryError: "", taskError: "",
    delivery: { baseline_sha256: "basis", baseline_revision: 14, source_item_count: 448, requirement_count: 328 },
    task: { task_id: "t", job_id: "j", job_status, runtime_status },
  } as never);
  mount(value);
  await screen.findByText("原文工作版 v14");
  expect(screen.getByText(label, { selector: ".document-status-chip" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "可审阅" }));
  fireEvent.click(screen.getByRole("button", { name: "打开成果" }));
  expect(value.updateRoute).toHaveBeenCalledWith(expect.objectContaining({ checkpoint: "list", documentId: "a", requirementSetId: "" }));
});
it("does not invent a task or discard saved outcomes when no task record exists", async () => {
  const value = workspaceValue(); value.documents = value.documents.slice(0, 1); mount(value);
  await screen.findByText("原文工作版 v14");
  expect(screen.getByText("尚未发起任务", { selector: ".document-status-chip" })).toBeInTheDocument();
  expect(screen.getByText("448 原文 · 328 需求")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "打开成果" })).toBeEnabled();
});
it("offers explicit work selection instead of opening the latest external pointer", async () => {
  localStorage.clear();
  const value = workspaceValue(); value.documents = value.documents.slice(0, 1);
  const a = `plugin-${"a".repeat(32)}`, b = `plugin-${"b".repeat(32)}`;
  vi.mocked(readDocumentWorkSummary).mockResolvedValue({ sourceSha256: "a", delivery: null, task: null, deliveryError: "", taskError: "",
    pluginDraft: { session_id: b, source_sha256: "a", revision_id: null, revision_number: 0, workbench_path: "/workspace" } });
  const get = vi.spyOn(client, "request").mockResolvedValue({ schema_version: "reqpilot-plugin-workbench-sessions/v1", project_id: "p", document_id: "a", source_sha256: "a", unavailable: [],
    sessions: [a, b].map((id, index) => ({ session_id: id, project_id: "p", document_id: "a", source_sha256: "a", title: `独立任务 ${index + 1}`, revision_id: index ? null : "1".repeat(64), revision_number: index ? 0 : 1,
      stages: index ? [] : [{ stage: "MAP", count: 3, artifact_sha256: "map" }], availability: index ? "NO_ARTIFACTS" : "AVAILABLE" })) });
  try {
    mount(value); await screen.findByText("插件已连接");
    fireEvent.click(screen.getByRole("button", { name: "打开工作台" }));
    await screen.findByText("独立任务 1");
    expect(value.updateRoute).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /独立任务 2/ }));
    expect(value.updateRoute).toHaveBeenCalledWith(expect.objectContaining({ documentId: "a", pluginSessionId: b, resultRevision: undefined, checkpoint: "structure" }));
    expect(readViewedWork("p", "a", "a")).toMatchObject({ producer: "EXTERNAL_PLUGIN", sessionId: b });
    expect(get.mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
  } finally { get.mockRestore(); localStorage.clear(); }
});

it("routes hosted PDF preview through the application resource and PDF viewer contracts", () => {
  const value = workspaceValue();
  value.documents = [{ ...value.documents[0], file_name: "hosted-source.pdf" }];
  const resourceUrl = vi.fn((path: string) => `https://pair.test/reqpilotx/service${path}`);
  const pdfViewerUrl = vi.fn((source: string, page: number, embedded: boolean) => `https://service.test/pdf-viewer?pdf=${encodeURIComponent(source)}&page=${page}&embedded=${embedded ? 1 : 0}`);
  render(<WorkbenchHostContext.Provider value={{ readOnly: false, resourceUrl, pdfViewerUrl }}><WorkspaceProvider value={value}><ProjectDocumentLibrary onRemove={vi.fn()} /></WorkspaceProvider></WorkbenchHostContext.Provider>);
  fireEvent.click(screen.getByRole("button", { name: "预览 hosted-source.pdf" }));
  const source = "https://pair.test/reqpilotx/service/api/projects/p/sor-documents/a/file";
  expect(resourceUrl).toHaveBeenCalledWith("/api/projects/p/sor-documents/a/file");
  expect(pdfViewerUrl).toHaveBeenCalledWith(source, 1, true);
  expect(screen.getByTitle("客户文档预览")).toHaveAttribute("src", `https://service.test/pdf-viewer?pdf=${encodeURIComponent(source)}&page=1&embedded=1`);
});

it("routes hosted non-PDF download to the mapped authorized resource path", () => {
  const value = workspaceValue();
  value.documents = [{ ...value.documents[0], id: "doc-2", file_name: "synthetic-source.docx" }];
  const resourceUrl = vi.fn((path: string) => `https://pair.test/reqpilotx/service${path}`);
  const pdfViewerUrl = vi.fn();
  render(<WorkbenchHostContext.Provider value={{ readOnly: true, resourceUrl, pdfViewerUrl }}><WorkspaceProvider value={value}><ProjectDocumentLibrary onRemove={vi.fn()} /></WorkspaceProvider></WorkbenchHostContext.Provider>);
  fireEvent.click(screen.getByRole("button", { name: "预览 synthetic-source.docx" }));
  expect(resourceUrl).toHaveBeenCalledWith("/api/projects/p/sor-documents/doc-2/file");
  expect(screen.getByRole("link", { name: "打开原文件" })).toHaveAttribute("href", "https://pair.test/reqpilotx/service/api/projects/p/sor-documents/doc-2/file");
  expect(pdfViewerUrl).not.toHaveBeenCalled();
});

it("retains standalone PDF viewer and raw API download behavior", () => {
  const value = workspaceValue();
  value.documents = [{ ...value.documents[0], file_name: "standalone.pdf" }];
  const view = mount(value);
  fireEvent.click(screen.getByRole("button", { name: "预览 standalone.pdf" }));
  expect(screen.getByTitle("客户文档预览")).toHaveAttribute("src", `/pdf-viewer?embedded=1&fit=page&pdf=${encodeURIComponent("/api/projects/p/sor-documents/a/file")}&page=1`);
  view.rerender(<WorkspaceProvider value={{ ...value, documents: [{ ...value.documents[0], id: "doc-3", file_name: "standalone.docx" }] }}><ProjectDocumentLibrary onRemove={vi.fn()} /></WorkspaceProvider>);
  fireEvent.click(screen.getByRole("button", { name: "预览 standalone.docx" }));
  expect(screen.getByRole("link", { name: "打开原文件" })).toHaveAttribute("href", "/api/projects/p/sor-documents/doc-3/file");
});
