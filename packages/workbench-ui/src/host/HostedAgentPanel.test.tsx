import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ApplicationHostContext, type ApplicationTaskReceipt, type WorkbenchApplicationHost } from "./ApplicationHost";
import { HostedAgentPanel } from "./HostedAgentPanel";

afterEach(cleanup);
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
