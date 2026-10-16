import { useEffect } from "react";
import { useDocumentAgent } from "./DocumentAgentContext";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DocumentWorkSurface } from "./DocumentWorkSurface";
import type { AgentLoopTask } from "../extraction/SourceItemAgentLoop";
import type { SourceStage } from "../extraction/SourceDocumentStageResults";
import type { WorkspaceRoute } from "./route";
let emitTask: (task: AgentLoopTask) => void;
let agentProps: { selectedSourceItemId: string; onUpdated: () => void; onOpenStage: (stage: SourceStage) => void; requestedScope?: { id: number; scope: "selected" | "document" } };
let suppliedSummary: unknown = { delivery: null, task: null, deliveryError: "", taskError: "" };
const harness = vi.hoisted(() => ({ route: { task: "extraction", mode: "organize", checkpoint: "structure", projectId: "p", documentId: "d", requirementSetId: "" } as WorkspaceRoute,
  updateRoute: vi.fn(), source: "source" }));
vi.mock("./WorkspaceContext", () => ({ useWorkspace: () => ({ route: harness.route,
  updateRoute: (patch: Partial<WorkspaceRoute>) => { harness.updateRoute(patch); harness.route = { ...harness.route, ...patch }; },
  selectedDocument: { source_sha256: harness.source, file_name: "原件.pdf" }, refreshDocument: vi.fn() }) }));
vi.mock("./documentWorkSummary", async original => ({ ...await original<object>(), useDocumentWorkSummary: () => suppliedSummary }));
vi.mock("../extraction/SourceItemAgentLoop", () => ({ SourceItemAgentLoop: (props: { onTaskChange: typeof emitTask; selectedSourceItemId: string; onUpdated: () => void; onOpenStage: (stage: SourceStage) => void }) => { emitTask = props.onTaskChange; agentProps = props; return <div>任务控制器<input aria-label="协作草稿" /></div>; } }));
vi.mock("../extraction/SourceDocumentStageResults", () => ({ SourceDocumentStageResults: (props: { taskId: string; sourceSha256: string; stages: SourceStage[]; confirmationRequired: boolean }) => <div data-testid="stage" data-task={props.taskId} data-source={props.sourceSha256} data-stage={props.stages[0].stage} data-hash={props.stages[0].artifact_sha256} data-confirm={String(props.confirmationRequired)}>阶段正文</div> }));
afterEach(() => { cleanup(); suppliedSummary = { delivery: null, task: null, deliveryError: "", taskError: "" }; harness.route = { task: "extraction", mode: "organize", checkpoint: "structure", projectId: "p", documentId: "d", requirementSetId: "" }; harness.source = "source"; harness.updateRoute.mockClear(); localStorage.clear(); });
it("keeps an open stage pinned when new artifacts arrive and invalidates its approval", async () => {
  render(<DocumentWorkSurface><div>已有成果</div></DocumentWorkSurface>);
  const task: AgentLoopTask = { task_id: "task", job_id: "job", source_sha256: "source", job_status: "SUCCEEDED", can_steer: false, can_resume: true, can_interrupt: false, map_confirmation_required: true,
    stages: [{ stage: "MAP", artifact_sha256: "first", count: 3 }] };
  act(() => emitTask(task));
  expect(screen.getByTestId("stage")).toHaveAttribute("data-hash", "first");
  act(() => emitTask({ ...task, stages: [{ stage: "MAP", artifact_sha256: "second", count: 4 }] }));
  expect(screen.getByTestId("stage")).toHaveAttribute("data-hash", "first");
  expect(screen.getByTestId("stage")).toHaveAttribute("data-confirm", "false");
  fireEvent.click(screen.getByRole("button", { name: "查看最新成果" }));
  expect(screen.getByTestId("stage")).toHaveAttribute("data-hash", "second");
});

