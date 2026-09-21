import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { SourceDocumentActions, startSourceDocumentGraph } from "./SourceDocumentActions";
import type { AgentLoopTask } from "./SourceItemAgentLoop";

const loopCallbacks = vi.hoisted(() => ({ onTaskChange: null as ((task: AgentLoopTask) => void) | null }));
vi.mock("../../shared/api/client", async importOriginal => ({ ...await importOriginal<object>(), request: vi.fn() }));
vi.mock("./SourceItemAgentLoop", () => ({ SourceItemAgentLoop: ({ onTaskChange }: { onTaskChange: (task: AgentLoopTask) => void }) => { loopCallbacks.onTaskChange = onTaskChange; return null; } }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("source document Agent entry", () => {
  it("starts an autonomous whole-file task before any Map exists", async () => {
    vi.mocked(request).mockResolvedValue({ task_id: "task", job_id: "job", job_status: "PENDING" });
    const job = await startSourceDocumentGraph("project", "document", "a".repeat(64), "");
    const [url, options] = vi.mocked(request).mock.calls[0];
    expect(url).toBe("/api/projects/project/sor-documents/document/agent-structure/delivery/extraction-loop");
    const body = JSON.parse(String(options?.body));
    expect(body.expected_source_sha256).toBe("a".repeat(64));
    expect(body.goal).toContain("完整阅读当前已上传的客户原文件");
    expect(body.goal).toContain("Matrix");
    expect(body).not.toHaveProperty("expected_baseline_sha256");
    expect(body).not.toHaveProperty("auto_source_items");
    expect(job).toMatchObject({ job_id: "job", status: "PENDING", kind: "agent.source-item.extract-loop" });
  });

  it("retains the explicitly selected existing-Map completion tool", async () => {
    vi.mocked(request).mockResolvedValue({ jobs: [] });
    await startSourceDocumentGraph("project", "document", "a".repeat(64), "map-current");
    const [url, options] = vi.mocked(request).mock.calls[0];
    expect(url).toContain("/requirement-baseline/all-runs/start");
    expect(JSON.parse(String(options?.body)).expected_artifact_id).toBe("map-current");
  });

  it("reports the active original-file task without letting an older Map job replace it", async () => {
    const changed = vi.fn();
    vi.mocked(request).mockImplementation(async path => String(path).endsWith("/latest")
      ? { job_id: "current-loop", job_status: "RUNNING", progress: { message: "正在核对原表" } } as never
      : { job_id: "old-map", status: "RUNNING", kind: "agent.document-map" } as never);
    render(<SourceDocumentActions projectId="project" documentId="document" sourceSha256={"a".repeat(64)} mapArtifactId="" onUpdated={vi.fn()} onActivityChange={changed} />);
    expect(screen.getByRole("button", { name: "正在同步任务…" })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Agent 处理中…" })).toBeDisabled());
    await waitFor(() => expect(changed).toHaveBeenLastCalledWith(expect.objectContaining({ documentId: "document", checking: false,
      jobs: [expect.objectContaining({ job_id: "current-loop", kind: "agent.source-item.extract-loop", status: "RUNNING" })] })));
    fireEvent.click(screen.getByRole("button", { name: "Agent 处理中…" }));
    expect(vi.mocked(request).mock.calls.every(([, options]) => options?.method !== "POST")).toBe(true);
  });

  it("discards a delayed former document's task when the document changes", async () => {
    let resolveOld: (value: unknown) => void = () => {};
    vi.mocked(request).mockImplementation(path => String(path).includes("/old/") && String(path).endsWith("/latest")
      ? new Promise(resolve => { resolveOld = resolve; }) as never : Promise.resolve(null) as never);
    const changed = vi.fn();
    const props = { projectId: "project", sourceSha256: "a".repeat(64), mapArtifactId: "", onUpdated: vi.fn(), onActivityChange: changed };
    const view = render(<SourceDocumentActions {...props} documentId="old" />);
    view.rerender(<SourceDocumentActions {...props} documentId="new" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Agent 提取与分析" })).toBeEnabled());
    await act(async () => resolveOld({ job_id: "old-loop", job_status: "RUNNING" }));
    expect(changed).toHaveBeenLastCalledWith(expect.objectContaining({ documentId: "new", jobs: [], checking: false }));
    expect(screen.getByRole("button", { name: "Agent 提取与分析" })).toBeEnabled();
  });

  it("immediately locks sibling entry points when collaboration starts, even before old discovery finishes", async () => {
    let resolveLegacy: (value: unknown) => void = () => {};
    vi.mocked(request).mockImplementation(path => String(path).endsWith("/status")
      ? new Promise(resolve => { resolveLegacy = resolve; }) as never : Promise.resolve(null) as never);
    const changed = vi.fn();
    render(<SourceDocumentActions projectId="project" documentId="document" sourceSha256={"a".repeat(64)} mapArtifactId="" onUpdated={vi.fn()} onActivityChange={changed} />);
    act(() => loopCallbacks.onTaskChange?.({ task_id: "new-task", job_id: "new-loop", job_status: "PENDING", can_steer: true, can_interrupt: true, can_resume: false }));
    expect(screen.getByRole("button", { name: "Agent 处理中…" })).toBeDisabled();
    await act(async () => resolveLegacy({ job_id: "old-map", status: "RUNNING" }));
    expect(changed).toHaveBeenLastCalledWith(expect.objectContaining({ jobs: [expect.objectContaining({ job_id: "new-loop", status: "PENDING" })] }));
  });
});
