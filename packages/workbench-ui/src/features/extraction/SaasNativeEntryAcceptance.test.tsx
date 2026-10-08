import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, request } from "../../shared/api/client";
import type { AgentStructureProposalStatusV1, NativeStructureReferencePublicV1, SourceRequirementBaselineOutlineV1, SourceRequirementRecordDetailV1 } from "../../shared/api/contracts";
import { setSaasAccountIdentity } from "../../shared/api/saasSession";
import { DocumentWorkSurface } from "../workspace/DocumentWorkSurface";
import { DocumentAgentContext } from "../workspace/DocumentAgentContext";
import type { WorkspaceRoute } from "../workspace/route";
import { SourceDocumentActions } from "./SourceDocumentActions";
import { SourceItemAgentLoop, type AgentLoopTask } from "./SourceItemAgentLoop";
import { SourceItemDeliveryActions, SourceItemReasoningDetail, useSourceItemReasoning, type SourceItemDeliveryState } from "./SourceItemReasoning";
import { SourceItemLens } from "./SourceRequirementNodeLens";
import { AgentStructureProposalDock } from "./AgentStructureProposalDock";
import { NativeStructureReferenceDock } from "./NativeStructureReferenceDock";
import { SAAS_KIT_UNAVAILABLE_MESSAGE, useSaasNativeAgentAvailability } from "./saasAgentAvailability";

const harness = vi.hoisted(() => ({
  source: "a".repeat(64),
  route: { task: "extraction", mode: "organize", checkpoint: "structure", projectId: "accept-project", documentId: "accept-document", requirementSetId: "" } as WorkspaceRoute,
  updateRoute: vi.fn(), refreshDocument: vi.fn(),
}));
vi.mock("../workspace/WorkspaceContext", () => ({ useWorkspace: () => ({
  route: harness.route,
  updateRoute: (patch: Partial<WorkspaceRoute>) => { harness.updateRoute(patch); harness.route = { ...harness.route, ...patch }; },
  selectedDocument: { source_sha256: harness.source, file_name: "acceptance.pdf" },
  refreshDocument: harness.refreshDocument,
}) }));
vi.mock("../../shared/api/client", async original => ({ ...await original<object>(), request: vi.fn() }));

const configured = { schema_version: "agent-runtime-readiness/v1", provider_id: "aliyun", model_name: "acceptance-model", runtime_id: "provider-tool-loop", readiness_scope: "configuration", ready: true, configured: true, authentication_verified: false };
const readyCheck = { status: "ok", ready: true, reason_code: null, detail: "Acceptance fixture" };
const workflowConfigured = { schema_version: "agent-workflow-readiness/v1", status: "ready", ready: true,
  runtime_id: "provider-tool-loop", provider_id: configured.provider_id, model_name: configured.model_name,
  readiness_scope: "configuration_and_core", authentication_verified: false, reason_code: null, blocking_reason_codes: [],
  checks: { postgresql: readyCheck, object_store: readyCheck, durable_worker: readyCheck, source_reader: readyCheck,
    customer_data_authorization: readyCheck, provider_configuration: { ...readyCheck, runtime_id: configured.runtime_id,
      provider_id: configured.provider_id, model_name: configured.model_name, readiness_scope: configured.readiness_scope, authentication_verified: false } } };
const extractionRoot = "/api/projects/accept-project/sor-documents/accept-document/agent-structure/delivery/extraction-loop";
const props = { projectId: "accept-project", documentId: "accept-document", sourceSha256: "a".repeat(64), baselineSha256: "", selectedSourceItemId: "", mode: "extract" as const, embedded: true, onUpdated: vi.fn() };
let latest: AgentLoopTask | null = null;
let readiness: unknown = configured;
let workflowReadiness: unknown = workflowConfigured;
let write: ((path: string, options: RequestInit) => Promise<unknown>) | undefined;