it("waits for the saved ledger lookup before choosing an automatic stage landing", () => {
  suppliedSummary = null;
  const view = render(<DocumentWorkSurface><div>已有工作版正文</div></DocumentWorkSurface>);
  act(() => emitTask({ task_id: "task", job_id: "job", source_sha256: "source", job_status: "SUCCEEDED", can_steer: false, can_resume: true, can_interrupt: false,
    stages: [{ stage: "MAP", artifact_sha256: "old-map", count: 3 }] }));
  expect(screen.queryByTestId("stage")).not.toBeInTheDocument();
  suppliedSummary = { delivery: { baseline_sha256: "saved", baseline_revision: 14 }, task: null, deliveryError: "", taskError: "" };
  view.rerender(<DocumentWorkSurface><div>已有工作版正文</div></DocumentWorkSurface>);
  expect(screen.queryByTestId("stage")).not.toBeInTheDocument();
  expect(screen.getByText("已有工作版正文")).toBeVisible();
});
it("releases the previous item's scope and refresh callback after leaving its view", () => {
  const refreshSource = vi.fn();
  function SourceBinding() {
    const workspace = useDocumentAgent()!;
    useEffect(() => workspace.bind({ sourceSha256: "source", baselineSha256: "basis", selectedSourceItemId: "src-1", onUpdated: refreshSource }), [workspace.bind]);
    return <div>原文条目</div>;
  }
  const view = render(<DocumentWorkSurface><SourceBinding /></DocumentWorkSurface>);
  expect(agentProps.selectedSourceItemId).toBe("src-1");
  view.rerender(<DocumentWorkSurface><div>导出工作版</div></DocumentWorkSurface>);
  expect(agentProps.selectedSourceItemId).toBe("");
  act(() => agentProps.onUpdated());
  expect(refreshSource).not.toHaveBeenCalled();
});

it("forwards every explicit item feedback scope to the native controller", () => {
  function FeedbackEntry() {
    const workspace = useDocumentAgent()!;
    useEffect(() => workspace.bind({ sourceSha256: "source", baselineSha256: "basis", selectedSourceItemId: "src-1", onUpdated: vi.fn() }), [workspace.bind]);
    return <><button onClick={() => workspace.show("复核全文", "document")}>全文反馈</button><button onClick={() => workspace.show("核对此条", "selected")}>条目反馈</button></>;
  }
  render(<DocumentWorkSurface><FeedbackEntry /></DocumentWorkSurface>);
  fireEvent.click(screen.getByText("全文反馈"));
  expect(agentProps.requestedScope?.scope).toBe("document");
  fireEvent.click(screen.getByText("条目反馈"));
  expect(agentProps.requestedScope).toEqual({ id: 2, scope: "selected" });
  expect(agentProps.selectedSourceItemId).toBe("src-1");
});

const savedMap: SourceStage = { stage: "MAP", artifact_sha256: "saved-map", count: 3 };
const savedItems: SourceStage = { stage: "ITEMS", artifact_sha256: "saved-items", count: 2 };
function supplyWorkingTask() {
  const task: AgentLoopTask = { task_id: "task", job_id: "job", source_sha256: "source", job_status: "SUCCEEDED",
    can_steer: false, can_resume: false, can_interrupt: false, stages: [savedMap, savedItems] };
  suppliedSummary = { delivery: { baseline_sha256: "working", baseline_revision: 14 }, task, deliveryError: "", taskError: "" };
}

it.each(["analysis", "export"] as const)("shows the destination business view after leaving a native stage for %s without remounting collaboration", (task) => {
  supplyWorkingTask();
  const view = render(<DocumentWorkSurface><div data-testid="business">extraction</div></DocumentWorkSurface>);
  fireEvent.click(screen.getByRole("button", { name: "Agent 协作" }));
  const composer = screen.getByRole("textbox", { name: "协作草稿" });
  fireEvent.change(composer, { target: { value: "尚未发送的修改意见" } });
  act(() => agentProps.onOpenStage(savedMap));
  expect(screen.getByTestId("stage")).toBeVisible();
  harness.route = { ...harness.route, task, checkpoint: undefined };
  view.rerender(<DocumentWorkSurface><div data-testid="business">{task}</div></DocumentWorkSurface>);
  expect(screen.queryByTestId("stage")).not.toBeInTheDocument();
  expect(screen.getByTestId("business")).toBeVisible();
  expect(screen.getByLabelText("协作草稿")).toBe(composer);
  expect(composer).toHaveValue("尚未发送的修改意见");
  harness.route = { ...harness.route, task: "extraction", checkpoint: "structure" };
  view.rerender(<DocumentWorkSurface><div data-testid="business">structure</div></DocumentWorkSurface>);
  expect(screen.queryByTestId("stage")).not.toBeInTheDocument();
  expect(screen.getByTestId("business")).toBeVisible();
  act(() => agentProps.onOpenStage(savedMap));
  expect(screen.getByTestId("stage")).toHaveAttribute("data-hash", "saved-map");
});

