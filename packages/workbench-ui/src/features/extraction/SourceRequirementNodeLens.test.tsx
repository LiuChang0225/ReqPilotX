import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../../shared/api/client";
import type { SourceRequirementBaselineOutlineV1, SourceRequirementRecordDetailV1 } from "../../shared/api/contracts";
import { SourceItemLens } from "./SourceRequirementNodeLens";
import { DocumentAgentContext } from "../workspace/DocumentAgentContext";

const sha = "a".repeat(64), baselineSha = "b".repeat(64), mapSha = "c".repeat(64);
const sourceId = `source-record-${"1".repeat(32)}`;
const record = { source_record_id: sourceId, source_item_id: sourceId, source_display_id: "SRC-0005",
  requirement_display_id: "REQ-0005", display_label: "尺寸图", ancestor_path: ["规格", "尺寸"],
  pages: [1], requirementness: "REQUIREMENT", requirement_kinds: ["QUALITY"],
  logical_table_scope_id: "", decision_summary: "图中定义了尺寸上限。", confidence: .9, evidence_state: "SUFFICIENT" };
function fixture() {
  const outline = { source_sha256: sha, document_map: { artifact_sha256: mapSha },
    baseline: { artifact_id: "baseline", artifact_sha256: baselineSha, revision: 3 },
    records: [{ ...record }] } as unknown as SourceRequirementBaselineOutlineV1;
  const detail = { source_sha256: sha, baseline_artifact_sha256: baselineSha, document_map_artifact_sha256: mapSha,
    record: { ...record, source_form: "IMAGE", text_bindings: [], table_fragments: [],
      shared_header_cell_ids: [], shared_table_cell_ids: [], shared_source_content: [], feedbacks: [],
      image_bindings: [{ anchor_id: "image-1", source_sha256: sha, page: 1, bbox: [20, 30, 180, 90], source_bounds: [0, 0, 595, 842] }],
      visual_interpretation: { text: "宽度上限18 mm，高度上限6 mm。", authority: "AGENT_VISUAL_INTERPRETATION" },
    } } as unknown as SourceRequirementRecordDetailV1;
  const props = { projectId: "project", documentId: "document", sourceSha256: sha,
    selectedSourceItemId: sourceId, outline, tablePreviewsById: {}, tableModelsById: {}, activePage: 1,
    onPageChange: vi.fn(), onShowSource: vi.fn(), onEvidenceChange: vi.fn(), onFeedbackSaved: vi.fn() };
  return { detail, props };
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("SourceItem original evidence", () => {
  it("keeps unsent feedback across item switches and remounts, isolated by baseline", async () => {
    const { detail, props } = fixture();
    props.projectId = "draft-navigation-project";
    const secondId = "source-record-second";
    const second = { ...detail, record: { ...detail.record, source_record_id: secondId, source_item_id: secondId, source_display_id: "SRC-0006", requirement_display_id: "REQ-0006", display_label: "另一条" } };
    const outline = { ...props.outline, records: [...props.outline.records, second.record] };
    vi.spyOn(api, "sourceRequirementRecord").mockImplementation(async (_p, _d, id) => id === secondId ? second : detail);
    const view = render(<SourceItemLens {...props} outline={outline} />);
    await screen.findByRole("img");
    fireEvent.click(screen.getByRole("button", { name: "提出修改" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "请核对上限和原图。" } });
    view.rerender(<SourceItemLens {...props} outline={outline} selectedSourceItemId={secondId} />);
    await screen.findByText("另一条");
    fireEvent.click(screen.getByRole("button", { name: "提出修改" }));
    expect(screen.getByRole("textbox")).toHaveValue("");
    view.rerender(<SourceItemLens {...props} outline={outline} />);
    await screen.findByRole("heading", { name: "尺寸图" });
    fireEvent.click(screen.getByRole("button", { name: "继续写反馈" }));
    expect(screen.getByRole("textbox")).toHaveValue("请核对上限和原图。");
    view.unmount();
    const mounted = render(<SourceItemLens {...props} outline={outline} />);
    await screen.findByRole("img");
    fireEvent.click(screen.getByRole("button", { name: "继续写反馈" }));
    expect(screen.getByRole("textbox")).toHaveValue("请核对上限和原图。");
    const newerSha = "d".repeat(64);
    vi.mocked(api.sourceRequirementRecord).mockResolvedValue({ ...detail, baseline_artifact_sha256: newerSha });
    mounted.rerender(<SourceItemLens {...props} outline={{ ...outline, baseline: { ...outline.baseline, artifact_sha256: newerSha, revision: 4 } }} />);
    await screen.findByRole("img");
    fireEvent.click(screen.getByRole("button", { name: "提出修改" }));
    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(screen.getByText("其他版本的未提交草稿（1）")).toBeInTheDocument();
  });

  it("does not erase newer draft text when an earlier save finishes", async () => {
    const { detail, props } = fixture(); props.projectId = "draft-late-save-project";
    detail.record.logical_table_scope_id = "table";
    props.outline.records[0].logical_table_scope_id = "table";
    vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(detail);
    let resolve: (value: unknown) => void = () => {};
    vi.spyOn(api, "submitSourceRequirementFeedback").mockImplementation(() => new Promise(done => { resolve = done as (value: unknown) => void; }));
    render(<SourceItemLens {...props} />); await screen.findByRole("img");
    fireEvent.click(screen.getByRole("button", { name: "提出修改" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "已提交的第一段" } });
    fireEvent.click(screen.getByRole("button", { name: "保存反馈" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "继续补写的新意见" } });
    resolve({ feedback: { feedback_id: "saved-1", message: "已提交的第一段", status: "PENDING_AGENT", created_at_utc: "2026-09-07T00:00:00Z" }, delivery_message: "已保存，待处理" });
    await waitFor(() => expect(props.onFeedbackSaved).toHaveBeenCalled());
    expect(screen.getByRole("textbox")).toHaveValue("继续补写的新意见");
    expect(screen.getByText("反馈处理记录（1）")).toBeInTheDocument();
  });

  it("shows the original image and a separately labelled Agent reading", async () => {
    const { detail, props } = fixture();
    vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(detail);
    render(<SourceItemLens {...props} />);
    const image = await screen.findByRole("img");
    expect(image).toHaveAttribute("src", expect.stringContaining(`source_sha256=${sha}`));
    expect(screen.getByRole("region", { name: "Agent 图片释读" })).toHaveTextContent("宽度上限18 mm");
    expect(screen.queryByRole("region", { name: "当前原文条目的逐字内容" })).not.toBeInTheDocument();
    fireEvent.error(image);
    expect(screen.getByText("原图暂时无法加载，可定位到原 PDF 核查。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "定位到原页" }));
    expect(props.onPageChange).toHaveBeenCalledWith(1);
  });

  it("rejects an API detail from a different source version", async () => {
    const { detail, props } = fixture();
    detail.baseline_artifact_sha256 = "d".repeat(64);
    vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(detail);
    render(<SourceItemLens {...props} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("原文版本已更新");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("reloads an unchanged SRC identity when its baseline version changes", async () => {
    const { detail, props } = fixture();
    const spy = vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(detail);
    const view = render(<SourceItemLens {...props} />);
    await screen.findByRole("img");
    const newer = { ...props.outline, baseline: { ...props.outline.baseline, artifact_sha256: "d".repeat(64), revision: 4 } };
    view.rerender(<SourceItemLens {...props} outline={newer} />);
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("alert")).toHaveTextContent("原文版本已更新");
  });

  it("uses a proved reading projection while retaining its original paint text", async () => {
    const { detail, props } = fixture();
    detail.record.image_bindings = [];
    detail.record.visual_interpretation = undefined;
    detail.record.text_bindings = [{ content_id: "content", text: "ABC\nABC\nABC\nABC", page: 1,
      bbox: [20, 30, 180, 90], source_bounds: [0, 0, 595, 842],
      reading_projection: { display_text: "ABC", raw_text_sha256: "e".repeat(64) } }] as unknown as typeof detail.record.text_bindings;
    vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(detail);
    render(<SourceItemLens {...props} />);
    const original = await screen.findByRole("region", { name: "当前原文条目的逐字内容" });
    expect(original).toHaveTextContent("已合并同位置叠印");
    expect(original.querySelector("article > div > p")?.textContent).toBe("ABC");
    expect(original.querySelector("details p")?.textContent).toBe("ABC\nABC\nABC\nABC");
    expect(detail.record.text_bindings[0].text).toBe("ABC\nABC\nABC\nABC");
  });
});


it("opens a matching derived unit and visits every match before the next source item", async () => {
  const {detail,props}=fixture();vi.spyOn(api,"sourceRequirementRecord").mockResolvedValue(detail);
  const next=vi.fn();
  const reasoning={state:{source_sha256:sha,baseline_sha256:baselineSha,notices:[],active_jobs:[],
    decomposition:{records:{[sourceId]:[1,2,3].map(i=>({singular_requirement_id:`u${i}`,requirement_display_id:`REQ-0005-S0${i}`,text:`单元${i}`,source_excerpts:[]}))}},
    assessments:{records:{u1:{decision:"COMPLIANT",reason:"首项",evidence:[]},u2:{decision:"NEEDS_CLARIFICATION",reason:"第二项",evidence:[]},u3:{decision:"NEEDS_CLARIFICATION",reason:"第三项",evidence:[]}}}},
    run:vi.fn(),saveEngineerResponse:vi.fn()} as unknown as NonNullable<Parameters<typeof SourceItemLens>[0]["reasoning"]>;
  render(<SourceItemLens {...props} analysisMode analysisUnitIds={["u2","u3"]} reasoning={reasoning} onNextReviewItem={next}/>);
  expect(await screen.findByText("REQ-0005-S02 · 待澄清")).toBeVisible();
  expect(screen.queryByRole("button",{name:"REQ-0005-S01"})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"下个分析单元"}));
  expect(screen.getByText("REQ-0005-S03 · 待澄清")).toBeVisible();expect(next).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"下一项"}));expect(next).toHaveBeenCalledOnce();
});

