import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EngineerOpinion, type EngineerResponseView } from "./EngineerOpinion";
import { SourceItemReasoningDetail, type SourceItemDeliveryState } from "./SourceItemReasoning";
afterEach(() => { cleanup(); localStorage.clear(); });
function view(): EngineerResponseView { return { available:true,response_revision:0,response_sha256:"",history:[],records:{},
  units:{u:{unit_id:"u",source_item_id:"src",requirement_display_id:"REQ-0001-S01",unit_context_sha256:"a".repeat(64)}} }; }
describe("engineer working opinions", () => {
  it("submits explicit judgment, conditions and evidence requests without claiming approval", async () => {
    const user=userEvent.setup(),save=vi.fn().mockResolvedValue(undefined);
    render(<EngineerOpinion unitId="u" view={view()} onSave={save}/>);
    await user.click(screen.getByText("记录工程师工作意见"));
    expect(screen.getByText(/保存后作为本版工程师响应/)).toBeVisible();
    await user.selectOptions(screen.getByLabelText("工程师判断"),"NON_COMPLIANT");
    await user.type(screen.getByLabelText("判断理由"),"接口不一致");
    await user.click(screen.getByText("补充适用条件与证据请求"));
    await user.type(screen.getByLabelText("适用条件"),"当前方案");
    await user.type(screen.getByLabelText("证据请求"),"补正式图纸");
    await user.click(screen.getByRole("button",{name:"保存为本版工作意见"}));
    expect(save).toHaveBeenCalledWith("u",{action:"SET",decision:"NON_COMPLIANT",reason:"接口不一致",conditions:"当前方案",evidence_requests:"补正式图纸"});
  });
  it("shows manual priority and the retained AI advice for an Snn", async () => {
    const opinions=view();opinions.records.u={record_id:"m",unit_id:"u",source_item_id:"src",requirement_display_id:"REQ-0001-S01",decision:"NON_COMPLIANT",
      reason:"工程师说明",conditions:"当前方案",evidence_requests:"待图纸",state:"CURRENT",decided_at:"2026-09-08T00:00:00Z",origin:"USER_INPUT",baseline_sha256:"b".repeat(64)};
    const state={baseline_sha256:"b".repeat(64),baseline_revision:1,source_sha256:"c".repeat(64),source_item_count:1,requirement_count:1,
      non_requirement_count:0,analysis_unit_count:1,assessed_unit_count:1,pending_unit_count:0,decomposition_job_id:null,analysis_job_id:null,notices:[],active_jobs:[],
      engineer_responses:opinions,decomposition:{records:{src:[{singular_requirement_id:"u",requirement_display_id:"REQ-0001-S01",text:"原有派生内容"}]}},
      assessments:{records:{u:{decision:"COMPLIANT",reason:"原AI建议",clarification_question:"",evidence:[]}},evidence_catalog:{}}} as SourceItemDeliveryState;
    const workflow={state,busy:false,run:vi.fn(),saveEngineerResponse:vi.fn()} as unknown as Parameters<typeof SourceItemReasoningDetail>[0]["workflow"];
    render(<SourceItemReasoningDetail workflow={workflow} sourceItemId="src" requirementId="REQ-0001" onPageChange={vi.fn()}/>);
    expect(screen.getByText(/工程师工作意见：不符合/)).toBeVisible();
    expect(screen.getByText("AI分析建议（保留）")).toBeVisible();await userEvent.setup().click(screen.getByText("判断依据")); expect(screen.getByText("原AI建议")).toBeVisible();
  });
  it("makes a stale opinion explicit and requires a new save to reactivate it", async () => {
    const opinions=view();opinions.records.u={record_id:"old",unit_id:"u",source_item_id:"src",requirement_display_id:"REQ-0001",decision:"COMPLIANT",
      reason:"前版意见",conditions:"",evidence_requests:"",state:"STALE",decided_at:"2026-09-08T00:00:00Z",origin:"USER_INPUT",baseline_sha256:"b".repeat(64)};
    const save=vi.fn().mockResolvedValue(undefined);render(<EngineerOpinion unitId="u" view={opinions} onSave={save}/>);
    expect(screen.getByText("前版工程师意见已失效，需重新确认")).toBeVisible();expect(save).not.toHaveBeenCalled();
    const user=userEvent.setup();await user.click(screen.getByText("记录工程师工作意见"));
    await user.click(screen.getByRole("button",{name:"撤回人工意见"}));
    expect(save).toHaveBeenCalledWith("u",expect.objectContaining({action:"WITHDRAW"}));
  });
});

it("restores source-bound unit drafts and adopting AI never saves implicitly", async () => {
  const user = userEvent.setup(), save = vi.fn();
  const renderOpinion = (unitId: string, draftScope = "p:d:baseline") => <EngineerOpinion unitId={unitId} view={view()} onSave={save} defaultExpanded draftScope={draftScope} suggestion={{ decision:"NON_COMPLIANT",reason:"需要修正接口" }} />;
  const page = render(renderOpinion("u"));
  await user.click(screen.getByText("引用 Agent 建议为草稿"));
  expect(save).not.toHaveBeenCalled(); expect(screen.getByLabelText("判断理由")).toHaveValue("需要修正接口");
  page.rerender(renderOpinion("other")); expect(screen.getByLabelText("判断理由")).toHaveValue("");
  page.rerender(renderOpinion("u")); expect(screen.getByLabelText("判断理由")).toHaveValue("需要修正接口");
  page.rerender(renderOpinion("u", "p:d:new-baseline")); expect(screen.getByLabelText("判断理由")).toHaveValue("");
});


it("keeps a later draft when an earlier save resolves after switching away and back", async () => {
  const user=userEvent.setup(); let finish: () => void = () => {};
  const save=vi.fn(()=>new Promise<void>(resolve=>{finish=resolve;}));
  const props={view:view(),onSave:save,defaultExpanded:true,draftScope:"late-save"};
  const page=render(<EngineerOpinion {...props} unitId="u" />);
  await user.type(screen.getByLabelText("判断理由"),"第一份意见");
  await user.click(screen.getByRole("button",{name:"保存为本版工作意见"}));
  page.rerender(<EngineerOpinion {...props} unitId="other" />);
  page.rerender(<EngineerOpinion {...props} unitId="u" />);
  await user.clear(screen.getByLabelText("判断理由")); await user.type(screen.getByLabelText("判断理由"),"后来补写的草稿");
  await act(async()=>finish());
  const newer=view();newer.records.u={record_id:"saved",unit_id:"u",source_item_id:"src",requirement_display_id:"REQ-0001-S01",decision:"PENDING_EVIDENCE",reason:"第一份意见",conditions:"",evidence_requests:"",state:"CURRENT",decided_at:"2026-09-11",origin:"USER_INPUT",baseline_sha256:"b"};
  page.rerender(<EngineerOpinion {...props} view={newer} unitId="u" />);
  expect(screen.getByLabelText("判断理由")).toHaveValue("后来补写的草稿");
  page.unmount();render(<EngineerOpinion {...props} view={newer} unitId="u" />);
  expect(screen.getByLabelText("判断理由")).toHaveValue("后来补写的草稿");
});
