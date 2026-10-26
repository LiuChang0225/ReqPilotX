/** UI/transport fixtures only: no PDF parsing, model, server, or semantic oracle. */
import { createHash } from "node:crypto";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../app/App";

const uploadBytes = "UI transport fixture; never parsed";
const sourceSha = createHash("sha256").update(uploadBytes).digest("hex");
const baselineSha = "b".repeat(64), deliverySha = "d".repeat(64), basisSha = "e".repeat(64);
const projectId = "journey-project", documentId = "journey-document";
const documentRoot = `/api/projects/${projectId}/sor-documents/${documentId}`;
const deliveryRoot = `${documentRoot}/agent-structure/delivery`;
const extractionRoot = `${deliveryRoot}/extraction-loop`;
const exportBytes = "UI-only immutable download fixture, not an XLSX or extraction result";
const userA = { id: "account-a", email: "a@example.invalid" };
const userB = { id: "account-b", email: "b@example.invalid" };
const readyCheck = { status: "ok", ready: true, reason_code: null, detail: "UI fixture" };
const configured = { schema_version: "agent-runtime-readiness/v1", provider_id: "aliyun", model_name: "ui-only",
  runtime_id: "provider-tool-loop", readiness_scope: "configuration", ready: true, configured: true, authentication_verified: false };
const workflow = { ...configured, status: "ready",
  schema_version: "agent-workflow-readiness/v1", readiness_scope: "configuration_and_core",
  checks: { postgresql: readyCheck, object_store: readyCheck, durable_worker: readyCheck, source_reader: readyCheck,
    customer_data_authorization: readyCheck, provider_configuration: configured } };
const readiness = { can_lock: false, blockers: ["UI fixture: no engineer acceptance"], analysis_target_count: 0,
  effective_conclusion_count: 0, ai_default_count: 0, human_count: 0 };
const preview = { source_sha256: sourceSha, delivery_sha256: deliverySha, business_basis_sha256: basisSha,
  baseline_revision: 2, response_revision: 1, readiness };
const delivery = { baseline_sha256: baselineSha, baseline_revision: 2, source_sha256: sourceSha,
  source_item_count: 0, requirement_count: 0, non_requirement_count: 0, analysis_unit_count: 0,
  assessed_unit_count: 0, pending_unit_count: 0, decomposition_job_id: null, analysis_job_id: null,
  notices: [], active_jobs: [], decomposition: null, assessments: null, business_basis_sha256: basisSha };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}
type Call = { method: string; path: string; headers: Headers; body: Record<string, unknown>; transport: "fetch" | "xhr"; credentials?: RequestCredentials };

/** Stateful HTTP boundary, never a mock of an application component or API module. */
class JourneyHttp {
  account: typeof userA | null = null;
  nextAccount = userA;
  projects: Array<{ id: string; name: string; customer_name: string; owner: string }> = [];
  documents: Array<Record<string, unknown>> = [];
  receipts = new Map<string, unknown>();
  calls: Call[] = [];
  unexpected: string[] = [];
  uploads: FakeUpload[] = [];
  task: Record<string, unknown> | null = null;
  saved = false;
  versions: Array<Record<string, unknown>> = [];
  loginReply = deferred<void>();
  holdLogin = false;
  holdUpload = false;
  loseUploadReply = false;
  holdStart = false;
  startReply = deferred<void>();
  running = false;
  holdInterrupt = false;
  interruptReply = deferred<void>();
  holdResume = false;
  resumeReply = deferred<void>();
  holdVersion = false;
  versionReply = deferred<void>();
  mismatchPreview = false;

