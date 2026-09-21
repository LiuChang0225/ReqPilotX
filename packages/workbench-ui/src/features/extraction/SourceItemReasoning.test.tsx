import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourceItemReasoningDetail, SourceItemReasoningStatus, useSourceItemReasoning, type SourceItemDeliveryState } from "./SourceItemReasoning";
import { request } from "../../shared/api/client";
vi.mock("../../shared/api/client", () => ({ request: vi.fn() }));
afterEach(() => { cleanup(); localStorage.clear(); vi.mocked(request).mockReset(); });
const sid = "source-a";
function state(): SourceItemDeliveryState {
  return { baseline_sha256: "b".repeat(64), baseline_revision: 2, source_sha256: "s".repeat(64),
    source_item_count: 1, requirement_count: 1, non_requirement_count: 0, analysis_unit_count: 1,
    assessed_unit_count: 0, pending_unit_count: 1, decomposition_job_id: null, analysis_job_id: null,
    notices: [], active_jobs: [], decomposition: { records: {} }, assessments: { records: {}, evidence_catalog: {} } };
}
function draw(value: SourceItemDeliveryState) {
  const workflow = { state: value, busy: false, run: vi.fn() } as unknown as Parameters<typeof SourceItemReasoningDetail>[0]["workflow"];
  return render(<SourceItemReasoningDetail workflow={workflow} sourceItemId={sid} requirementId="REQ-0001" onPageChange={vi.fn()} reviewMode />);
}
describe("source reasoning carry provenance", () => {
  it("does not graft a post-save newer semantic response onto older reasoning", async () => {
    const value = state(); value.business_basis_sha256 = "a".repeat(64);
    value.engineer_responses = { available:true,response_revision:0,response_sha256:"",records:{},history:[],
      units:{[sid]:{unit_id:sid,source_item_id:sid,requirement_display_id:"REQ-0001",unit_context_sha256:"c".repeat(64)}} };
    vi.mocked(request).mockImplementation(path => String(path).endsWith("engineer-responses")
      ? Promise.resolve({ ...value.engineer_responses,response_revision:1,business_basis_sha256:"d".repeat(64) })
      : new Promise(() => {}));
    const { result } = renderHook(() => useSourceItemReasoning("p","d",value.baseline_sha256,value));
    await act(async () => {
      await expect(result.current.saveEngineerResponse(sid,{action:"SET",decision:"PENDING_EVIDENCE",reason:"用户输入",conditions:"",evidence_requests:""}))
        .rejects.toThrow("依据或建议随后更新");
    });
    expect(result.current.state?.engineer_responses?.response_revision).toBe(0);
    expect(request).toHaveBeenCalledWith(expect.stringContaining("delivery/state"),expect.anything());
    vi.mocked(request).mockReset();
  });
  it("separates unassessed work from completed analysis awaiting input", () => {
    const value = { ...state(), analysis_unit_count: 3, assessed_unit_count: 2, pending_unit_count: 3,
      unassessed_unit_count: 1, awaiting_evidence_unit_count: 1, clarification_unit_count: 1 };
    const workflow = { state: value } as unknown as Parameters<typeof SourceItemReasoningStatus>[0]['workflow'];
    render(<SourceItemReasoningStatus workflow={workflow} />);
    expect(screen.getByText(/已分析 2/)).toHaveTextContent('待分析 1 · 待内部证据 1 · 待澄清 1');
    expect(screen.queryByText(/待证据或澄清 3/)).not.toBeInTheDocument();
  });
  it("distinguishes retained advice and preserves its earliest input baseline", () => {
    const value = state();
    value.assessments!.records[sid] = { decision: "COMPLIANT", reason: "Existing evidence supports the target.", evidence: [], clarification_question: "" };
    value.assessments!.unit_input_receipts = { [sid]: { operation: "SOURCE_VALIDATED_REBIND", baseline_sha256: "b".repeat(64), origin_input_receipt: { baseline_sha256: "a".repeat(64) } } };
    draw(value);
    expect(screen.getByText("沿用前版分析建议；本轮未重新分析。")).toBeVisible();
    expect(screen.getByText("a".repeat(64))).toBeInTheDocument();
    expect(screen.getByText(/符合建议/)).toBeVisible();
  });
  it("shows stale children and their analysis at the parent without claiming no prior split", () => {
    const value = state();
    value.decomposition!.carry_forward = { operation: "SOURCE_VALIDATED_REBIND", stale_parent_ids: [sid], from_baseline_sha256: "a".repeat(64) };
    value.decomposition!.identity_registry = { [sid]: { "old-child": "REQ-0001-S01" } };
    value.assessments!.carry_forward = { operation: "SOURCE_VALIDATED_REBIND", stale_unit_ids: ["old-child"], from_baseline_sha256: "a".repeat(64) };
    draw(value);
    expect(screen.getByText(/需检查拆分/)).toBeVisible();
    expect(screen.getByText(/REQ-0001-S01：前版分析/)).toBeVisible();
    expect(screen.queryByText("当前保留为一个完整原文条目，未另行派生。")).not.toBeInTheDocument();
  });
  it("shows reanalysis needed for a stale original item without fabricating a decision", () => {
    const value = state();
    value.assessments!.carry_forward = { operation: "SOURCE_VALIDATED_REBIND", stale_unit_ids: [sid] };
    draw(value);
    expect(screen.getByText(/前版分析未通过本版输入一致性核验/)).toBeVisible();
    expect(screen.queryByText(/符合建议/)).not.toBeInTheDocument();
  });
  it("does not mark freshly assessed or never split items as inherited", () => {
    draw(state());
    expect(screen.queryByLabelText("版本继承与待重评")).not.toBeInTheDocument();
    expect(screen.getByText("当前保留为一个完整原文条目，未另行派生。")).toBeVisible();
  });
});

