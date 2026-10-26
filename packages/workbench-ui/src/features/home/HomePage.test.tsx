import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HomePage } from "./HomePage";

const serviceReady = { runtime_ready: true, ready: false, status: "READY", checks: [] };
const agentConfigured = {
  ready: true, configured: true, runtime_id: "provider-tool-loop",
  readiness_scope: "configuration", authentication_verified: false, model_name: "trial-model",
};

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
}

function serve(overrides: Record<string, () => Response | Promise<Response>> = {}) {
  const handlers: Record<string, () => Response | Promise<Response>> = {
    "/api/projects": () => json([]),
    "/api/runtime-readiness": () => json(serviceReady),
    "/api/runtime/agent-readiness": () => json(agentConfigured),
    ...overrides,
  };
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const handler = handlers[path] || (/^\/api\/projects\/[^/]+\/sor-documents$/.test(path) ? () => json([]) : undefined);
    if (!handler || (init?.method && init.method !== "GET")) throw new Error(`Unexpected request: ${String(input)}`);
    return Promise.resolve(handler());
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("HomePage", () => {
  it("loads saved projects and preserves project, quick, agent, and configuration destinations", async () => {
    const fetchMock = serve({
      "/api/projects": () => json([
        { id: "old-project", name: "旧项目", customer_name: "客户乙", created_at: "2026-10-01T10:00:00Z" },
        { id: "project/one", name: "传感器 SOR", customer_name: "客户甲", created_at: "2026-10-20T10:00:00Z" },
      ]),
      "/api/projects/project%2Fone/sor-documents": () => json([
        { id: "older-doc", project_id: "project/one", file_name: "此前版本.docx", file_type: "docx", created_at: "2026-10-20T10:00:00Z" },
        { id: "doc/two", project_id: "project/one", file_name: "客户技术要求.pdf", file_type: "pdf", created_at: "2026-10-23T10:00:00Z" },
      ]),
    });
    render(<HomePage />);

    const projectLink = await screen.findByRole("link", { name: "继续项目：传感器 SOR" });
    expect(projectLink).toHaveAttribute("href", "/workspace?task=projects&projectId=project%2Fone");
    expect(within(projectLink).getByText("客户甲")).toBeVisible();
    const projectLinks = within(screen.getByRole("region", { name: "继续项目" })).getAllByRole("link", { name: /^继续项目/ });
    expect(projectLinks[0]).toBe(projectLink);
    expect(screen.getByRole("link", { name: "选择文档读取" })).toHaveAttribute("href", "/workspace?task=projects&entry=quick");
    expect(screen.getByRole("link", { name: "选择文档整理" })).toHaveAttribute("href", "/workspace?task=projects&entry=agent");
    expect(screen.getByRole("link", { name: "配置 Agent" })).toHaveAttribute("href", "/workspace?task=projects&settings=agent");
    expect(await screen.findByText("配置就绪")).toBeVisible();
    expect(screen.getByText(/在线连接与额度将在任务启动时检查/)).toBeVisible();
    expect(screen.getByText("服务可用")).toBeVisible();
    const quickLink = await screen.findByRole("link", { name: "快速读取：客户技术要求.pdf" });
    expect(quickLink).toHaveAttribute("href", "/workspace?task=extraction&mode=organize&checkpoint=structure&entry=quick&projectId=project%2Fone&documentId=doc%2Ftwo");
    expect(screen.getByRole("link", { name: "Agent 整理：客户技术要求.pdf" })).toHaveAttribute("href", "/workspace?task=extraction&mode=organize&checkpoint=structure&entry=agent&projectId=project%2Fone&documentId=doc%2Ftwo");
    const documentRegion = screen.getByRole("region", { name: "最近文档" });
    expect(within(documentRegion).getAllByRole("link", { name: /^快速读取/ })[0]).toBe(quickLink);
    expect(within(documentRegion).getByText("客户技术要求.pdf")).toBeVisible();
    expect(within(documentRegion).getByText(/PDF/)).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("sor-documents")).map(([input]) => input)).toEqual(["/api/projects/project%2Fone/sor-documents"]);
    expect(document.title).toBe("ReqPilotX · 工作首页");
  });

  it("shows the real empty state without treating a missing Agent as a service failure", async () => {
    serve({ "/api/runtime/agent-readiness": () => json({ ready: false, configured: false, runtime_id: "none" }) });
    render(<HomePage />);

    expect(await screen.findByRole("heading", { name: "创建你的第一个项目" })).toBeVisible();
    expect(await screen.findByText("未配置")).toBeVisible();
    expect(screen.getByText("服务可用")).toBeVisible();
    expect(screen.getByRole("link", { name: "打开工作台" })).toBeVisible();
    expect(screen.queryByRole("link", { name: /^继续项目/ })).not.toBeInTheDocument();
  });

  it("retains project navigation when readiness checks fail and refreshes both statuses", async () => {
    let recovered = false;
    serve({
      "/api/projects": () => json([{ id: "saved", name: "已有项目", customer_name: "客户" }]),
      "/api/runtime-readiness": () => recovered ? json(serviceReady) : json({ detail: "Service unavailable" }, 503),
      "/api/runtime/agent-readiness": () => recovered ? json({ runtime_id: "codex-app-server", ready: true, model_name: "configured-model" }) : json({ detail: "Agent unavailable" }, 503),
    });
    render(<HomePage />);

    expect(await screen.findByRole("link", { name: "继续项目：已有项目" })).toBeVisible();
    expect(await screen.findByText("连接未确认")).toBeVisible();
    expect(screen.getByText("状态未取得")).toBeVisible();
    recovered = true;
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    expect(await screen.findByText("服务可用")).toBeVisible();
    expect(await screen.findByText("已就绪")).toBeVisible();
    expect(screen.queryByText("状态未取得")).not.toBeInTheDocument();
  });

  it("retries a failed project request without hiding the entry choices", async () => {
    let attempts = 0;
    serve({
      "/api/projects": () => ++attempts === 1 ? json({ detail: "项目读取暂时失败" }, 503) : json([{ id: "recovered", name: "恢复的项目", customer_name: "客户" }]),
    });
    render(<HomePage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("项目读取暂时失败");
    expect(screen.getByRole("link", { name: "选择文档读取" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByRole("link", { name: "继续项目：恢复的项目" })).toBeVisible();
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(attempts).toBe(2);
  });

  it("keeps project entry available while document loading fails and retries only that list", async () => {
    let documentAttempts = 0;
    const fetchMock = serve({
      "/api/projects": () => json([{ id: "active", name: "当前项目", customer_name: "客户" }]),
      "/api/projects/active/sor-documents": () => ++documentAttempts === 1
        ? json({ detail: "文档服务暂不可用" }, 503)
        : json([{ id: "document", project_id: "active", file_name: "新文档.pdf", file_type: "pdf" }]),
    });
    render(<HomePage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("文档服务暂不可用");
    expect(screen.getByRole("link", { name: "继续项目：当前项目" })).toBeVisible();
    expect(screen.getByText("服务可用")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重新加载文档" }));
    expect(await screen.findByRole("link", { name: "快速读取：新文档.pdf" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(documentAttempts).toBe(2);
    expect(fetchMock.mock.calls.filter(([input]) => input === "/api/projects")).toHaveLength(1);
  });
});
