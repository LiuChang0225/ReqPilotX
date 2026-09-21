import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DocumentAgentContext, type DocumentAgentContextValue } from "./DocumentAgentContext";
import { DocumentTaskHome } from "./DocumentTaskHome";
afterEach(cleanup);
function mount(task: DocumentAgentContextValue["task"]) {
  const value = { bind: vi.fn(), task, show: vi.fn(), openStage: vi.fn(), historyHost: null };
  render(<DocumentAgentContext.Provider value={value}><DocumentTaskHome fileName="source.pdf" sourceFileUrl="/api/source.pdf" /></DocumentAgentContext.Provider>);
  return value;
}
it("offers a concrete task entry for a saved source with no task", () => {
  const value = mount(null);
  fireEvent.click(screen.getByRole("button", { name: "编辑任务目标" }));
  expect(value.show).toHaveBeenCalled();
  expect(screen.getByRole("link", { name: "打开原文件" })).toHaveAttribute("href", "/api/source.pdf");
});
it.each(["FAILED", "CANCELLED", "SUCCEEDED"])("does not display stale running progress after %s", status => {
  mount({ task_id: "t", job_id: "j", job_status: status, can_steer: false, can_resume: false, can_interrupt: false, progress: { message: "正在执行上一轮任务" } });
  expect(screen.queryByText("正在执行上一轮任务")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "查看任务与补充意见" })).toBeEnabled();
});
it("continues to display the real progress for an active task", () => {
  mount({ task_id: "t", job_id: "j", job_status: "RUNNING", can_steer: true, can_resume: false, can_interrupt: true, progress: { message: "正在查阅第 4 页" } });
  expect(screen.getByRole("status")).toHaveTextContent("正在查阅第 4 页");
});
