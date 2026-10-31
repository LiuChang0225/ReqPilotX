import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ApplicationHostContext, type ApplicationTaskReceipt, type WorkbenchApplicationHost } from "./ApplicationHost";
import { HostedAgentPanel } from "./HostedAgentPanel";
import { HostLocaleProvider } from "./HostLocaleContext";

afterEach(() => { cleanup(); vi.useRealTimers(); });
const receipt: ApplicationTaskReceipt = { commandId: "command-one", projectId: "p", documentId: "d", sourceSha256: "a".repeat(64),
  taskId: "task-one", workRef: "plugin-" + "b".repeat(32), sessionId: "session-one", status: "ACCEPTED" };
function host(): WorkbenchApplicationHost {
  return { id: "host-one", label: "PAIR", modelLabel: "Configured task model", captureAvailable: false, taskStartAvailable: true,
    resourceUrl: path => path, pdfViewerUrl: source => source, startTask: vi.fn(async () => receipt),
    restoreTask: vi.fn(async () => null), taskDraft: () => undefined, controlTask: vi.fn(async () => receipt), openSession: vi.fn(async () => {}) };
}
function show(value: WorkbenchApplicationHost, workRef?: string) {
  const accepted = vi.fn();
  render(<ApplicationHostContext.Provider value={value}><HostedAgentPanel projectId="p" documentId="d" sourceSha256={receipt.sourceSha256}
    workRef={workRef} initialGoal="Keep the current document scope" onAccepted={accepted} onClose={() => {}} /></ApplicationHostContext.Provider>);
  return accepted;
}

it("retains the entered goal after an uncertain start and opens only the acknowledged work", async () => {
  const value = host();
  vi.mocked(value.startTask).mockRejectedValueOnce(new Error("acknowledgement unavailable"));
  const accepted = show(value);
  fireEvent.click(screen.getByRole("button", { name: "开始固定流程" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("acknowledgement unavailable");
  expect(screen.getByLabelText("补充说明（可选）")).toHaveValue("Keep the current document scope");
  expect(accepted).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "开始固定流程" }));
  await waitFor(() => expect(accepted).toHaveBeenCalledWith(receipt));
  expect(vi.mocked(value.startTask).mock.calls[0]).toEqual(vi.mocked(value.startTask).mock.calls[1]);
});

it("adopts confirmed reconnect capabilities and opens the exact persisted Pair Session", async () => {
  const value = host();
  vi.mocked(value.restoreTask).mockResolvedValue({ ...receipt, controlCapabilities: { reconnect: true } });
  vi.mocked(value.controlTask).mockResolvedValue({ ...receipt, controlCapabilities: { continue: true } });
  show(value, receipt.workRef);
  fireEvent.click(await screen.findByRole("button", { name: "核对并重连原任务" }));
  expect(await screen.findByRole("button", { name: "继续同一任务" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "核对并重连原任务" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "打开 PAIR 会话" }));
  await waitFor(() => expect(value.openSession).toHaveBeenCalledWith("session-one"));
  expect(value.controlTask).toHaveBeenCalledWith(expect.objectContaining({ commandId: "command-one", workRef: receipt.workRef }), "reconnect");
});
it("prepares the selected source discussion with the exact task identity and never sends it", async () => {
  const value = host();
  vi.mocked(value.restoreTask).mockResolvedValue(receipt);
  const accepted = vi.fn();
  render(<ApplicationHostContext.Provider value={value}><HostedAgentPanel projectId="p" documentId="d" sourceSha256={receipt.sourceSha256}
    workRef={receipt.workRef} initialGoal="Keep this goal" onAccepted={accepted} onClose={() => {}}
    binding={{ sourceSha256: receipt.sourceSha256, baselineSha256: "baseline", selectedSourceItemId: "SRC-0001",
      selectedNodeId: "node-one", selectedDerivedUnitId: "unit-one", revisionId: "c".repeat(64), onUpdated: () => {} }} /></ApplicationHostContext.Provider>);
  fireEvent.click(await screen.findByRole("button", { name: "在会话中讨论当前成果" }));
  await waitFor(() => expect(value.openSession).toHaveBeenCalledWith("session-one", {
    projectId: "p", documentId: "d", sourceSha256: receipt.sourceSha256, workRef: receipt.workRef,
    revisionId: "c".repeat(64), taskId: "task-one", scope: "selected", nodeId: "node-one",
    sourceItemId: "SRC-0001", derivedUnitId: "unit-one" }));
  expect(accepted).not.toHaveBeenCalled();
});
it("restores a pending task's original goal without submitting it", async () => {
  const value = host(); value.taskDraft = () => ({ goal: "Original durable task goal", requireMapConfirmation: true });
  show(value);
  await waitFor(() => expect(screen.getByLabelText("补充说明（可选）")).toHaveValue("Original durable task goal"));
  expect(screen.getByLabelText("先核对结构，再继续整理条目")).toBeChecked();
  expect(value.startTask).not.toHaveBeenCalled();
});

