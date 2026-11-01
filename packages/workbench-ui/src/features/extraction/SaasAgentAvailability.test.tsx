import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { setSaasAccountIdentity } from "../../shared/api/saasSession";
import { SourceItemAgentLoop, type AgentLoopTask } from "./SourceItemAgentLoop";
import { SourceDocumentActions, startSourceDocumentGraph } from "./SourceDocumentActions";
import { SAAS_KIT_UNAVAILABLE_MESSAGE } from "./saasAgentAvailability";

vi.mock("../../shared/api/client", async original => ({ ...await original<object>(), request: vi.fn() }));
const props = { projectId: "owner-project", documentId: "owner-document", sourceSha256: "a".repeat(64),
  baselineSha256: "", selectedSourceItemId: "", onUpdated: vi.fn(), mode: "extract" as const, embedded: true };
const configured = { schema_version: "agent-runtime-readiness/v1", ready: true, configured: true, runtime_id: "provider-tool-loop", provider_id: "aliyun", model_name: "fixture-only", readiness_scope: "configuration", authentication_verified: false };
const coreReady = { schema_version: "agent-workflow-readiness/v1", ready: true, runtime_id: "provider-tool-loop", provider_id: "aliyun", model_name: "fixture-only", readiness_scope: "configuration_and_core", authentication_verified: false,
  checks: Object.fromEntries(["postgresql", "object_store", "durable_worker", "provider_configuration", "customer_data_authorization", "source_reader"].map(name => [name, { ready: true }])) };
const task: AgentLoopTask = { task_id: "saved-task", job_id: "saved-job", job_status: "SUCCEEDED", can_resume: true, can_interrupt: false, can_steer: false };
function setup(readiness: unknown, existing: AgentLoopTask | null = null) {
  setSaasAccountIdentity("account-a");
  vi.mocked(request).mockImplementation(async path => String(path).endsWith("/agent-readiness") ? readiness as never
    : String(path).endsWith("/agent-workflow-readiness") ? coreReady as never
    : String(path).includes("/events?") ? { events: [], next_cursor: 0 } as never : existing as never);
  return render(<SourceItemAgentLoop {...props} />);
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); setSaasAccountIdentity(undefined); localStorage.clear(); });