it.each([[savedMap, "list"], [savedMap, "diagnose"], [savedItems, "structure"]] as const)("closes %s preview when selecting extraction checkpoint %s", (savedStage, checkpoint) => {
  supplyWorkingTask();
  const view = render(<DocumentWorkSurface><div data-testid="business">当前工作版</div></DocumentWorkSurface>);
  act(() => agentProps.onOpenStage(savedStage));
  expect(screen.getByTestId("stage")).toHaveAttribute("data-hash", savedStage.artifact_sha256);
  harness.route = { ...harness.route, checkpoint };
  view.rerender(<DocumentWorkSurface><div data-testid="business">{checkpoint}</div></DocumentWorkSurface>);
  expect(screen.queryByTestId("stage")).not.toBeInTheDocument();
  expect(screen.getByTestId("business")).toBeVisible();
});

it("routes an initial automatic MAP landing to the explicit structure checkpoint", () => {
  harness.route = { ...harness.route, checkpoint: "diagnose" };
  render(<DocumentWorkSurface><div>初始文档页</div></DocumentWorkSurface>);
  act(() => emitTask({ task_id: "task", job_id: "job", source_sha256: "source", job_status: "SUCCEEDED",
    can_steer: false, can_resume: true, can_interrupt: false, stages: [savedMap] }));
  expect(harness.route.checkpoint).toBe("structure");
  expect(screen.getByTestId("stage")).toHaveAttribute("data-hash", "saved-map");
});

it.each([["analysis", savedMap, "structure"], ["export", savedItems, "list"]] as const)("opens a saved stage from %s at its extraction checkpoint", (task, savedStage, checkpoint) => {
  supplyWorkingTask();
  harness.route = { ...harness.route, task, checkpoint: undefined };
  render(<DocumentWorkSurface><div data-testid="business">原业务页</div></DocumentWorkSurface>);
  act(() => agentProps.onOpenStage(savedStage));
  expect(harness.updateRoute).toHaveBeenCalledWith({ task: "extraction", mode: "organize", checkpoint });
  expect(harness.route).toMatchObject({ task: "extraction", checkpoint });
  expect(screen.getByTestId("stage")).toHaveAttribute("data-task", "task");
  expect(screen.getByTestId("stage")).toHaveAttribute("data-source", "source");
  expect(screen.getByTestId("stage")).toHaveAttribute("data-hash", savedStage.artifact_sha256);
});

it.each(["analysis", "export"] as const)("does not automatically cover %s with a stage when no working ledger exists", (task) => {
  harness.route = { ...harness.route, task, checkpoint: undefined };
  render(<DocumentWorkSurface><div data-testid="business">尚无工作版</div></DocumentWorkSurface>);
  act(() => emitTask({ task_id: "task", job_id: "job", source_sha256: "source", job_status: "SUCCEEDED",
    can_steer: false, can_resume: true, can_interrupt: false, stages: [savedMap] }));
  expect(screen.queryByTestId("stage")).not.toBeInTheDocument();
  expect(screen.getByTestId("business")).toBeVisible();
});

it("never carries a viewed stage into a replaced source", () => {
  supplyWorkingTask();
  const view = render(<DocumentWorkSurface><div data-testid="business">原文</div></DocumentWorkSurface>);
  act(() => agentProps.onOpenStage(savedMap));
  harness.source = "replacement-source";
  suppliedSummary = { delivery: { baseline_sha256: "replacement", baseline_revision: 1 }, task: null, deliveryError: "", taskError: "" };
  view.rerender(<DocumentWorkSurface><div data-testid="business">新来源</div></DocumentWorkSurface>);
  expect(screen.queryByTestId("stage")).not.toBeInTheDocument();
  expect(screen.getByTestId("business")).toBeVisible();
});