  mutations(path: string) { return this.calls.filter(call => call.method === "POST" && call.path === path); }
  session() { return { enabled: true, user: this.account, csrf_token: this.account ? `csrf-${this.account.id}` : null }; }
  setTask(status: string, saved = false, jobId = String(this.task?.job_id || "initial-attempt")) {
    this.saved = saved;
    return this.task = { task_id: "journey-task", job_id: jobId,
      source_sha256: sourceSha, job_status: status, can_steer: status === "RUNNING", can_interrupt: status === "RUNNING",
      can_resume: status !== "RUNNING", stages: [], result: saved ? { application_saved: true } : null };
  }
  record(method: string, path: string, headers: Headers, body: Record<string, unknown>, transport: Call["transport"], credentials?: RequestCredentials) {
    const call = { method, path, headers, body, transport, credentials }; this.calls.push(call); return call;
  }
  async fetch(input: RequestInfo | URL, init: RequestInit = {}) {
    const url = new URL(String(input), window.location.origin);
    if (url.origin !== window.location.origin) throw new Error("External fixture request refused");
    const path = url.pathname, method = init.method || "GET";
    const body = typeof init.body === "string" ? JSON.parse(init.body) as Record<string, unknown> : {};
    this.record(method, path, new Headers(init.headers), body, "fetch", init.credentials);
    if (path === "/api/auth/session") return json(this.session());
    if (path === "/api/auth/login") {
      if (this.holdLogin) await this.loginReply.promise;
      this.account = this.nextAccount; return json(this.session());
    }
    if (path === "/api/auth/logout") { this.account = null; return json(this.session()); }
    if (!this.account) return json({ detail: "session_expired" }, 401);
    // Match the real private pilot surface: these legacy/admin reads are not
    // employee capabilities. A fixture must not make login succeed by granting them.
    if (path === '/api/runtime/ai-provider-setup' || path === '/api/runtime/worker-fleet'
      || /\/(knowledge-documents|workspace|requirement-sets|exports)$/.test(path)) {
      return json({ detail: 'saas_route_not_enabled' }, 403);
    }
    if (path === "/api/projects") {
      if (method === "POST") {
        const project = { id: projectId, name: String(body.name), customer_name: String(body.customer_name), owner: this.account.id };
        this.projects.push(project); this.receipts.set(String(body.command_id), project); return json(project);
      }
      return json(this.projects.filter(project => project.owner === this.account!.id));
    }
    if (path.includes("/creation-commands/") || path.includes("/import-commands/")) {
      const receipt = this.receipts.get(decodeURIComponent(path.split("/").at(-1)!));
      return receipt ? json(receipt) : json({ detail: "entry_command_not_found" }, 404);
    }
    if (path === `/api/projects/${projectId}/sor-documents`) return json(this.documents);
    if (path.endsWith("/knowledge-documents")) return json([]);
    if (path.endsWith("/processing-profile")) return json({ profile: null });
    if (path === "/api/runtime-readiness") return json({ ready: true, runtime_ready: true, checks: [], status: "READY" });
    if (path === "/api/runtime/ai-provider-setup") return json({ provider: "none", providers: {}, configured: false });
    if (path === "/api/runtime/capabilities") return json({ capabilities: [] });
    if (path === "/api/runtime/worker-fleet") return json({ workers: [], summary: {} });
    if (path === "/api/runtime/agent-readiness") return json(configured);
    if (path === "/api/runtime/agent-workflow-readiness") return json(workflow);
    if (path.endsWith("/workspace")) return json({ project: this.projects[0], sor_document: this.documents[0], requirements: [], entries: [], summary: {} });
    if (path.endsWith("/requirement-sets")) return json({ requirement_sets: [], active_requirement_set_id: "" });
    if (path.endsWith("/extraction/llm-document/status")) return json({ project_id: projectId, document_id: documentId, job: null });
    if (path.endsWith("/structure")) return json({ nodes: [], entries: [], summary: {} });
    if (path.endsWith("/structure/optimization/status")) return json({ job: null });
    if (path.endsWith("/exports")) return json([]);
    if (path === `${deliveryRoot}/state`) return this.saved ? json(delivery) : json({ detail: "not_found" }, 404);
    if (path.endsWith("/plugin-workbench/latest")) return json(null);
    if (path.endsWith("/plugin-workbench/sessions")) return json({ sessions: [] });
    if (path.endsWith("/events")) return json({ events: [], next_cursor: 0 });
    if (path === extractionRoot && method === "POST") {
      if (this.holdStart) await this.startReply.promise;
      return json(this.setTask(this.running ? "RUNNING" : "SUCCEEDED", !this.running));
    }
    if (path === `${extractionRoot}/journey-task/interrupt` && method === "POST") {
      if (this.holdInterrupt) await this.interruptReply.promise;
      return json(this.setTask("CANCELLED"));
    }
    if (path === `${extractionRoot}/journey-task/resume` && method === "POST") {
      if (this.holdResume) await this.resumeReply.promise;
      return json(this.setTask("SUCCEEDED", true, "resumed-attempt"));
    }
    if (/\/delivery\/(?:extraction-loop|loop)\/latest$/.test(path)) return json(this.task);
    if (/\/delivery\/(?:extraction-loop|loop)\/journey-task$/.test(path)) return json(this.task);
    if (path === `${deliveryRoot}/versions/preview`) return json({ ...preview, ...(this.mismatchPreview ? { source_sha256: "f".repeat(64) } : {}) });
    if (path === `${deliveryRoot}/versions`) {
      if (method === "POST") {
        if (this.holdVersion) await this.versionReply.promise;
        const saved = { ...preview, revision: 1, mode: "DRAFT", created_at: "2026-10-15T00:00:00Z" };
        this.versions = [saved]; return json(saved);
      }
      return json({ versions: this.versions });
    }
    if (path === `${deliveryRoot}/versions/1/export`) return new Response(exportBytes, { headers: { "Content-Type": "application/octet-stream" } });
    // Explicitly absent compatibility artifacts: this journey uses the native document loop.
    if (/\/agent-structure\/(?:requirement-baseline\/status|document-map\/status|source-graph\/status|native\/reference|native-reference(?:\/working-revision-head)?)$/.test(path)
      || path.endsWith("/extraction/runs/latest")) return json({ detail: "not_found" }, 404);
    this.unexpected.push(`${method} ${path}`); return json({ detail: "unexpected_ui_fixture_route" }, 500);
  }
}