describe("SaaS visible native Agent entry", () => {
  it.each([null, { ready: true }, { ...configured, runtime_id: "codex-app-server" }, { ...configured, authentication_verified: true }, { ...configured, configured: false }, { ...configured, provider_id: "unknown" }, { ...configured, model_name: "" }])("fails closed for absent or incompatible preflight %j", async readiness => {
    setup(readiness, task);
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "完整处理这份文档" } });
    await screen.findByText(/模型尚未配置或当前 Agent 运行时不可用/);
    expect(screen.getByRole("button", { name: "开始新任务" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "继续此任务" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "开始新任务" }));
    fireEvent.click(screen.getByRole("button", { name: "继续此任务" }));
    expect(vi.mocked(request).mock.calls.some(([, opts]) => opts?.method === "POST")).toBe(false);
  });

  it("starts through the exact HTTP extraction loop exercised by the real PG SDK acceptance", async () => {
    setup(configured);
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "完整处理这份文档" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "开始新任务" })).toBeEnabled());
    expect(screen.getByText(/在线认证、权限与可用性尚未核验/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开始新任务" }));
    await waitFor(() => expect(vi.mocked(request).mock.calls.some(([, opts]) => opts?.method === "POST")).toBe(true));
    const [path, opts] = vi.mocked(request).mock.calls.find(([, opts]) => opts?.method === "POST")!;
    expect(path).toBe("/api/projects/owner-project/sor-documents/owner-document/agent-structure/delivery/extraction-loop");
    expect(JSON.parse(String(opts?.body))).toMatchObject({ expected_source_sha256: props.sourceSha256, origin: "USER_REQUEST", goal: "完整处理这份文档" });
    expect(vi.mocked(request).mock.calls.some(([path]) => String(path).includes("baseline/all-runs"))).toBe(false);
  });

  it("blocks while refresh is pending or fails and preserves unsent text", async () => {
    setup(configured);
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "未发送的任务" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "开始新任务" })).toBeEnabled());
    let reject: (error: Error) => void = () => {};
    vi.mocked(request).mockImplementation(path => String(path).endsWith("/agent-readiness")
      ? new Promise((_, fail) => { reject = fail; }) as never : Promise.resolve(String(path).endsWith("/agent-workflow-readiness") ? coreReady : null) as never);
    fireEvent.click(screen.getByRole("button", { name: "刷新 Agent 配置" }));
    expect(screen.getByRole("button", { name: "开始新任务" })).toBeDisabled();
    await act(async () => reject(new Error("offline")));
    await screen.findByText(/Agent 配置状态暂时无法读取/);
    expect(screen.getByRole("button", { name: "开始新任务" })).toBeDisabled();
    expect(screen.getByLabelText("任务或修改意见")).toHaveValue("未发送的任务");
  });

  it("keeps pause available even when model configuration is unavailable", async () => {
    setup(null, { ...task, job_status: "RUNNING", can_resume: false, can_interrupt: true, can_steer: true });
    await screen.findByText(/模型尚未配置或当前 Agent 运行时不可用/);
    expect(screen.getByRole("button", { name: "暂停任务" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "暂停任务" }));
    await waitFor(() => expect(vi.mocked(request).mock.calls.some(([path, opts]) => String(path).endsWith("/saved-task/interrupt") && opts?.method === "POST")).toBe(true));
  });

  it("does not admit a late prior account's readiness response", async () => {
    setSaasAccountIdentity("account-a");
    let acceptOld: (value: unknown) => void = () => {};
    vi.mocked(request).mockImplementation(path => String(path).endsWith("/agent-readiness")
      ? new Promise(resolve => { acceptOld = resolve; }) as never : Promise.resolve(String(path).endsWith("/agent-workflow-readiness") ? coreReady : null) as never);
    const page = render(<SourceItemAgentLoop {...props} />);
    const resolveA = acceptOld;
    setSaasAccountIdentity("account-b");
    page.rerender(<SourceItemAgentLoop {...props} />);
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "账户 B 的任务" } });
    await act(async () => resolveA(configured));
    expect(screen.getByRole("button", { name: "开始新任务" })).toBeDisabled();
    await act(async () => acceptOld({ ready: false }));
    await screen.findByText(/模型尚未配置或当前 Agent 运行时不可用/);
    expect(screen.getByRole("button", { name: "开始新任务" })).toBeDisabled();
  });

  it("preserves local and Pair entry contracts without a SaaS readiness request", async () => {
    setSaasAccountIdentity(undefined);
    vi.mocked(request).mockResolvedValue(null);
    render(<SourceItemAgentLoop {...props} />);
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "本地任务" } });
    expect(screen.getByRole("button", { name: "开始新任务" })).toBeEnabled();
    expect(vi.mocked(request).mock.calls.some(([path]) => String(path).endsWith("/agent-readiness"))).toBe(false);
  });

  it("disables the visible existing-Map Kit mode, rejects direct calls and leaves deterministic capture available", async () => {
    setSaasAccountIdentity("account-a");
    vi.mocked(request).mockImplementation(async path => String(path).endsWith("/agent-readiness") ? configured as never : String(path).endsWith("/agent-workflow-readiness") ? coreReady as never : null as never);
    render(<SourceDocumentActions projectId="p" documentId="d" sourceSha256={props.sourceSha256} mapArtifactId="old-map" onUpdated={vi.fn()} />);
    await screen.findByRole("button", { name: "补全原文条目" });
    expect(screen.getByRole("button", { name: "补全原文条目" })).toBeDisabled();
    expect(screen.getByText(SAAS_KIT_UNAVAILABLE_MESSAGE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "快速读取 · 无 AI" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "补全原文条目" }));
    await expect(startSourceDocumentGraph("p", "d", props.sourceSha256, "old-map")).rejects.toThrow(SAAS_KIT_UNAVAILABLE_MESSAGE);
    expect(vi.mocked(request).mock.calls.some(([, opts]) => opts?.method === "POST")).toBe(false);
  });
});