it("shows recorded KEEP reasons and incomplete authorized scope without claiming full completion",()=>{
  const value=state();value.decomposition!.dispositions={[sid]:{action:"KEEP",reason:"共同组成一项完整试验",baseline_sha256:value.baseline_sha256}};
  value.decomposition!.review_scope={authorized_count:13,decided_count:1,remaining_count:12,complete:false};
  draw(value);expect(screen.getByText("保留整体：共同组成一项完整试验")).toBeInTheDocument();
  expect(screen.getByText(/待处理 12/)).toHaveTextContent("授权范围尚未完成");
});

it.each([{ ids: null }, { ids: [] }, { ids: ["knowledge-doc"] }])("sends the chosen analysis scope $ids while retaining selected derived units", async ({ ids }) => {
  const value = state(); value.decomposition!.records[sid] = [{ singular_requirement_id: "child", requirement_display_id: "REQ-0001-S01", text: "child" }];
  vi.mocked(request).mockResolvedValue({ job_id: "job", status: "SUCCEEDED" });
  const { result } = renderHook(() => useSourceItemReasoning("p", "d", value.baseline_sha256, value));
  act(() => result.current.knowledgeSelection.setIds(ids));
  await act(async () => result.current.run("analysis", sid));
  const body = JSON.parse(String(vi.mocked(request).mock.calls[0][1]?.body));
  expect(body.selected_unit_ids).toEqual(["child"]);
  if (ids === null) expect(body).not.toHaveProperty("knowledge_document_ids");
  else expect(body.knowledge_document_ids).toEqual(ids);
});

it("does not add analysis evidence selection to decomposition commands", async () => {
  const value = state(); vi.mocked(request).mockResolvedValue({ job_id: "job", status: "SUCCEEDED" });
  const { result } = renderHook(() => useSourceItemReasoning("p", "d", value.baseline_sha256, value));
  act(() => result.current.knowledgeSelection.setIds(["knowledge-doc"]));
  await act(async () => result.current.run("decomposition", sid));
  expect(JSON.parse(String(vi.mocked(request).mock.calls[0][1]?.body))).not.toHaveProperty("knowledge_document_ids");
});

it("keeps a single-item analysis scoped to the parent when its decomposition list is empty", async () => {
  const value = state(); value.decomposition!.records[sid] = [];
  vi.mocked(request).mockResolvedValue({ job_id: "job", status: "SUCCEEDED" });
  const { result } = renderHook(() => useSourceItemReasoning("p", "d", value.baseline_sha256, value));
  await act(async () => result.current.run("analysis", sid));
  expect(JSON.parse(String(vi.mocked(request).mock.calls[0][1]?.body)).selected_unit_ids).toEqual([sid]);
});

it("links a cited fact to the exact registered original and page", () => {
  const value = state();
  value.assessments!.records[sid] = { decision: "PENDING_EVIDENCE", reason: "引用不代表符合", clarification_question: "", evidence: [{ fact_id: "f", quote: "来源摘录" }] };
  value.assessments!.evidence_catalog.f = { document_id: "k", document_name: "记录.pdf", source_sha256: "a".repeat(64), source_ref: { location: { page_number: 3 } } };
  function Detail() { const workflow = useSourceItemReasoning("p", "d", value.baseline_sha256, value); return <SourceItemReasoningDetail workflow={workflow} sourceItemId={sid} requirementId="REQ-0001" onPageChange={vi.fn()} reviewMode />; }
  render(<Detail />);
  expect(screen.getByRole("link", { name: "记录.pdf · P3" })).toHaveAttribute("href", `/api/projects/p/knowledge-documents/k/file?expected_source_sha256=${"a".repeat(64)}#page=3`);
});