let http: JourneyHttp;
class FakeUpload extends EventTarget {
  upload = new EventTarget();
  responseType = "";
  withCredentials = false;
  response: unknown = null;
  responseText = "";
  status = 0;
  aborted = false;
  headers = new Headers();
  path = "";
  form: FormData | null = null;
  open(method: string, path: string) { expect(method).toBe("POST"); this.path = path; }
  setRequestHeader(key: string, value: string) { this.headers.set(key, value); }
  send(form: FormData) {
    expect(this.path).toBe(`/api/projects/${projectId}/sor-documents`);
    expect(this.withCredentials).toBe(true);
    expect(form.get("file")).toBeInstanceOf(File);
    expect((form.get("file") as File).name).toBe("journey-ui-only.pdf");
    expect((form.get("file") as File).size).toBe(new TextEncoder().encode(uploadBytes).length);
    this.form = form; http.uploads.push(this);
    http.record("POST", this.path, this.headers, { command_id: form.get("command_id") }, "xhr");
    if (!http.holdUpload) queueMicrotask(() => this.finish());
  }
  abort() { this.aborted = true; this.dispatchEvent(new Event("abort")); }
  lateSuccess() {
    // Deliberately emit load even after abort: the old response must not remount private UI.
    this.status = 200;
    this.response = { id: documentId, project_id: projectId, file_name: "journey-ui-only.pdf",
      source_sha256: sourceSha, source_size: new TextEncoder().encode(uploadBytes).length };
    this.dispatchEvent(new Event("load"));
  }
  finish() {
    if (this.aborted) return;
    const file = this.form!.get("file") as File;
    const document = { id: documentId, project_id: projectId, file_name: file.name, file_type: "pdf", source_sha256: sourceSha,
      source_size: file.size, source_format: "pdf", page_count: 0 };
    http.documents = [document]; http.receipts.set(String(this.form!.get("command_id")), document);
    this.upload.dispatchEvent(new ProgressEvent("progress", { lengthComputable: true, loaded: file.size, total: file.size }));
    this.upload.dispatchEvent(new Event("load"));
    if (http.loseUploadReply) { this.dispatchEvent(new Event("error")); return; }
    this.status = 200; this.response = document; this.dispatchEvent(new Event("load"));
  }
}

beforeEach(() => {
  http = new JourneyHttp(); localStorage.clear(); sessionStorage.clear();
  window.history.replaceState(null, "", "/");
  vi.stubGlobal("fetch", http.fetch.bind(http)); vi.stubGlobal("XMLHttpRequest", FakeUpload);
  vi.stubGlobal("EventSource", undefined);
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear(); });