function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (cause: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const other: ApplicationTaskReceipt = { ...receipt, commandId: "command-two", workRef: "plugin-" + "c".repeat(32), sessionId: "session-two" };
function panel(value: WorkbenchApplicationHost, accepted: (value: ApplicationTaskReceipt) => void, workRef?: string, selectedRevisionId?: string) {
  return <ApplicationHostContext.Provider value={value}><HostedAgentPanel projectId="p" documentId="d" sourceSha256={receipt.sourceSha256}
    workRef={workRef} revisionId="displayed-version" selectedRevisionId={selectedRevisionId} initialGoal="Keep this goal" onAccepted={accepted} onClose={() => {}} /></ApplicationHostContext.Provider>;
}

it.each(["resolve", "reject"])("ignores a late %s from another work and keeps only the current polling loop", async completion => {
  vi.useFakeTimers(); const value = host(), accepted = vi.fn(), old = deferred<ApplicationTaskReceipt | null>();
  vi.mocked(value.restoreTask).mockImplementation(async input => input.workRef === receipt.workRef ? old.promise : other);
  const view = render(panel(value, accepted, receipt.workRef));
  view.rerender(panel(value, accepted, other.workRef));
  await act(async () => {});
  expect(vi.mocked(value.restoreTask).mock.calls[0][1]?.signal?.aborted).toBe(true);
  await act(async () => { if (completion === "resolve") old.resolve(receipt); else old.reject(Error("old work error")); });
  expect(screen.queryByText("old work error")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "打开 PAIR 会话" }));
  expect(value.openSession).toHaveBeenCalledWith(other.sessionId);
  expect(vi.getTimerCount()).toBe(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(vi.mocked(value.restoreTask).mock.calls.map(([input]) => input.workRef)).toEqual([receipt.workRef, other.workRef, other.workRef]);
  expect(accepted).not.toHaveBeenCalled();
});

it("does not reactivate an old request when the user returns to the same work", async () => {
  vi.useFakeTimers(); const value = host(), old = deferred<ApplicationTaskReceipt | null>();
  vi.mocked(value.restoreTask).mockReturnValueOnce(old.promise).mockResolvedValue({ ...receipt, controlCapabilities: { cancel: true } });
  const view = render(panel(value, vi.fn(), receipt.workRef));
  view.rerender(panel(value, vi.fn(), other.workRef)); await act(async () => {});
  view.rerender(panel(value, vi.fn(), receipt.workRef)); await act(async () => {});
  await act(async () => { old.resolve({ ...receipt, controlCapabilities: { reconnect: true } }); });
  expect(screen.getByRole("button", { name: "停止本轮" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "核对并重连原任务" })).not.toBeInTheDocument();
  expect(vi.getTimerCount()).toBe(1);
});

