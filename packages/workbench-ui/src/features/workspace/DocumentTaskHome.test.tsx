import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DocumentAgentContext, type DocumentAgentContextValue } from "./DocumentAgentContext";
import { DocumentTaskHome } from "./DocumentTaskHome";

const harness = vi.hoisted(() => ({
  source: "a".repeat(64), documentId: "d", entry: "quick" as "quick" | "agent",
  request: vi.fn(), availability: { ready: true, message: "", refresh: vi.fn() },
}));
vi.mock("./WorkspaceContext", () => ({ useWorkspace: () => ({
  route: { projectId: "p", documentId: harness.documentId, entry: harness.entry },
  selectedDocument: { source_sha256: harness.source },
}) }));
vi.mock("../../shared/api/client", () => ({ request: (...args: unknown[]) => harness.request(...args) }));
vi.mock("../extraction/saasAgentAvailability", () => ({ useSaasNativeAgentAvailability: () => harness.availability }));
beforeEach(() => { harness.source = "a".repeat(64); harness.documentId = "d"; harness.entry = "quick"; harness.availability.ready = true; harness.availability.message = ""; harness.request.mockReset(); });
afterEach(cleanup);
function mount(task: DocumentAgentContextValue["task"] = null) {
  const value = { bind: vi.fn(), task, show: vi.fn(), openStage: vi.fn(), historyHost: null };
  const element = () => <DocumentAgentContext.Provider value={value}><DocumentTaskHome fileName="source.pdf" sourceFileUrl="/api/source.pdf" /></DocumentAgentContext.Provider>;
  const view = render(element());
  return { value, view, rerender: () => view.rerender(element()) };
}
function capture(overrides: Record<string, unknown> = {}) {
  return { schema_version: "project-fast-source-capture/v1", project_id: "p", document_id: "d", source_sha256: "a".repeat(64),
    capture_status: "PARTIAL", elapsed_seconds: 1.2, cache_reused: false,
    page_facts: [{ page: 1, native_text_characters: 40 }, { page: 2, native_text_characters: 20 }],
    capture_counts: { table: 1, image: 2 }, gaps: [{ code: "missing-text", message: "此页文字未完整读取，请核对原页。", pages: [2] }],
    source_ir: { nodes: [{ source_node_id: "source-1", raw_text: "1.2 完整试验需要包含温度条件", native_locator: { page_number: 2 } }] },
    structure_proposal: { nodes: [{ structure_node_id: "structure-1", kind: "clause", source_order: 1, source_refs: [{ source_id: "source-1" }] }] },
    ...overrides };
}

it("starts with direct reading and never opens an Agent or makes a request automatically", () => {
  const { value } = mount();
  expect(screen.getByRole("button", { name: "快速读取原文" })).toBeEnabled();
  expect(value.show).not.toHaveBeenCalled();
  expect(harness.request).not.toHaveBeenCalled();
  expect(screen.getByRole("link", { name: "打开原文件" })).toHaveAttribute("href", "/api/source.pdf");
  expect(screen.getByTitle("任务原文件预览")).toHaveAttribute("src", expect.stringContaining("page=1"));
});

it("reads the selected source and shows browsable candidate text, problems and original-page access", async () => {
  harness.request.mockResolvedValue(capture());
  const { value } = mount();
  fireEvent.click(screen.getByRole("button", { name: "快速读取原文" }));
  expect(harness.request).toHaveBeenCalledWith("/api/projects/p/sor-documents/d/source-capture/fast", expect.objectContaining({ method: "POST", body: JSON.stringify({ expected_source_sha256: harness.source }) }));
  expect(await screen.findByRole("heading", { name: "程序候选" })).toBeVisible();
  expect(screen.getByText("以下内容供核对，尚未生成正式 SRC 或完成需求分类。")).toBeVisible();
  expect(screen.getByText("60 字符")).toBeVisible();
  expect(screen.getByText("此页文字未完整读取，请核对原页。")).toBeVisible();
  const row = screen.getByRole("list", { name: "结构候选清单" }).querySelector("details")!;
  fireEvent.click(row.querySelector("summary")!);
  fireEvent.click(screen.getByRole("button", { name: "查看原件第 2 页" }));
  expect(screen.getByTitle("任务原文件预览")).toHaveAttribute("src", expect.stringContaining("page=2"));
  fireEvent.change(screen.getByRole("searchbox", { name: "搜索结构候选" }), { target: { value: "不匹配的内容" } });
  expect(screen.getByText("没有匹配的候选，试试其他编号或文字。")).toBeVisible();
  expect(value.show).not.toHaveBeenCalled();
});