async function login(view = render(<App />)) {
  await screen.findByRole("heading", { name: "登录私人工作区" });
  expect(http.calls.filter(call => call.path.startsWith("/api/projects"))).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: http.nextAccount.email } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "UI-fixture-password-only" } });
  await userEvent.dblClick(screen.getByRole("button", { name: "登录" }));
  return view;
}
async function createProject() {
  await userEvent.click(await screen.findByRole("link", { name: "打开工作台" }));
  fireEvent.click(await screen.findByRole("button", { name: "创建项目" }));
  fireEvent.change(screen.getByRole("textbox", { name: "项目名称" }), { target: { value: "UI journey project" } });
  fireEvent.change(screen.getByRole("textbox", { name: "客户名称" }), { target: { value: "UI fixture owner" } });
  await userEvent.dblClick(screen.getByRole("button", { name: "创建并进入" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "添加客户来源" })).toBeEnabled());
  expect(http.mutations("/api/projects")).toHaveLength(1);
}
async function upload(view: ReturnType<typeof render>) {
  const input = view.container.querySelector('input[type="file"]')!;
  const file = new File([uploadBytes], "journey-ui-only.pdf", { type: "application/pdf", lastModified: 1 });
  fireEvent.change(input, { target: { files: [file, file] } });
  await waitFor(() => expect(http.uploads).toHaveLength(1));
  return file;
}
async function openAgent() {
  const complete = await screen.findByRole("button", { name: "关闭上传窗口" });
  fireEvent.click(complete);
  fireEvent.click(await screen.findByRole("button", { name: /^journey-ui-only\.pdf/ }));
  fireEvent.click(screen.getByRole("button", { name: "矩阵导出" }));
  const collaboration = screen.queryByRole("button", { name: "Agent 协作" });
  if (collaboration) fireEvent.click(collaboration);
  const composer = await screen.findByLabelText("任务或修改意见");
  fireEvent.change(composer, { target: { value: "UI request only; no extraction assertion" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "开始新任务" })).toBeEnabled());
}
async function savedExport() {
  await screen.findByRole("heading", { name: "导出当前工作版" });
  const section = await screen.findByRole("region", { name: "交付版本" });
  expect(within(section).getByText("原文 v2 · 响应 v1")).toBeInTheDocument();
  const save = await within(section).findByRole("button", { name: "保存草稿版本" });
  http.holdVersion = true;
  await userEvent.dblClick(save);
  expect(http.mutations(`${deliveryRoot}/versions`)).toHaveLength(1);
  expect(within(section).getByRole("button", { name: "正在保存…" })).toBeDisabled();
  await act(async () => http.versionReply.resolve());
  const download = await within(section).findByRole("link", { name: "下载 Excel" });
  expect(download).toHaveAttribute("href", `${deliveryRoot}/versions/1/export`);
  expect(within(section).getByRole("checkbox")).toBeDisabled();
  const call = http.mutations(`${deliveryRoot}/versions`)[0];
  expect(call.body).toMatchObject({ expected_source_sha256: sourceSha, expected_delivery_sha256: deliverySha,
    expected_business_basis_sha256: basisSha, mode: "DRAFT", accept_snapshot: false });
  expect(http.mutations(`${deliveryRoot}/versions`)).toHaveLength(1);
  expect(await (await fetch(download.getAttribute("href")!)).text()).toBe(exportBytes);
}