it.each(["resolve", "reject"])("keeps a late control %s attached to the work that dispatched it", async completion => {
  vi.useFakeTimers(); const value = host(), accepted = vi.fn(), a = deferred<ApplicationTaskReceipt>(), b = deferred<ApplicationTaskReceipt>();
  vi.mocked(value.restoreTask).mockImplementation(async input => ({ ...(input.workRef === receipt.workRef ? receipt : other), controlCapabilities: { continue: true } }));
  vi.mocked(value.controlTask).mockImplementation(async current => current.workRef === receipt.workRef ? a.promise : b.promise);
  const view = render(panel(value, accepted, receipt.workRef)); await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "继续同一任务" }));
  view.rerender(panel(value, accepted, other.workRef)); await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "继续同一任务" }));
  await act(async () => { if (completion === "resolve") a.resolve({ ...receipt, controlCapabilities: { reconnect: true } }); else a.reject(Error("old control failed")); });
  expect(screen.getByRole("button", { name: "继续同一任务" })).toBeDisabled();
  expect(screen.queryByText("old control failed")).not.toBeInTheDocument();
  expect(accepted).not.toHaveBeenCalled();
  expect(vi.mocked(value.controlTask).mock.calls.map(([current]) => current.workRef)).toEqual([receipt.workRef, other.workRef]);
  await act(async () => { b.resolve({ ...other, controlCapabilities: { cancel: true } }); });
  expect(screen.getByRole("button", { name: "停止本轮" })).toBeEnabled();
  expect(accepted).not.toHaveBeenCalled();
});

it.each(["selected-version-two", undefined])("invalidates a restore at revision selection %s before the reader updates", async selected => {
  vi.useFakeTimers(); const value = host(), pending = deferred<ApplicationTaskReceipt | null>(), accepted = vi.fn();
  vi.mocked(value.restoreTask).mockReturnValueOnce(pending.promise).mockResolvedValue({ ...receipt, controlCapabilities: { continue: true } });
  const view = render(panel(value, accepted, receipt.workRef, "selected-version-one"));
  view.rerender(panel(value, accepted, receipt.workRef, selected)); await act(async () => {});
  await act(async () => { pending.resolve({ ...receipt, controlCapabilities: { reconnect: true } }); });
  expect(vi.mocked(value.restoreTask).mock.calls[0][1]?.signal?.aborted).toBe(true);
  expect(screen.getByRole("button", { name: "继续同一任务" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "核对并重连原任务" })).not.toBeInTheDocument();
  expect(vi.getTimerCount()).toBe(1); expect(accepted).not.toHaveBeenCalled();
});

it.each(["selected-version-two", undefined])("retains revision selection %s while an earlier control completes", async selected => {
  vi.useFakeTimers(); const value = host(), accepted = vi.fn(), pending = deferred<ApplicationTaskReceipt>();
  vi.mocked(value.restoreTask).mockResolvedValue({ ...receipt, controlCapabilities: { continue: true } });
  vi.mocked(value.controlTask).mockReturnValue(pending.promise);
  const view = render(panel(value, accepted, receipt.workRef, "selected-version-one")); await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "继续同一任务" }));
  view.rerender(panel(value, accepted, receipt.workRef, selected)); await act(async () => {});
  await act(async () => { pending.resolve({ ...receipt, controlCapabilities: { reconnect: true } }); });
  expect(accepted).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "继续同一任务" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "核对并重连原任务" })).not.toBeInTheDocument();
  expect(vi.getTimerCount()).toBe(1);
});

it("does not navigate to a newly accepted task after the user selects a different saved work", async () => {
  vi.useFakeTimers(); const value = host(), accepted = vi.fn(), pending = deferred<ApplicationTaskReceipt>();
  vi.mocked(value.startTask).mockReturnValue(pending.promise);
  vi.mocked(value.restoreTask).mockImplementation(async input => input.workRef ? other : null);
  const view = render(panel(value, accepted)); await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "开始固定流程" }));
  view.rerender(panel(value, accepted, other.workRef)); await act(async () => {});
  await act(async () => { pending.resolve(receipt); });
  expect(accepted).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "打开 PAIR 会话" }));
  expect(value.openSession).toHaveBeenCalledWith(other.sessionId);
});

