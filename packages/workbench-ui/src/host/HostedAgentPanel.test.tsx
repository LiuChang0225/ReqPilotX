import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ApplicationHostContext, type ApplicationTaskReceipt, type WorkbenchApplicationHost } from "./ApplicationHost";
import { HostedAgentPanel } from "./HostedAgentPanel";

afterEach(() => { cleanup(); vi.useRealTimers(); });
const receipt: ApplicationTaskReceipt = { commandId: "command-one", projectId: "p", documentId: "d", sourceSha256: "a".repeat(64),
  workRef: "plugin-" + "b".repeat(32), sessionId: "session-one", status: "ACCEPTED" };
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
  fireEvent.click(screen.getByRole("button", { name: "交给 PAIR Agent" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("acknowledgement unavailable");
  expect(screen.getByLabelText("PAIR 任务目标")).toHaveValue("Keep the current document scope");
  expect(accepted).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "交给 PAIR Agent" }));
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
  await waitFor(() => expect(value.openSession).toHaveBeenCalledWith("session-one", expect.objectContaining({ projectId: "p", documentId: "d", workRef: receipt.workRef, scope: "document" })));
  expect(value.controlTask).toHaveBeenCalledWith(expect.objectContaining({ commandId: "command-one", workRef: receipt.workRef }), "reconnect");
});
it("restores a pending task's original goal without submitting it", async () => {
  const value = host(); value.taskDraft = () => ({ goal: "Original durable task goal", requireMapConfirmation: true });
  show(value);
  await waitFor(() => expect(screen.getByLabelText("PAIR 任务目标")).toHaveValue("Original durable task goal"));
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
  expect(value.openSession).toHaveBeenCalledWith(other.sessionId, expect.objectContaining({ workRef: other.workRef }));
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
  fireEvent.click(screen.getByRole("button", { name: "交给 PAIR Agent" }));
  view.rerender(panel(value, accepted, other.workRef)); await act(async () => {});
  await act(async () => { pending.resolve(receipt); });
  expect(accepted).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "打开 PAIR 会话" }));
  expect(value.openSession).toHaveBeenCalledWith(other.sessionId, expect.objectContaining({ workRef: other.workRef }));
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