function task(patch: Partial<AgentLoopTask> = {}): AgentLoopTask {
  return { task_id: "accept-task", job_id: "accept-job", job_status: "SUCCEEDED", source_sha256: harness.source,
    can_resume: true, can_interrupt: false, can_steer: false, ...patch };
}
function serve() {
  vi.mocked(request).mockImplementation(async (path, options) => {
    const url = String(path);
    if (options?.method === "POST") return (write ? await write(url, options) : task()) as never;
    if (url.endsWith("/agent-readiness")) return readiness as never;
    if (url.endsWith("/agent-workflow-readiness")) return workflowReadiness as never;
    if (url.endsWith("/delivery/state")) throw new ApiError("no working ledger", 404, null);
    if (url.endsWith("/plugin-workbench/latest") || url.endsWith("/document-map/status")) return null as never;
    if (url.endsWith("/latest") || url.endsWith("/accept-task")) return latest as never;
    if (url.includes("/events?")) return { events: [], next_cursor: 0 } as never;
    if (url.endsWith("/stages/MAP")) return { artifact_sha256: "c".repeat(64), source_sha256: harness.source, rows: [], total: 0, next_offset: null } as never;
    throw new Error(`Unexpected acceptance request: ${url}`);
  });
}
function surface(mapArtifactId = "") {
  return <DocumentWorkSurface><SourceDocumentActions projectId={harness.route.projectId} documentId={harness.route.documentId}
    sourceSha256={harness.source} mapArtifactId={mapArtifactId} onUpdated={vi.fn()} /></DocumentWorkSurface>;
}
function Probe() {
  const availability = useSaasNativeAgentAvailability();
  return <><p role="status">{availability.message}</p><button disabled={!availability.ready}>Start probe</button><button onClick={availability.refresh}>Refresh probe</button></>;
}
const posts = () => vi.mocked(request).mock.calls.filter(([, options]) => options?.method === "POST");

beforeEach(() => {
  harness.source = "a".repeat(64);
  harness.route = { task: "extraction", mode: "organize", checkpoint: "structure", projectId: "accept-project", documentId: "accept-document", requirementSetId: "" };
  latest = null; readiness = configured; workflowReadiness = workflowConfigured; write = undefined;
  setSaasAccountIdentity("account-a"); serve();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); setSaasAccountIdentity(undefined); localStorage.clear(); });

describe("independent SaaS native document entry acceptance", () => {
  it("opens the actual default DocumentWorkSurface composer and starts the audited extraction-loop", async () => {
    render(surface());
    const input = await screen.findByLabelText("任务或修改意见");
    await waitFor(() => expect(input).toBeVisible());
    await waitFor(() => expect((input as HTMLTextAreaElement).value).toContain("完整阅读当前客户文档"));
    const start = screen.getByRole("button", { name: "开始新任务" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.change(input, { target: { value: "读取完整文档并保存同版本成果" } });
    fireEvent.click(start);
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0][0]).toBe(extractionRoot);
    expect(JSON.parse(String(posts()[0][1]?.body))).toMatchObject({ expected_source_sha256: harness.source, origin: "USER_REQUEST", require_map_confirmation: false, goal: "读取完整文档并保存同版本成果" });
    expect(vi.mocked(request).mock.calls.some(([path]) => /all-runs\/start|text-runs\/start|runs\/start/.test(String(path)))).toBe(false);
  });

  it("keeps the native task resumable after MAP publication while the existing-Map Kit control is unavailable", async () => {
    latest = task({ stages: [{ stage: "MAP", artifact_sha256: "c".repeat(64), count: 1 }] });
    render(surface("published-map"));
    fireEvent.click(screen.getByRole("button", { name: "Agent 协作" }));
    const resume = await screen.findByRole("button", { name: "继续此任务" });
    await waitFor(() => expect(resume).toBeEnabled());
    expect(screen.getByRole("button", { name: "补全原文条目", hidden: true })).toBeDisabled();
    expect(screen.getAllByText(SAAS_KIT_UNAVAILABLE_MESSAGE).length).toBeGreaterThan(0);
    fireEvent.click(resume);
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0][0]).toBe(`${extractionRoot}/accept-task/resume`);
    expect(JSON.parse(String(posts()[0][1]?.body))).toMatchObject({ origin: "USER_REQUEST" });
  });

  it("preserves the replacement source and its unsent draft when an old start response arrives late", async () => {
    let acceptOld: (value: unknown) => void = () => {};
    write = () => new Promise(resolve => { acceptOld = resolve; });
    const page = render(surface());
    const oldInput = await screen.findByLabelText("任务或修改意见");
    await waitFor(() => expect(screen.getByRole("button", { name: "开始新任务" })).toBeEnabled());
    fireEvent.change(oldInput, { target: { value: "旧来源任务" } });
    fireEvent.click(screen.getByRole("button", { name: "开始新任务" }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    const oldSource = harness.source;
    harness.source = "d".repeat(64);
    page.rerender(surface());
    const newInput = await screen.findByLabelText("任务或修改意见");
    fireEvent.change(newInput, { target: { value: "新来源未发送任务" } });
    await act(async () => acceptOld(task({ source_sha256: oldSource, goal: "旧来源任务", stages: [{ stage: "MAP", artifact_sha256: "c".repeat(64), count: 1 }] })));
    expect(newInput).toHaveValue("新来源未发送任务");
    expect(screen.queryByRole("region", { name: "阶段成果工作区" })).not.toBeInTheDocument();
    expect(harness.refreshDocument).not.toHaveBeenCalled();
    expect(posts()).toHaveLength(1);
  });

  it("does not offer resume for a latest task from a replaced source", async () => {
    latest = task({ source_sha256: "f".repeat(64) });
    render(<SourceItemAgentLoop {...props} />);
    await waitFor(() => expect(vi.mocked(request).mock.calls.some(([path]) => String(path).endsWith("/latest"))).toBe(true));
    await act(async () => {});
    const resume = screen.queryByRole("button", { name: "继续此任务" });
    if (resume) { expect(resume).toBeDisabled(); fireEvent.click(resume); }
    expect(posts()).toHaveLength(0);
  });
});