it("does not overwrite a control acknowledgement with an older restore in the same selection", async () => {
  vi.useFakeTimers(); const value = host(), accepted = vi.fn(), pending = deferred<ApplicationTaskReceipt | null>();
  vi.mocked(value.restoreTask).mockResolvedValueOnce({ ...receipt, controlCapabilities: { continue: true } }).mockReturnValue(pending.promise);
  vi.mocked(value.controlTask).mockResolvedValue({ ...receipt, controlCapabilities: { cancel: true } });
  render(panel(value, accepted, receipt.workRef)); await act(async () => {});
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  fireEvent.click(screen.getByRole("button", { name: "继续同一任务" })); await act(async () => {});
  expect(vi.mocked(value.restoreTask).mock.calls[1][1]?.signal?.aborted).toBe(true);
  await act(async () => { pending.resolve({ ...receipt, controlCapabilities: { reconnect: true } }); });
  expect(screen.getByRole("button", { name: "停止本轮" })).toBeVisible();
  expect(accepted).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(1);
});

it("shows Not started without an execution error when no task exists", async () => {
  const value = host();
  show(value);
  expect(await screen.findByText("尚未开始")).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "开始固定流程" })).toBeEnabled();
});

it("shows saved business stages separately from execution until review", async () => {
  const value = host();
  vi.mocked(value.restoreTask).mockResolvedValue({ ...receipt, status: "ACCEPTED", execution: { status: "RUNNING" },
    workflowState: { schema: "reqpilot-fixed-workflow-state/v1",
      workflow: { id: "reqpilot-fixed-workflow", version: "1.0.0" }, source_sha256: receipt.sourceSha256,
      stages: [
        { id: "PREPARATION", label: "资料准备", status: "SAVED" },
        { id: "SOURCE_MAP", label: "来源与结构", status: "SAVED" },
        { id: "ITEMS_CLASSIFICATION", label: "条目、分类与明确派生", status: "BLOCKED", reason: "ITEMS_NOT_SAVED" },
      ], next_stage: "ITEMS_CLASSIFICATION", ready_to_review: false, verification: "SAVED_ARTIFACTS_ONLY",
      open_items: [], human_acceptance: "NOT_CLAIMED" } });
  show(value, receipt.workRef);
  expect(await screen.findByRole("region", { name: "固定流程成果" })).toHaveTextContent("来源与结构");
  expect(screen.getByText("等待前置阶段")).toBeVisible();
  expect(screen.getByText(/运行状态：正在处理/)).toBeVisible();
  expect(screen.queryByText(/已保存成果满足/)).not.toBeInTheDocument();
});

it("shows an actionable unconfirmed workflow instead of a forged ready state", async () => {
  const value = host();
  vi.mocked(value.restoreTask).mockResolvedValue({ ...receipt, status: "ACCEPTED", workflowState: null,
    workflowUnconfirmed: { reason: "TASK_WORKFLOW_UNCONFIRMED", message: "workflow state failed source/stage/evidence validation" } });
  show(value, receipt.workRef);
  expect(await screen.findByText(/固定流程状态未确认/)).toBeVisible();
  expect(screen.queryByText(/已保存成果满足/)).not.toBeInTheDocument();
});