it("does not run concurrent capture requests and permits retry after a failure", async () => {
  let reject!: (reason: Error) => void;
  harness.request.mockReturnValueOnce(new Promise((_, failure) => { reject = failure; }));
  mount();
  const button = screen.getByRole("button", { name: "快速读取原文" });
  fireEvent.click(button); fireEvent.click(button);
  expect(harness.request).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "正在读取原文…" })).toBeDisabled();
  await act(async () => reject(new Error("读取连接中断")));
  expect(screen.getByRole("alert")).toHaveTextContent("读取连接中断");
  harness.request.mockResolvedValueOnce(capture());
  fireEvent.click(screen.getByRole("button", { name: "重试读取原文" }));
  expect(await screen.findByRole("heading", { name: "程序候选" })).toBeVisible();
  expect(harness.request).toHaveBeenCalledTimes(2);
});

it.each([{ source_sha256: "b".repeat(64) }, { document_id: "other-document" }, { project_id: "other-project" }])("rejects capture identity drift: %o", async overrides => {
  harness.request.mockResolvedValue(capture(overrides));
  mount();
  fireEvent.click(screen.getByRole("button", { name: "快速读取原文" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("读取结果与当前原件不一致");
  expect(screen.queryByRole("heading", { name: "程序候选" })).not.toBeInTheDocument();
});

it("aborts and ignores an old request when the document changes", async () => {
  let resolve!: (value: unknown) => void;
  harness.request.mockReturnValueOnce(new Promise(done => { resolve = done; }));
  const { rerender } = mount();
  fireEvent.click(screen.getByRole("button", { name: "快速读取原文" }));
  const signal = harness.request.mock.calls[0][1].signal as AbortSignal;
  harness.documentId = "next-document";
  rerender();
  expect(signal.aborted).toBe(true);
  await act(async () => resolve(capture()));
  expect(screen.queryByRole("heading", { name: "程序候选" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "快速读取原文" })).toBeEnabled();
});

it("opens the optional Agent only on an explicit click with a concrete document goal", () => {
  harness.entry = "agent";
  const { value } = mount();
  expect(value.show).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "使用 Agent 整理" }));
  expect(value.show).toHaveBeenCalledWith(expect.stringContaining("完整BOM、试验及跨页条件"), "document");
  expect(harness.request).not.toHaveBeenCalled();
});

it("offers configuration when the Agent is unavailable while direct reading remains enabled", () => {
  harness.entry = "agent"; harness.availability.ready = false; harness.availability.message = "模型尚未配置";
  const { value } = mount();
  expect(screen.getByRole("link", { name: "配置 Agent" })).toHaveAttribute("href", "/workspace?task=projects&settings=agent");
  expect(screen.queryByRole("button", { name: "使用 Agent 整理" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "快速读取原文" })).toBeEnabled();
  expect(value.show).not.toHaveBeenCalled();
});

it.each(["FAILED", "CANCELLED", "SUCCEEDED"])("preserves task access without stale running progress after %s", status => {
  mount({ task_id: "t", job_id: "j", job_status: status, can_steer: false, can_resume: false, can_interrupt: false, progress: { message: "正在执行上一轮任务" } });
  expect(screen.queryByText("正在执行上一轮任务")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "查看任务与补充意见" })).toBeEnabled();
});
it("continues to display real progress for an active task", () => {
  mount({ task_id: "t", job_id: "j", job_status: "RUNNING", can_steer: true, can_resume: false, can_interrupt: true, progress: { message: "正在查阅第 4 页" } });
  expect(screen.getByRole("status")).toHaveTextContent("正在查阅第 4 页");
});

it("reveals large candidate sets in bounded batches", async () => {
  const nodes = Array.from({ length: 70 }, (_, i) => ({ structure_node_id: `candidate-${i}`, kind: "content", source_order: i, source_refs: [{ source_id: "source-1" }] }));
  harness.request.mockResolvedValue(capture({ structure_proposal: { nodes } }));
  mount(); fireEvent.click(screen.getByRole("button", { name: "快速读取原文" }));
  await waitFor(() => expect(screen.getByRole("list", { name: "结构候选清单" }).children).toHaveLength(50));
  fireEvent.click(screen.getByRole("button", { name: "再显示 50 个候选" }));
  expect(screen.getByRole("list", { name: "结构候选清单" }).children).toHaveLength(70);
});