it("keeps the complete parent distinct from a selected derived row and saves against that unit", async () => {
  const {detail,props}=fixture();vi.spyOn(api,"sourceRequirementRecord").mockResolvedValue(detail);
  const save=vi.fn().mockResolvedValue(undefined),select=vi.fn();
  const children=[1,2].map(i=>({singular_requirement_id:`unit-${i}`,requirement_display_id:`REQ-0005-S0${i}`,text:`派生正文${i}`,source_excerpts:[]}));
  const reasoning={scopeKey:"derived-row-test",state:{source_sha256:sha,baseline_sha256:baselineSha,notices:[],active_jobs:[],decomposition:{records:{[sourceId]:children}},assessments:{records:{}},
    engineer_responses:{available:true,response_revision:0,records:{},history:[],units:Object.fromEntries(children.map(child=>[child.singular_requirement_id,{unit_id:child.singular_requirement_id,unit_context_sha256:"context"}]))}},
    run:vi.fn(),saveEngineerResponse:save} as unknown as NonNullable<Parameters<typeof SourceItemLens>[0]["reasoning"]>;
  const page=render(<SourceItemLens {...props} tableView analysisMode reasoning={reasoning} onAnalysisUnitChange={select} onNextReviewItem={vi.fn()}/>);
  expect(await screen.findByRole("region",{name:"父条款与派生项"})).toBeVisible();
  expect(screen.queryByRole("textbox",{name:"判断理由"})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"下个分析单元"}));expect(select).toHaveBeenCalledWith("unit-1");
  page.rerender(<SourceItemLens {...props} tableView analysisMode reasoning={reasoning} selectedDerivedUnitId="unit-2" onAnalysisUnitChange={select}/>);
  expect(screen.getByRole("heading",{name:"派生正文2"})).toBeVisible();
  expect(screen.getByText("REQ-0005-S02 · 派生自 REQ-0005 · 来源 SRC-0005")).toBeVisible();
  fireEvent.change(screen.getByRole("textbox",{name:"判断理由"}),{target:{value:"仅针对第二项的工作意见"}});
  fireEvent.click(screen.getByRole("button",{name:"保存为本版工作意见"}));
  await waitFor(()=>expect(save).toHaveBeenCalledWith("unit-2",expect.objectContaining({reason:"仅针对第二项的工作意见"})));
  expect(screen.getByRole("button",{name:"修改父条款"})).toBeVisible();
});