describe("independent readiness lifecycle acceptance", () => {
  it.each([
    false, "ready", [], { ...configured, ready: false }, { ...configured, ready: "true" },
    { ...configured, configured: "true" }, { ...configured, readiness_scope: "online" },
    { ...configured, authentication_verified: undefined }, { ...configured, runtime_id: "provider-window-loop" },
    { ...configured, schema_version: "agent-runtime-readiness/v0" }, { ...configured, provider_id: "unknown" }, { ...configured, model_name: "" },
  ])("rejects a malformed or incompatible configuration response %j", async value => {
    readiness = value; render(<Probe />);
    await screen.findByText(/模型尚未配置或当前 Agent 运行时不可用/);
    expect(screen.getByRole("button", { name: "Start probe" })).toBeDisabled();
  });

  it("keeps loading closed and ignores the first response after refresh aborts it", async () => {
    const replies: Array<(value: unknown) => void> = [];
    const signals: AbortSignal[] = [];
    vi.mocked(request).mockImplementation((path, options) => {
      if (String(path).endsWith("/agent-workflow-readiness")) return Promise.resolve(workflowConfigured) as never;
      signals.push(options!.signal as AbortSignal);
      return new Promise(resolve => replies.push(resolve)) as never;
    });
    render(<Probe />);
    expect(screen.getByRole("button", { name: "Start probe" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Refresh probe" }));
    expect(signals[0].aborted).toBe(true);
    await act(async () => replies[0](configured));
    expect(screen.getByRole("button", { name: "Start probe" })).toBeDisabled();
    await act(async () => replies[1]({ ...configured, configured: false }));
    await screen.findByText(/模型尚未配置或当前 Agent 运行时不可用/);
    expect(screen.getByRole("button", { name: "Start probe" })).toBeDisabled();
  });

  it("fails closed on network error and allows a successful explicit refresh", async () => {
    vi.mocked(request).mockRejectedValue(new Error("offline"));
    render(<Probe />);
    await screen.findByText(/Agent 配置状态暂时无法读取/);
    expect(screen.getByRole("button", { name: "Start probe" })).toBeDisabled();
    serve();
    fireEvent.click(screen.getByRole("button", { name: "Refresh probe" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start probe" })).toBeEnabled());
  });

  it("makes no readiness request for guest or Local/Pair mode and invalidates ready state when accounts change", async () => {
    setSaasAccountIdentity(null);
    const page = render(<Probe />);
    await screen.findByText("请先登录后运行 Agent。");
    fireEvent.click(screen.getByRole("button", { name: "Refresh probe" }));
    expect(request).not.toHaveBeenCalled();
    setSaasAccountIdentity(undefined); page.rerender(<Probe />);
    expect(screen.getByRole("button", { name: "Start probe" })).toBeEnabled();
    expect(request).not.toHaveBeenCalled();
    setSaasAccountIdentity("account-a"); page.rerender(<Probe />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Start probe" })).toBeEnabled());
    let finishB: (value: unknown) => void = () => {};
    vi.mocked(request).mockImplementation(path => String(path).endsWith("/agent-workflow-readiness")
      ? Promise.resolve(workflowConfigured) as never : new Promise(resolve => { finishB = resolve; }) as never);
    setSaasAccountIdentity("account-b"); page.rerender(<Probe />);
    expect(screen.getByRole("button", { name: "Start probe" })).toBeDisabled();
    setSaasAccountIdentity(null); page.rerender(<Probe />);
    await act(async () => finishB(configured));
    expect(screen.getByRole("button", { name: "Start probe" })).toBeDisabled();
    expect(screen.getByText("请先登录后运行 Agent。")).toBeInTheDocument();
  });

  it.each([
    null, { ready: true }, { ...workflowConfigured, ready: false },
    { ...workflowConfigured, runtime_id: "codex-app-server" },
    { ...workflowConfigured, readiness_scope: "authentication_and_core" },
    { ...workflowConfigured, authentication_verified: true },
    { ...workflowConfigured, checks: { ...workflowConfigured.checks, customer_data_authorization: { ready: false } } },
  ].map((workflow, index) => ({ workflow, index })))("keeps a configured provider closed when workflow authorization/core readiness is incompatible: $index", async ({ workflow }) => {
    workflowReadiness = workflow;
    render(<SourceItemAgentLoop {...props} />);
    fireEvent.change(screen.getByLabelText("任务或修改意见"), { target: { value: "已配置但未授权的任务" } });
    await screen.findByText(/文档处理授权或后台运行环境未就绪/);
    const start = screen.getByRole("button", { name: "开始新任务" });
    expect(start).toBeDisabled(); fireEvent.click(start); expect(posts()).toHaveLength(0);
  });

  it.each(Object.keys(workflowConfigured.checks))("rejects a missing or false required workflow check: %s", async check => {
    for (const missing of [false, true]) {
      const checks: Record<string, unknown> = { ...workflowConfigured.checks };
      if (missing) delete checks[check]; else checks[check] = { ready: false };
      workflowReadiness = { ...workflowConfigured, checks };
      const page = render(<Probe />);
      await screen.findByText(/文档处理授权或后台运行环境未就绪/);
      expect(screen.getByRole("button", { name: "Start probe" })).toBeDisabled();
      page.unmount();
    }
  });

  it.each([{ provider_id: "azure" }, { model_name: "changed-profile-model" }])("rejects model/profile drift between readiness responses %j", async identity => {
    workflowReadiness = { ...workflowConfigured, ...identity };
    render(<Probe />);
    await screen.findByText(/文档处理授权或后台运行环境未就绪/);
    expect(screen.getByRole("button", { name: "Start probe" })).toBeDisabled();
  });
});

it("disables old analysis/decomposition shortcuts and rejects direct hook calls while keeping export available", async () => {
  const state: SourceItemDeliveryState = { source_sha256: harness.source, baseline_sha256: "b".repeat(64), baseline_revision: 1,
    source_item_count: 1, requirement_count: 1, non_requirement_count: 0, analysis_unit_count: 1, assessed_unit_count: 0, pending_unit_count: 1,
    decomposition_job_id: null, analysis_job_id: null, notices: [], active_jobs: [], decomposition: null, assessments: null };
  function Reasoning() {
    const workflow = useSourceItemReasoning("accept-project", "accept-document", state.baseline_sha256, state);
    return <><SourceItemDeliveryActions workflow={workflow} /><SourceItemReasoningDetail workflow={workflow} sourceItemId="source-1" requirementId="REQ-1" onPageChange={vi.fn()} reviewMode />
      <button onClick={() => void workflow.run("analysis")}>Direct analysis</button><button onClick={() => void workflow.run("decomposition", "source-1")}>Direct decomposition</button></>;
  }
  render(<Reasoning />);
  for (const name of ["分析全部需求", "分析本条", "检查本条拆分"]) expect(screen.getByRole("button", { name })).toBeDisabled();
  expect(screen.getAllByText(SAAS_KIT_UNAVAILABLE_MESSAGE).length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "导出 Matrix 草稿" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Direct analysis" }));
  fireEvent.click(screen.getByRole("button", { name: "Direct decomposition" }));
  await act(async () => {});
  expect(posts()).toHaveLength(0);
});

let lensSequence = 0;
function lensFixture(table = false) {
  const record = { source_record_id: "source-1", source_item_id: "source-1", source_display_id: "SRC-1", node_id: "text-1",
    requirement_display_id: "REQ-1", display_label: "Original clause", ancestor_path: [], pages: [1],
    requirementness: "REQUIREMENT", requirement_kinds: ["QUALITY"], logical_table_scope_id: table ? "table-1" : "", confidence: 1, evidence_state: "SUFFICIENT" };
  const outline = { source_sha256: harness.source, document_map: { artifact_sha256: "c".repeat(64) }, baseline: { artifact_id: "baseline", artifact_sha256: "b".repeat(64), revision: 1 }, records: [record] } as unknown as SourceRequirementBaselineOutlineV1;
  const detail = { source_sha256: harness.source, baseline_artifact_sha256: outline.baseline.artifact_sha256, document_map_artifact_sha256: outline.document_map.artifact_sha256,
    record: { ...record, source_form: "TEXT", text_bindings: [], table_fragments: [], shared_header_cell_ids: [], shared_table_cell_ids: [], shared_source_content: [], feedbacks: [] } } as unknown as SourceRequirementRecordDetailV1;
  vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(detail);
  return { projectId: `lens-acceptance-${++lensSequence}`, documentId: "accept-document", sourceSha256: harness.source, selectedSourceItemId: "source-1", outline,
    tablePreviewsById: {}, tableModelsById: {}, activePage: 1, onPageChange: vi.fn(), onShowSource: vi.fn(), onEvidenceChange: vi.fn(), onFeedbackSaved: vi.fn() };
}

it.each([false, true])("visibly blocks legacy text/table feedback execution in a standalone SaaS lens: table=%s", async table => {
  const lens = lensFixture(table);
  const legacyStart = vi.spyOn(api, "startSourceItemScopeRevisionRun");
  const legacyFeedback = vi.spyOn(api, "submitSourceRequirementFeedback");
  render(<SourceItemLens {...lens} />);
  await screen.findByRole("heading", { name: "Original clause" });
  fireEvent.click(screen.getByRole("button", { name: "提出修改" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "保留此条未发送意见" } });
  const classify = screen.getByRole("button", { name: table ? "交给 Agent 复核分类" : "让 Agent 复核本条" });
  expect(classify).toBeDisabled();
  expect(screen.getByRole("button", { name: "打开文档 Agent 协作" })).toBeDisabled();
  expect(screen.getByText(/本条草稿保留，意见须在文档 Agent 协作中核对并发送/)).toBeVisible();
  fireEvent.click(classify);
  expect(legacyStart).not.toHaveBeenCalled(); expect(legacyFeedback).not.toHaveBeenCalled(); expect(posts()).toHaveLength(0);
});

it("carries a preserved legacy text draft into native collaboration for explicit review without starting a legacy task", async () => {
  const lens = lensFixture();
  const first = render(<SourceItemLens {...lens} />);
  await screen.findByRole("heading", { name: "Original clause" });
  fireEvent.click(screen.getByRole("button", { name: "提出修改" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "原页中的阈值需要核对" } });
  first.unmount();
  const show = vi.fn();
  render(<DocumentAgentContext.Provider value={{ producer: "NATIVE", bind: () => () => {}, task: null, show, openStage: vi.fn(), historyHost: null }}><SourceItemLens {...lens} /></DocumentAgentContext.Provider>);
  await screen.findByRole("heading", { name: "Original clause" });
  fireEvent.click(screen.getByRole("button", { name: "继续写反馈" }));
  expect(show).toHaveBeenCalledWith("原页中的阈值需要核对", "selected");
  expect(posts()).toHaveLength(0);
  expect(Object.values(localStorage).some(value => String(value).includes("原页中的阈值需要核对"))).toBe(true);
});

it.each([false, true])("blocks the independent parent-overview shortcut in SaaS: analysis=%s", async analysisMode => {
  const lens = lensFixture();
  const run = vi.fn();
  const reasoning = { state: { source_sha256: harness.source, baseline_sha256: lens.outline.baseline.artifact_sha256, notices: [], active_jobs: [], decomposition: { records: { "source-1": [1, 2].map(value => ({ singular_requirement_id: `unit-${value}`, requirement_display_id: `REQ-1-S0${value}`, text: `child ${value}` })) } }, assessments: null }, run, busy: false } as unknown as NonNullable<Parameters<typeof SourceItemLens>[0]["reasoning"]>;
  render(<SourceItemLens {...lens} tableView analysisMode={analysisMode} reasoning={reasoning} />);
  await screen.findByRole("region", { name: "父条款与派生项" });
  const button = screen.getByRole("button", { name: analysisMode ? "分析本条派生项" : "检查本条拆分" });
  expect(button).toBeDisabled(); expect(button).toHaveAttribute("title", SAAS_KIT_UNAVAILABLE_MESSAGE);
  fireEvent.click(button); expect(run).not.toHaveBeenCalled();
});

it.each(["empty", "ready", "running"] as const)("keeps legacy proposal history readable but all unsupported SaaS writes disabled: %s", async phase => {
  const status = { proposal: phase === "ready" ? { status: "READY", source_sha256: harness.source,
    base: { expected_revision_number: 1, expected_effective_snapshot_id: "snapshot" }, operations: [], operation_count: 0 } : null,
    job: phase === "running" ? { status: "RUNNING", progress: {} } : null, steer: null } as unknown as AgentStructureProposalStatusV1;
  const reads = vi.spyOn(api, "agentStructureProposalStatus").mockResolvedValue(status);
  const starts = vi.spyOn(api, "startAgentStructureProposal");
  const revises = vi.spyOn(api, "reviseAgentStructureProposal");
  const steers = vi.spyOn(api, "steerAgentStructureProposal");
  const rejects = vi.spyOn(api, "rejectAgentStructureProposal");
  const applies = vi.spyOn(api, "applyAgentStructureProposal");
  render(<AgentStructureProposalDock projectId="accept-project" documentId="accept-document" sourceSha256={harness.source}
    revisionNumber={1} effectiveSnapshotId="snapshot" selectedStructureNodeId="node-1" onFocusNode={vi.fn()} onApplied={vi.fn()} />);
  await waitFor(() => expect(reads).toHaveBeenCalledTimes(1));
  expect(screen.getByText(SAAS_KIT_UNAVAILABLE_MESSAGE)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "详情" }));
  const names = phase === "ready" ? ["生成新版本", "拒绝建议", "应用 0 项修正"] : phase === "running" ? ["调整当前检查", "检查当前节点"] : ["检查当前节点"];
  for (const name of names) { const button = screen.getByRole("button", { name }); expect(button).toBeDisabled(); fireEvent.click(button); }
  for (const mutation of [starts, revises, steers, rejects, applies]) expect(mutation).not.toHaveBeenCalled();
  expect(posts()).toHaveLength(0);
});

it("keeps legacy native structure feedback, continuation and proposal decisions visibly unavailable", async () => {
  const node = { node_id: "doc-root", parent_id: null, order: 0, kind: "DOCUMENT_ROOT", label: "", title: "Acceptance native structure", title_authority: "VISIBLE_TITLE", pages: [1], content_blocks: [], finding_codes: [], source_anchors: [], source_content: [] };
  const reference = { schema_version: "native-structure-reference-public/v1", availability: "READY", artifact_id: "native-acceptance", source_sha256: harness.source,
    artifact_status: "PARTIAL", coverage_status: "COMPLETE", boundary_status: "PARTIAL_WITH_EXPLICIT_UNRESOLVED", workflow_step: "DOCUMENT_STRUCTURE_RECOVERY",
    structure_projection: { schema_version: "native-structure-projection-state/v1", state: "ACCEPTED", base_artifact_id: "native-acceptance", attempt_id: null, attempt_ordinal: null, affected_scope_ids: [], pending_rendered_scope_ids: [], validation_status: "PASS", projection_sha256: "b".repeat(64) },
    summary: { primary_document_units: 1, main_document_top_level_sections: 0, top_level_logical_tables: 0, nested_physical_tables: 0, nested_visual_tables: 0, opened_high_resolution_pages: 1, count_is_quality_gate: false },
    page_ownership: [], main_document_sections: [], document_tree: { schema_version: "native-document-structure-tree/v1", root_ids: [node.node_id], nodes: [node] },
    logical_tables: [], unresolved: [], validation: { status: "PASS", checks: {}, quality_claim: "NOT_GOLD" },
    oracle_diagnostic: { authority: "NOT_GOLD", oracle_expected_region_count: 0, native_region_disposition_count: 0, count_is_correctness_signal: false, native_boundary_corrections: [] },
    structure_proposals: [{ proposal_id: "proposal-image-1", finding_code: "visual_asset_parent_binding_missing", node_id: node.node_id, object_kind: "IMAGE_PLACEMENT", object_id: "image-1", page: 1,
      bbox: [10, 10, 100, 100], source_bounds: [0, 0, 595, 842], proposal_title: "Image parent", observation: "Source image", recommended_action: "ATTACH_IMAGE_TO_NODE", recommendation: "Attach source image", recommended_target_node_id: node.node_id, impact: "Image source", confidence: "HIGH", default_policy: "AUTO_APPLY_REVERSIBLE", status: "AUTO_APPLIED_PENDING_REVIEW", overlap_ratio: 1, decision: null }],
    activity: [], feedbacks: [], continuation: { status: "READY_TO_CONTINUE", can_continue: true, pending_feedback_count: 1, pending_proposal_decision_count: 0, confirmed_proposal_count: 0, undone_proposal_count: 0, kept_unresolved_proposal_count: 0, review_pending_proposal_count: 1, latest_handoff: null, message: "Pending legacy guidance" },
    feedback_capability: { status: "QUEUE_READY", agent_delivery: "IDLE", message: "Pending legacy reader" },
  } as unknown as NativeStructureReferencePublicV1;
  vi.spyOn(api, "nativeStructureReference").mockResolvedValue(reference);
  const save = vi.spyOn(api, "submitNativeStructureFeedback");
  const resume = vi.spyOn(api, "continueNativeStructureReference");
  const decision = vi.spyOn(api, "submitNativeStructureProposalDecision");
  const dock = <NativeStructureReferenceDock projectId="native-acceptance" documentId="document" sourceSha256={harness.source} onPageChange={vi.fn()} />;
  const page = render(<DocumentAgentContext.Provider value={null}>{dock}</DocumentAgentContext.Provider>);
  const input = await screen.findByLabelText("给 Agent 的节点指引");
  fireEvent.change(input, { target: { value: "保留此节点未发送指引" } });
  for (const name of ["保存指引", "继续处理 1 条指引", "撤销"]) {
    const button = screen.getByRole("button", { name }); expect(button).toBeDisabled(); fireEvent.click(button);
  }
  expect(screen.getByText(/旧结构指引与建议确认尚未在当前 SaaS 试点开放/)).toBeVisible();
  const show = vi.fn();
  page.rerender(<DocumentAgentContext.Provider value={{ producer: "NATIVE", bind: () => () => {}, task: null, show, openStage: vi.fn(), historyHost: null }}>{dock}</DocumentAgentContext.Provider>);
  fireEvent.click(screen.getByRole("button", { name: "打开文档 Agent 协作" }));
  expect(show).toHaveBeenCalledWith("保留此节点未发送指引", "document");
  expect(screen.getByLabelText("给 Agent 的节点指引")).toHaveValue("保留此节点未发送指引");
  expect(save).not.toHaveBeenCalled(); expect(resume).not.toHaveBeenCalled(); expect(decision).not.toHaveBeenCalled();
});
