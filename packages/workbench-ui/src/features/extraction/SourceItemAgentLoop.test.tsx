import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { SourceItemAgentLoop, type AgentLoopTask } from "./SourceItemAgentLoop";

vi.mock("../../shared/api/client", async importOriginal => ({ ...await importOriginal<object>(), request: vi.fn() }));
const props = { projectId: "p", documentId: "d", sourceSha256: "a".repeat(64), baselineSha256: "b".repeat(64), selectedSourceItemId: "src-1", onUpdated: vi.fn(), onTaskChange: vi.fn() };
const finished: AgentLoopTask = { task_id: "task-1", job_id: "job-1", job_status: "SUCCEEDED", can_steer: false, can_interrupt: false, can_resume: true,
  result: { status: "NEEDS_CONTINUATION", application_saved: false, message: "还需核对原图。" } };
function setup(task: AgentLoopTask | null = null, overrides: Partial<Parameters<typeof SourceItemAgentLoop>[0]> = {}) {
  vi.mocked(request).mockImplementation(async path => String(path).includes("/events?") ? { events: [], next_cursor: 0 } : task as never);
  const view = render(<SourceItemAgentLoop {...props} {...overrides} />);
  const details = screen.getByText("Agent 协作", { selector: "summary span" }).closest("details")!;
  details.open = true; fireEvent(details, new Event("toggle"));
  return view;
}
afterEach(() => { cleanup(); vi.clearAllMocks(); localStorage.clear(); });