it("keeps derived source review separate from response editing and offers a scoped transition", async () => {
  const { detail, props } = fixture();
  vi.spyOn(api, "sourceRequirementRecord").mockResolvedValue(detail);
  const changePurpose = vi.fn(), run = vi.fn();
  const children = [1, 2].map(i => ({ singular_requirement_id: `unit-${i}`, requirement_display_id: `REQ-0005-S0${i}`, text: `派生正文${i}`, source_excerpts: [] }));
  const reasoning = { scopeKey: "purpose-boundary", state: { source_sha256: sha, baseline_sha256: baselineSha, notices: [], active_jobs: [],
    decomposition: { records: { [sourceId]: children } }, assessments: { records: {} },
    engineer_responses: { available: true, response_revision: 0, records: {}, history: [], units: { "unit-2": { unit_id: "unit-2", unit_context_sha256: "context" } } } },
    run, saveEngineerResponse: vi.fn() } as unknown as NonNullable<Parameters<typeof SourceItemLens>[0]["reasoning"]>;
  const view = render(<SourceItemLens {...props} tableView selectedDerivedUnitId="unit-2" reasoning={reasoning} onChangePurpose={changePurpose} />);
  expect(await screen.findByRole("heading", { name: "派生正文2" })).toBeVisible();
  expect(screen.getByRole("img")).toBeVisible();
  expect(screen.queryByRole("textbox", { name: "判断理由" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "分析父条款派生项" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "检查父条款拆分" })).toBeVisible();
  expect(screen.queryByText(/^REQ-0005-S01/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "评审此项响应" }));
  expect(changePurpose).toHaveBeenCalledWith("analysis");
  expect(run).not.toHaveBeenCalled();

  view.rerender(<SourceItemLens {...props} tableView analysisMode selectedDerivedUnitId="unit-2" reasoning={reasoning} onChangePurpose={changePurpose} />);
  expect(screen.getByRole("textbox", { name: "判断理由" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "检查父条款拆分" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "分析父条款派生项" }));
  expect(run).toHaveBeenCalledWith("analysis", sourceId);
  fireEvent.click(screen.getByRole("button", { name: "修改父条款" }));
  expect(changePurpose).toHaveBeenLastCalledWith("review");
});