describe("SaaS account-to-delivery composed Web UI (HTTP fixtures, no extraction quality claim)", () => {
  it("gates the real App, creates once, uploads once, starts the native loop and downloads the pinned draft", async () => {
    http.holdLogin = true; http.holdStart = true;
    const view = await login();
    expect(screen.queryByRole("button", { name: "添加客户来源" })).not.toBeInTheDocument();
    expect(http.mutations("/api/auth/login")).toHaveLength(1);
    await act(async () => http.loginReply.resolve());
    await createProject(); await upload(view); await openAgent();
    expect(screen.queryByRole('button', {name:'管理员设置'})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name:'添加知识依据'})).not.toBeInTheDocument();
    await userEvent.dblClick(screen.getByRole("button", { name: "开始新任务" }));
    expect(http.mutations(extractionRoot)).toHaveLength(1);
    expect(http.mutations(extractionRoot)[0].body).toMatchObject({ expected_source_sha256: sourceSha,
      goal: "UI request only; no extraction assertion", origin: "USER_REQUEST", require_map_confirmation: false });
    expect(http.mutations(extractionRoot)[0].body).not.toHaveProperty("expected_baseline_sha256");
    await act(async () => http.startReply.resolve());
    await savedExport();
    expect(http.calls.filter(call => call.path === '/api/runtime/ai-provider-setup'
      || /\/(knowledge-documents|workspace|requirement-sets|exports)$/.test(call.path)).map(call => call.path)).toEqual([]);
    for (const call of http.calls.filter(call => call.method === "POST" && !call.path.startsWith("/api/auth/"))) {
      expect(call.headers.get("X-ReqPilot-CSRF")).toBe("csrf-account-a");
      expect(call.body.command_id).toEqual(expect.any(String));
    }
    for (const call of http.calls.filter(call => call.method === "POST" && call.transport === "fetch")) {
      expect(call.credentials).toBe("same-origin");
    }
    expect(http.unexpected).toEqual([]);
  });

  it("recovers a committed upload after its reply is lost, without resending or losing it on reload", async () => {
    http.loseUploadReply = true;
    let view = await login(); await createProject(); await upload(view);
    await screen.findByText(/已保存 1 份/);
    expect(http.calls.some(call => call.path.includes("/import-commands/"))).toBe(true);
    view.unmount(); view = render(<App />);
    await screen.findByRole("button", { name: /^journey-ui-only\.pdf/ });
    expect(http.uploads).toHaveLength(1);
    expect(screen.queryByText("上传失败")).not.toBeInTheDocument();
    expect(http.unexpected).toEqual([]);
  });

  it("interrupts and resumes the same native task, preserving its source and publishing a versioned draft", async () => {
    http.running = true;
    const view = await login(); await createProject(); await upload(view); await openAgent();
    fireEvent.click(screen.getByRole("button", { name: "开始新任务" }));
    const stop = await screen.findByRole("button", { name: "暂停任务" });
    expect(http.task?.job_id).toBe("initial-attempt");
    http.holdInterrupt = true;
    await userEvent.dblClick(stop);
    expect(http.mutations(`${extractionRoot}/journey-task/interrupt`)).toHaveLength(1);
    expect(stop).toBeDisabled();
    await act(async () => http.interruptReply.resolve());
    expect(http.task).toMatchObject({ task_id: "journey-task", job_id: "initial-attempt", job_status: "CANCELLED", source_sha256: sourceSha });
    const resume = await screen.findByRole("button", { name: "继续此任务" });
    http.holdResume = true;
    await userEvent.dblClick(resume);
    expect(http.mutations(extractionRoot)).toHaveLength(1);
    expect(http.mutations(`${extractionRoot}/journey-task/interrupt`)).toHaveLength(1);
    expect(http.mutations(`${extractionRoot}/journey-task/resume`)).toHaveLength(1);
    const commands = [extractionRoot, `${extractionRoot}/journey-task/interrupt`, `${extractionRoot}/journey-task/resume`]
      .map(path => http.mutations(path)[0].body.command_id);
    for (const command of commands) expect(command).toEqual(expect.stringMatching(/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/));
    expect(new Set(commands).size).toBe(3);
    await act(async () => http.resumeReply.resolve());
    expect(http.task).toMatchObject({ task_id: "journey-task", job_id: "resumed-attempt", source_sha256: sourceSha });
    await savedExport();
    expect(http.unexpected).toEqual([]);
  });

  it("unmounts private work and aborts an in-flight upload on expiry before another account signs in", async () => {
    http.holdUpload = true;
    const view = await login(); await createProject(); await upload(view);
    act(() => { http.account = null; window.dispatchEvent(new Event("reqpilotx:session-expired")); });
    await screen.findByRole("heading", { name: "登录私人工作区" });
    expect(http.uploads[0].aborted).toBe(true);
    expect(screen.queryByText("UI journey project")).not.toBeInTheDocument();
    http.nextAccount = userB;
    fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: userB.email } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "UI-fixture-password-only" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await screen.findByLabelText("当前账户");
    expect(screen.getByLabelText("当前账户")).toHaveTextContent(userB.email);
    act(() => http.uploads[0].lateSuccess());
    expect(screen.queryByText("journey-ui-only.pdf")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "下载 Excel" })).not.toBeInTheDocument();
    expect(http.unexpected).toEqual([]);
  });

  it("refuses a mismatched delivery preview at the composed export surface", async () => {
    http.mismatchPreview = true;
    const view = await login(); await createProject(); await upload(view); await openAgent();
    fireEvent.click(screen.getByRole("button", { name: "开始新任务" }));
    await screen.findByText("工作版已更新，请同步工作版后核对交付。");
    expect(screen.queryByRole("button", { name: "保存草稿版本" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "下载 Excel" })).not.toBeInTheDocument();
    expect(http.mutations(`${deliveryRoot}/versions`)).toHaveLength(0);
    expect(http.unexpected).toEqual([]);
  });
});