describe("SourceItemAgentLoop", () => {
  it.each([
    ["PROVIDER_LOOP_AUTHENTICATIONERROR", "模型服务认证失败"],
    ["PROVIDER_LOOP_PERMISSIONDENIEDERROR", "模型服务拒绝访问"],
  ])("explains a persisted provider failure and keeps the same-task recovery path: %s", async (code, message) => {
    setup({ ...finished, job_status: "FAILED", error_message: code });
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByText("诊断代码")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续此任务" })).toBeEnabled();
    expect(screen.queryByText("本轮已结束")).not.toBeInTheDocument();
    expect(vi.mocked(request).mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
    expect(props.onUpdated).not.toHaveBeenCalled();
  });
  it("restores selected-item scope on each explicit item feedback entry after document scope was used", async () => {
    const view = setup();
    fireEvent.change(screen.getByLabelText("修改范围"), { target: { value: "document" } });
    const enterItem = (id: number) => view.rerender(<SourceItemAgentLoop {...props} openRequest={id} requestedScope={{ id, scope: "selected" }} />);
    enterItem(1);
    expect(screen.getByLabelText("修改范围")).toHaveValue("selected");
    fireEvent.change(screen.getByLabelText("修改范围"), { target: { value: "document" } });
    enterItem(2);
    expect(screen.getByLabelText("修改范围")).toHaveValue("selected");
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "只核对当前条目" } });
    fireEvent.click(screen.getByRole("button", { name: "开始新任务" }));
    await waitFor(() => expect(vi.mocked(request).mock.calls.some(([, options]) => options?.method === "POST")).toBe(true));
    const sent = vi.mocked(request).mock.calls.find(([, options]) => options?.method === "POST")!;
    expect(JSON.parse(String(sent[1]?.body))).toMatchObject({ source_item_ids: ["src-1"], expected_baseline_sha256: props.baselineSha256 });
  });
  it.each([false, true])("starts an autonomous task unless structure confirmation is explicitly selected: %s", async (requireConfirmation) => {
    setup(null, { mode: "extract" });
    const checkbox = screen.getByRole("checkbox", { name: "先核对文档结构，确认后继续" });
    expect(checkbox).not.toBeChecked();
    if (requireConfirmation) fireEvent.click(checkbox);
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "自主完成这份合成文档并保存成果" } });
    fireEvent.click(screen.getByRole("button", { name: "开始新任务" }));
    await waitFor(() => expect(vi.mocked(request).mock.calls.some(([, options]) => options?.method === "POST")).toBe(true));
    const sent = vi.mocked(request).mock.calls.find(([, options]) => options?.method === "POST")!;
    expect(JSON.parse(String(sent[1]?.body))).toMatchObject({ require_map_confirmation: requireConfirmation, expected_source_sha256: props.sourceSha256 });
  });
  it("shows newly available stages without stealing focus from source review", async () => {
    const sourceControl = document.createElement("button");
    document.body.append(sourceControl);
    sourceControl.focus();
    const task = { ...finished, stages: [{ stage: "MAP" as const, artifact_sha256: "c".repeat(64), count: 3 }] };
    vi.mocked(request).mockImplementation(async path => String(path).includes("/events?") ? { events: [], next_cursor: 0 } : task as never);
    try {
      render(<SourceItemAgentLoop {...props} mode="extract" />);
      await screen.findByRole("dialog", { name: "Agent 连续任务" });
      expect(sourceControl).toHaveFocus();
      expect(screen.getByRole("button", { name: /查看文档结构/ })).toBeInTheDocument();
    } finally { sourceControl.remove(); }
  });

  it("keeps the nonmodal panel outside clipping containers and closes from the composer", async () => {
    setup();
    const dialog = await screen.findByRole("dialog", { name: "Agent 连续任务" });
    expect(dialog.parentElement).toBe(document.body);
    expect(dialog).toHaveAttribute("aria-modal", "false");
    fireEvent.click(screen.getByRole("button", { name: "复核当前分类" }));
    const draft = screen.getByLabelText("任务或修改意见") as HTMLTextAreaElement;
    expect(draft.value).toContain("完整原文");
    expect(vi.mocked(request).mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
    fireEvent.keyDown(draft, { key: "Escape" });
    expect(dialog).toHaveAttribute("hidden");
    const trigger = screen.getByText("Agent 协作", { selector: "summary span" }).closest("summary")!;
    expect(trigger).toHaveFocus();
    expect(draft.value).toContain("完整原文");
  });

  it("opens from the shared task entry and can close and reopen without losing a draft", async () => {
    vi.mocked(request).mockResolvedValue(null);
    const view = render(<SourceItemAgentLoop {...props} />);
    const details = screen.getByText("Agent 协作", { selector: "summary span" }).closest("details")!;
    expect(details).not.toHaveAttribute("open");
    view.rerender(<SourceItemAgentLoop {...props} openRequest={1} />);
    await waitFor(() => expect(details).toHaveAttribute("open"));
    expect(screen.getByLabelText("任务或修改意见")).toHaveFocus();
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "保留未发送的核对意见" } });
    fireEvent.keyDown(details, { key: "Escape" });
    await waitFor(() => expect(details).not.toHaveAttribute("open"));
    view.rerender(<SourceItemAgentLoop {...props} openRequest={2} />);
    await waitFor(() => expect(details).toHaveAttribute("open"));
    expect(screen.getByLabelText("任务或修改意见")).toHaveValue("保留未发送的核对意见");
    expect(vi.mocked(request).mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
  });

  it("starts a version-bound selected-item task while preserving later draft typing", async () => {
    setup(); await waitFor(() => expect(request).toHaveBeenCalled());
    let resolve: (value: AgentLoopTask) => void = () => {};
    vi.mocked(request).mockImplementation((path, options) => options?.method === "POST" ? new Promise(done => { resolve = done as typeof resolve; }) : Promise.resolve(null) as never);
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "核对图表与分类" } });
    fireEvent.click(screen.getByRole("button", { name: "开始新任务" }));
    const call = vi.mocked(request).mock.calls.find(([, opts]) => opts?.method === "POST")!;
    expect(JSON.parse(String(call[1]?.body))).toMatchObject({ expected_baseline_sha256: props.baselineSha256, expected_source_sha256: props.sourceSha256, source_item_ids: ["src-1"], goal: "核对图表与分类", origin: "USER_REQUEST" });
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "还有新的补充" } });
    resolve(finished);
    await waitFor(() => expect(screen.getByLabelText("任务或修改意见")).toHaveValue("还有新的补充"));
    expect(props.onTaskChange).toHaveBeenCalledWith(finished);
  });

  it("restores incomplete work from the server and resumes the same task", async () => {
    setup(finished);
    await screen.findByText("等待继续");
    expect(screen.queryByRole("button", { name: "暂停任务" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "继续核对原图" } });
    fireEvent.click(screen.getByRole("button", { name: "继续此任务" }));
    await waitFor(() => expect(vi.mocked(request).mock.calls.some(([path]) => String(path).endsWith("/task-1/resume"))).toBe(true));
    expect(props.onUpdated).not.toHaveBeenCalled();
  });

  it("refreshes saved analysis even when the source baseline SHA stays the same", async () => {
    setup({ ...finished, progress: { message: '模型的旧进度：将在本轮结束后保存。' }, result: { status: "COMPLETE", application_saved: true, baseline_revision_artifact: { baseline_artifact_sha256: props.baselineSha256, baseline_revision: 27 } } });
    await waitFor(() => expect(props.onUpdated).toHaveBeenCalledTimes(1));
    expect(screen.getByText('工作版已保存')).toBeInTheDocument();
    expect(screen.queryByText('模型的旧进度：将在本轮结束后保存。')).not.toBeInTheDocument();
  });

  it("keeps unsent drafts separate across source items and baselines", async () => {
    const view = setup();
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "只修改第一项" } });
    view.rerender(<SourceItemAgentLoop {...props} selectedSourceItemId="src-2" />);
    expect(screen.getByLabelText("任务或修改意见")).toHaveValue("");
    view.rerender(<SourceItemAgentLoop {...props} />);
    expect(screen.getByLabelText("任务或修改意见")).toHaveValue("只修改第一项");
    view.rerender(<SourceItemAgentLoop {...props} baselineSha256={"c".repeat(64)} />);
    expect(screen.getByLabelText("任务或修改意见")).toHaveValue("");
  });

  it("keeps feedback receipt visible beside ongoing progress", async () => {
    setup({ ...finished, job_status: "RUNNING", can_steer: true, can_interrupt: true, can_resume: false,
      progress: { message: "Agent 正在调用 reqpilot_read" }, result: undefined });
    await screen.findByLabelText("补充本轮意见");
    fireEvent.change(screen.getByLabelText("补充本轮意见"), { target: { value: "补充图像可读性核对" } });
    fireEvent.click(screen.getByRole("button", { name: "补充给 Agent" }));
    await screen.findByText("补充意见已保存，将进入当前任务。");
    expect(screen.getByText("正在查阅原文与成果")).toBeInTheDocument();
  });
  it("pauses the current task without submitting or clearing the unsent draft", async () => {
    setup({ ...finished, job_status: "RUNNING", can_steer: true, can_interrupt: true, can_resume: false, result: undefined });
    const input = await screen.findByLabelText("补充本轮意见");
    fireEvent.change(input, { target: { value: "尚未提交的核对意见" } });
    fireEvent.click(screen.getByRole("button", { name: "暂停任务" }));
    await screen.findByText("暂停请求已保存，已产生的成果会保留。");
    const writes = vi.mocked(request).mock.calls.filter(([, options]) => options?.method === "POST");
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toContain("/task-1/interrupt");
    const body = JSON.parse(String(writes[0][1]?.body));
    expect(body.command_id).toBeTruthy();
    expect(body.feedback).toBeUndefined();
    expect(input).toHaveValue("尚未提交的核对意见");
  });

  it("explains when unsent drafts only remain on this page", () => {
    setup();
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage unavailable"); });
    try {
      fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "仍能编辑" } });
      expect(screen.getByText("本机草稿暂存不可用，未发送文字仅保留在当前页面。")).toBeInTheDocument();
      expect(screen.getByLabelText("任务或修改意见")).toHaveValue("仍能编辑");
    } finally { spy.mockRestore(); }
  });

  it("discovers an external new task without mixing the previous task's events or cursor", async () => {
    vi.useFakeTimers();
    try {
      let latest = finished;
      vi.mocked(request).mockImplementation(async path => {
        const url = String(path);
        if (url.includes('/events?')) return { events: [{ message: url.includes('/task-2/') ? '第二项任务的动作' : '第一项任务的动作' }], next_cursor: 500 } as never;
        return latest as never;
      });
      await act(async () => { render(<SourceItemAgentLoop {...props} />); });
      expect(screen.getByText('第一项任务的动作')).toBeInTheDocument();
      latest = { ...finished, task_id: 'task-2', job_id: 'job-2' };
      await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
      expect(screen.queryByText('第一项任务的动作')).not.toBeInTheDocument();
      expect(screen.getByText('第二项任务的动作')).toBeInTheDocument();
      expect(vi.mocked(request).mock.calls.some(([path]) => String(path).endsWith('/task-2/events?after=0'))).toBe(true);
    } finally { vi.useRealTimers(); }
  });
});