it("uses the external version for details and drafts, saving feedback without starting a native Agent", async () => {
  const {detail,props}=fixture();props.projectId="plugin-drafts";
  const nativeRead=vi.spyOn(api,"sourceRequirementRecord"), nativeSave=vi.spyOn(api,"submitSourceRequirementFeedback"), nativeStart=vi.spyOn(api,"startSourceItemScopeRevisionRun");
  const external = { revision_id:"e".repeat(64), loadRecord:vi.fn(async()=>detail), saveFeedback:vi.fn(async()=>{}) } as unknown as NonNullable<Parameters<typeof SourceItemLens>[0]["sourceReader"]>;
  const view=render(<SourceItemLens {...props} sourceReader={external}/>);
  await screen.findByRole('img');fireEvent.click(screen.getByRole('button',{name:'提出修改'}));
  fireEvent.change(screen.getByRole('textbox'),{target:{value:'仍针对插件旧版本'}});
  const newer={...external,revision_id:'f'.repeat(64)};
  view.rerender(<SourceItemLens {...props} sourceReader={newer}/>);
  await screen.findByRole('img');fireEvent.click(screen.getByRole('button',{name:'提出修改'}));
  expect(screen.getByRole('textbox')).toHaveValue('');
  view.rerender(<SourceItemLens {...props} sourceReader={external}/>);
  await screen.findByRole('img');fireEvent.click(screen.getByRole('button',{name:'继续写反馈'}));
  expect(screen.getByRole('textbox')).toHaveValue('仍针对插件旧版本');
  fireEvent.click(screen.getByRole('button',{name:'保存反馈'}));
  await waitFor(()=>expect(external.saveFeedback).toHaveBeenCalled());
  expect(nativeRead).not.toHaveBeenCalled();expect(nativeSave).not.toHaveBeenCalled();expect(nativeStart).not.toHaveBeenCalled();
});
it("opens new plugin feedback in the shared composer while preserving an existing legacy item draft", async () => {
  const { detail, props } = fixture(); props.projectId = "shared-feedback-project";
  const show = vi.fn();
  const context = { producer: "EXTERNAL_PLUGIN" as const, bind: () => () => {}, task: null, show, openStage: vi.fn(), historyHost: null };
  const reader = { revision_id: "9".repeat(64), loadRecord: vi.fn(async () => detail), saveFeedback: vi.fn(async () => {}) } as unknown as NonNullable<Parameters<typeof SourceItemLens>[0]["sourceReader"]>;
  let page = render(<DocumentAgentContext.Provider value={context}><SourceItemLens {...props} sourceReader={reader}/></DocumentAgentContext.Provider>);
  await screen.findByRole("img"); fireEvent.click(screen.getByRole("button", { name: "提出修改" }));
  expect(show).toHaveBeenCalledWith("", "selected"); expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(reader.saveFeedback).not.toHaveBeenCalled();
  page.unmount(); show.mockClear();
  page = render(<SourceItemLens {...props} sourceReader={reader}/>);
  await screen.findByRole("img"); fireEvent.click(screen.getByRole("button", { name: "提出修改" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "已有的旧条款草稿" } }); page.unmount();
  render(<DocumentAgentContext.Provider value={context}><SourceItemLens {...props} sourceReader={reader}/></DocumentAgentContext.Provider>);
  await screen.findByRole("img"); fireEvent.click(screen.getByRole("button", { name: "继续写反馈" }));
  expect(screen.getByRole("textbox")).toHaveValue("已有的旧条款草稿"); expect(show).not.toHaveBeenCalled();
});

it("retains explicitly linked source conditions and opens their complete original item", async () => {
  const {detail,props}=fixture();
  detail.record.context_source_items=[{source_item_id:'condition',source_display_id:'SRC-0001',label:'Operating conditions',pages:[1],text_bindings:[{content_id:'c',text:'Supply 12 V at 23 C.',page:1,start_offset:0} as never]}];
  vi.spyOn(api,'sourceRequirementRecord').mockResolvedValue(detail);
  const select=vi.fn();render(<SourceItemLens {...props} onSelectContext={select}/>);
  fireEvent.click(await screen.findByText('关联原文与条件 · 1 项'));
  expect(screen.getByText('Supply 12 V at 23 C.')).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:'查看完整关联条目'}));expect(select).toHaveBeenCalledWith('condition');
});