it("rerenders English through the same bound t identity while retaining draft, scope and revision selection", async () => {
  const value = host(); const accepted = vi.fn(); let language: "zh" | "en" = "zh";
  const en: Record<string, string> = {
    "intake.title": "Organize this customer document with the fixed workflow",
    "intake.note": "{model}. Start once; optional notes cannot remove fixed stages or output checks.",
    "intake.notesLabel": "Supplementary instructions (optional)",
    "intake.notesPlaceholder": "Optional focus for this run.",
    "intake.confirmMap": "Check the structure before items", "intake.start": "Start fixed workflow",
    "intake.notStarted": "Not started",
  };
  const zh: Record<string, string> = {
    "intake.title": "按固定流程整理当前客户文档", "intake.note": "{model}。用户只需开始一次；可选补充说明不会取消固定阶段或输出检查。",
    "intake.notesLabel": "补充说明（可选）", "intake.notesPlaceholder": "本次可选关注点。",
    "intake.confirmMap": "先核对结构，再继续整理条目", "intake.start": "开始固定流程", "intake.notStarted": "尚未开始",
  };
  const t = (key: string) => (language === "en" ? en : zh)[key] ?? key;
  const element = () => <HostLocaleProvider t={t}><ApplicationHostContext.Provider value={value}>
    <HostedAgentPanel projectId="p" documentId="d" sourceSha256={receipt.sourceSha256} revisionId="displayed-version"
      selectedRevisionId="selected-version-one" initialGoal="Retained draft" onAccepted={accepted} onClose={() => {}} />
  </ApplicationHostContext.Provider></HostLocaleProvider>;
  const view = render(element());
  const input = await screen.findByLabelText("补充说明（可选）");
  fireEvent.change(input, { target: { value: "当前草稿必须保留" } });
  const restores = vi.mocked(value.restoreTask).mock.calls.length;
  language = "en";
  view.rerender(element());
  await waitFor(() => expect(screen.getByLabelText("Supplementary instructions (optional)")).toHaveValue("当前草稿必须保留"));
  expect(screen.getByRole("button", { name: "Start fixed workflow" })).toBeVisible();
  expect(screen.queryByText("开始固定流程")).not.toBeInTheDocument();
  expect(vi.mocked(value.restoreTask).mock.calls.length).toBe(restores);
  expect(vi.mocked(value.restoreTask).mock.calls[0][0]).toMatchObject({ projectId: "p", documentId: "d", sourceSha256: receipt.sourceSha256 });
});

it.each([
  ["ACCEPTED", "Accepted"], ["RUNNING", "Processing"], ["WAITING", "Waiting"], ["SUCCEEDED", "This turn has ended"],
  ["FAILED", "This turn did not complete"], ["INTERRUPTED", "Paused"], ["IDLE", "Idle"], ["UNKNOWN", "Status unconfirmed"],
])("routes displayed execution state %s through the host language binding", async (status, label) => {
  const value = host();
  vi.mocked(value.restoreTask).mockResolvedValue({ ...receipt, status: "ACCEPTED", execution: { status }, controlCapabilities: {} });
  const english: Record<string, string> = {
    "execution.ACCEPTED": "Accepted", "execution.RUNNING": "Processing", "execution.WAITING": "Waiting",
    "execution.SUCCEEDED": "This turn has ended", "execution.FAILED": "This turn did not complete",
    "execution.INTERRUPTED": "Paused", "execution.IDLE": "Idle", "execution.UNKNOWN": "Status unconfirmed",
    "session.heading": "PAIR session", "session.bound": "Original task linked · {status}",
    "session.runAndBusiness": "Runtime: {status}; business results remain governed by the saved stages above.",
    "control.openSession": "Open PAIR session",
  };
  const t = (key: string) => english[key] ?? key;
  render(<HostLocaleProvider t={t}><ApplicationHostContext.Provider value={value}>
    <HostedAgentPanel projectId="p" documentId="d" sourceSha256={receipt.sourceSha256} workRef={receipt.workRef}
      initialGoal="Keep this goal" onAccepted={vi.fn()} onClose={() => {}} />
  </ApplicationHostContext.Provider></HostLocaleProvider>);
  expect((await screen.findAllByText(new RegExp(label))).length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "Open PAIR session" })).toBeVisible();
});
