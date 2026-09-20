import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DocumentWorkSurface } from "./DocumentWorkSurface";
import { useDocumentAgent } from "./DocumentAgentContext";
import type { PluginState } from "./documentWorkProvider";
import type { SourceWorkbenchReader } from "../extraction/SourceWorkbenchReader";
import type { WorkspaceRoute } from "./route";
import { sourceFeedbackDraftKey } from "../extraction/sourceItemDrafts";
import { storeReadingHandoffIntent } from "./readingHandoff";
let current: PluginState;
let route: WorkspaceRoute;
let calls: { url: string; body: Record<string, unknown> }[];
let displayed: SourceWorkbenchReader;
let failWrite = false;
const updateRoute = vi.fn((patch: Partial<WorkspaceRoute>) => { route={...route,...patch}; });
vi.mock("./WorkspaceContext", () => ({useWorkspace: () => ({route,updateRoute,selectedDocument:{source_sha256:current.source_sha256,file_name:"合成.pdf"},refreshDocument:vi.fn()})}));
vi.mock("./documentWorkSummary", async original => ({...await original<object>(),useDocumentWorkSummary:()=>({delivery:null,task:null,deliveryError:"",taskError:""})}));
vi.mock("../extraction/SourceItemAgentLoop",()=>({SourceItemAgentLoop:()=> <div>native-controller</div>}));
vi.mock("../../shared/api/client", async original => ({...await original<object>(),request:vi.fn(async (url: string, options?: {body?:string}) => {
  if(url.includes("/reader")) return {revision_id:new URL('http://local'+url).searchParams.get('revision'),source_sha256:current.source_sha256,outline:{baseline:{artifact_sha256:"basis"},summary:{source_item_count:1,requirement_count:1,non_requirement_count:0}},reference:{},tables:{},reasoning:{analysis_unit_count:1},review:null};
  if(options?.body) {calls.push({url,body:JSON.parse(options.body)});if(failWrite){failWrite=false;throw new Error("network interrupted");}}
  return structuredClone(current);
})}));
vi.mock("../extraction/NativeStructureReferenceDock",()=>({NativeStructureReferenceDock:({sourceReader,mode}:{sourceReader:SourceWorkbenchReader;mode:string})=>{
  const context=useDocumentAgent(); displayed=sourceReader;
  useEffect(()=>context?.bind({sourceSha256:sourceReader.source_sha256,baselineSha256:"basis",revisionId:sourceReader.revision_id,selectedSourceItemId:"s1",selectedNodeId:"node",selectedLabel:"REQ-0001",onUpdated:()=>{}}),[context?.bind,sourceReader.revision_id]);
  return <div data-testid="incumbent-workbench">{mode}<button type="button" onClick={() => context?.show("", "selected")}>提出范围修改</button></div>;
}}));
beforeEach(()=>{
  calls=[];failWrite=false;updateRoute.mockClear();localStorage.clear();sessionStorage.clear();
  current={session_id:"plugin-"+"a".repeat(32),source_sha256:"a".repeat(64),revision_id:"1".repeat(64),revision_number:1,
    stages:[{stage:"MAP",artifact_sha256:"m",count:2},{stage:"ITEMS",artifact_sha256:"b",count:1}],
    versions:[{revision_id:"1".repeat(64),number:1,stages:[{stage:"MAP",artifact_sha256:"m",count:2},{stage:"ITEMS",artifact_sha256:"b",count:1}]}],map_confirmation_required:false,feedback:[],
    execution:{status:"UNREPORTED"},capabilities:{save_feedback:{supported:true,enabled:true,effect:"FEEDBACK_SAVED"},confirm_map:{supported:true,enabled:true},export_draft:{supported:true,enabled:true}}};
  route={task:"extraction",mode:"organize",checkpoint:"list",projectId:"p",documentId:"d",requirementSetId:"",pluginSessionId:current.session_id};
});
afterEach(()=>{cleanup();vi.useRealTimers();});
function show(){return render(<DocumentWorkSurface><div>native-content</div></DocumentWorkSurface>);}
it("uses one document shell and a permanent external collaboration entry without native execution",async()=>{
  show();expect(await screen.findByTestId("incumbent-workbench")).toHaveTextContent("SOURCE_REQUIREMENTS");
  expect(screen.getAllByRole("region",{name:"文档任务与成果"})).toHaveLength(1);
  expect(screen.queryByText("native-controller")).not.toBeInTheDocument();expect(screen.queryByText("native-content")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"Agent 协作"}));
  expect(screen.getByText("外部运行状态未报告")).toBeVisible();
  fireEvent.change(screen.getByRole("textbox",{name:"给外部任务的修改意见"}),{target:{value:"修改当前原文范围"}});
  fireEvent.click(screen.getByRole("button",{name:"保存意见"}));
  await waitFor(()=>expect(calls).toHaveLength(1));expect(calls[0].url).toMatch(/\/feedback$/);
  expect(calls[0].body).toMatchObject({source_item_ids:["s1"],node_id:"node",expected_revision_id:"1".repeat(64)});
  expect(screen.getByText(/意见已保存到 v1/)).toBeVisible();expect(calls.every(call=>!call.url.includes("/loop"))).toBe(true);
});
it("pins versions through head updates and exports the viewed revision in the delivery surface",async()=>{
  vi.useFakeTimers();const page=show();await act(async()=>{await Promise.resolve();await Promise.resolve();});
  current={...current,revision_id:"2".repeat(64),revision_number:2,versions:[...current.versions,{revision_id:"2".repeat(64),number:2,stages:current.stages}]};
  await act(async()=>vi.advanceTimersByTimeAsync(3000));expect(displayed.revision_id).toBe("1".repeat(64));expect(screen.getByLabelText("核对版本")).toHaveValue("1".repeat(64));
  route={...route,task:"export"};page.rerender(<DocumentWorkSurface><div>native-content</div></DocumentWorkSurface>);
  expect(screen.getByRole("heading",{name:"交付当前核对版"})).toBeVisible();
  expect(screen.queryByTestId("incumbent-workbench")).not.toBeInTheDocument();
  expect(screen.getByRole("link",{name:"导出当前核对版 Excel"})).toHaveAttribute("href",expect.stringContaining("revision="+"1".repeat(64)));
  expect(screen.getByRole("button",{name:"Agent 协作"})).toBeInTheDocument();
});
it("reuses an uncertain feedback command after retry and remount",async()=>{
  let page=show();await screen.findByTestId("incumbent-workbench");failWrite=true;
  await act(async()=>{await expect(displayed.saveFeedback("同一份意见","node",["s1"])).rejects.toThrow("network interrupted");});
  const first=calls[0].body.command_id;page.unmount();page=show();await screen.findByTestId("incumbent-workbench");
  await act(async()=>displayed.saveFeedback("同一份意见","node",["s1"]));expect(calls[1].body.command_id).toBe(first);
});
it("does not enable unavailable external commands or call them as native operations",async()=>{
  current={...current,capabilities:{save_feedback:{supported:false,enabled:false,reason:"此连接暂不支持写入"}}};show();await screen.findByTestId("incumbent-workbench");
  fireEvent.click(screen.getByRole("button",{name:"Agent 协作"}));
  expect(screen.getByText("此连接暂不支持写入")).toBeVisible();expect(screen.getByRole("button",{name:"保存意见"})).toBeDisabled();
  await act(async()=>{await expect(displayed.saveFeedback("不应发送","node",["s1"])).rejects.toThrow("暂不支持写入");});expect(calls).toEqual([]);
});
it("opens a reading handoff in the document draft without submitting it or changing the pinned version", async () => {
  const key = sourceFeedbackDraftKey("p", "d", `external:${current.session_id}:document`, current.revision_id!);
  localStorage.setItem(key, JSON.stringify({ schema: 1, text: "待核对的阅读发现", revision: 1, updatedAt: "2026-09-12" }));
  storeReadingHandoffIntent({ projectId: "p", documentId: "d", sourceSha256: current.source_sha256,
    work: { producer: "EXTERNAL_PLUGIN", sessionId: current.session_id, revisionId: current.revision_id! }, url: "/workspace" });
  show();
  const input = await screen.findByRole("textbox", { name: "给外部任务的修改意见" });
  await waitFor(() => expect(input).toBeVisible());
  expect(input).toHaveValue("待核对的阅读发现");
  expect(screen.getByLabelText("修改范围")).toHaveValue("document");
  expect(screen.getByLabelText("核对版本")).toHaveValue(current.revision_id);
  expect(calls).toHaveLength(0);
});
it("keeps a source-only external task external and ends artifact loading honestly", async () => {
  current = { ...current, revision_id: null, revision_number: 0, versions: [], stages: [], capabilities: { save_feedback: { supported: true, enabled: false, reason: "当前任务尚无阶段成果。" } } };
  route = { ...route, checkpoint: "structure" };
  show();
  expect(await screen.findByText("尚无阶段成果")).toBeVisible();
  expect(screen.getByRole("button", { name: "阅读原件" })).toBeVisible();
  expect(screen.queryByLabelText("核对版本")).not.toBeInTheDocument();
  expect(screen.queryByText("native-controller")).not.toBeInTheDocument();
  expect(screen.queryByText("成果已保存")).not.toBeInTheDocument();
  expect(screen.queryByText(/正在查看历史版本/)).not.toBeInTheDocument();
  expect(calls).toHaveLength(0);
});
it("a scoped feedback shortcut selects the current item even after the composer was on the document scope", async () => {
  show(); await screen.findByTestId("incumbent-workbench");
  fireEvent.click(screen.getByRole("button", { name: "Agent 协作" }));
  fireEvent.change(screen.getByLabelText("修改范围"), { target: { value: "document" } });
  fireEvent.click(screen.getByRole("button", { name: "提出范围修改" }));
  expect(screen.getByLabelText("修改范围")).toHaveValue("selected");
  expect(calls).toHaveLength(0);
});
