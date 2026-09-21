import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ReqPilotXReadonlyPanel } from "./ReqPilotXReadonlyPanel";
import { createReadonlyHttpClient, matchingExecution, type HostExecutionSnapshot, type ReadonlyWorkbenchHost, type ReadonlyWorkSelection, type SessionBinding } from "./readonlyHost";
import { useDocumentAgent } from "../features/workspace/DocumentAgentContext";
import { usePluginWorkbench } from "../features/workspace/usePluginWorkbench";
import type { SourceWorkbenchReader } from "../features/extraction/SourceWorkbenchReader";
import type { PluginState } from "../features/workspace/documentWorkProvider";

const bindingHarness = vi.hoisted(()=>({skip:false}));
beforeEach(()=>{bindingHarness.skip=false;});
vi.mock("../features/extraction/NativeStructureReferenceDock", () => ({ NativeStructureReferenceDock: ({ sourceReader }: { sourceReader: SourceWorkbenchReader }) => {
  const agent = useDocumentAgent();
  useEffect(() => bindingHarness.skip ? undefined : agent?.bind({ sourceSha256: sourceReader.source_sha256, baselineSha256: "basis", revisionId: sourceReader.revision_id,
    selectedSourceItemId: "source", selectedNodeId: "node", selectedDerivedUnitId: "child", onUpdated: () => {} }), [agent?.bind, sourceReader.revision_id]);
  return <div>共用业务工作面<button type="button" disabled={!agent?.collaborationAvailable} onClick={() => agent?.show("", "selected")}>讨论当前条目</button></div>;
} }));
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); });
const selection: ReadonlyWorkSelection = { projectId: "p", documentId: "d", sourceSha256: "a".repeat(64), workRef: `plugin-${"b".repeat(32)}`, revisionId: "1".repeat(64), view: "items" };
const binding: SessionBinding = { ...selection, hostId: "pair", workspaceId: "files", sessionId: "conversation" };
function setup() {
  let execution: HostExecutionSnapshot | null = null;
  const listeners = new Set<() => void>();
  let workUpdate: Parameters<NonNullable<ReadonlyWorkbenchHost["subscribeWorkUpdates"]>>[0] | undefined;
  const state = { session_id: selection.workRef, source_sha256: selection.sourceSha256, revision_id: "2".repeat(64), revision_number: 2,
    versions: [1,2].map(n => ({ revision_id: String(n).repeat(64), number: n, stages: [{stage:"MAP",artifact_sha256:"m",count:2},{stage:"ITEMS",artifact_sha256:"i",count:1}] })),
    stages: [], feedback: [], map_confirmation_required: true, capabilities: { save_feedback: { supported: true, enabled: true }, confirm_map: { supported: true, enabled: true } } } as PluginState;
  const request = vi.fn(async (path: string) => path.includes("/reader?") ? { revision_id: new URL(path,"https://service.test").searchParams.get("revision"), source_sha256: selection.sourceSha256,
    outline: { summary: { source_item_count: 1, requirement_count: 1, non_requirement_count: 0 } }, reference: {}, tables: {}, reasoning: null, review: null } : state);
  const host: ReadonlyWorkbenchHost = { id: "pair-panel", label: "Pair", client: { id: "service", request: request as never },
    resourceUrl: path => `https://service.test${path}`, pdfViewerUrl: () => "https://viewer.test/pdf-viewer", sessionBinding: binding,
    openSession: vi.fn(), onSelectionChange: vi.fn(), execution: { getSnapshot: () => execution, subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener); }; } },
    subscribeWorkUpdates: listener => { workUpdate = listener; return () => { workUpdate = undefined; }; } };
  return { host, request, listeners, state, update: (next: HostExecutionSnapshot) => { execution = next; listeners.forEach(listener => listener()); }, event: (event: Parameters<NonNullable<typeof workUpdate>>[0]) => workUpdate?.(event) };
}
it("mounts the existing business view without global navigation, model setup or business writes", async () => {
  const {host, request} = setup(); const before = location.href;
  render(<ReqPilotXReadonlyPanel host={host} selection={selection} documentName="source.pdf" />);
  await screen.findByText("共用业务工作面");
  expect(screen.queryByRole("navigation", {name:"工作台导航"})).not.toBeInTheDocument();
  expect(screen.queryByText("确认此结构")).not.toBeInTheDocument();
  expect(request.mock.calls.every(([path]) => path.includes("plugin-workbench"))).toBe(true);
  fireEvent.click(screen.getByRole("button",{name:"响应与证据"}));
  expect(location.href).toBe(before);
  expect(host.onSelectionChange).toHaveBeenLastCalledWith({...selection,view:"analysis"});
  expect(host.openSession).not.toHaveBeenCalled();
});
it("opens only the explicitly bound session with the selected source and derived unit", async () => {
  const {host} = setup(); render(<ReqPilotXReadonlyPanel host={host} selection={selection} documentName="source.pdf" />);
  await screen.findByText("共用业务工作面");
  fireEvent.click(screen.getByRole("button",{name:"讨论当前条目"}));
  await waitFor(()=>expect(host.openSession).toHaveBeenCalledWith(binding,{...selection,scope:"selected",nodeId:"node",sourceItemId:"source",derivedUnitId:"child"}));
});
it("refuses unrelated session bindings and never uses their reported progress", async () => {
  const {host,update} = setup(); host.sessionBinding = {...binding,workRef:`plugin-${"c".repeat(32)}`};
  render(<ReqPilotXReadonlyPanel host={host} selection={selection} documentName="source.pdf" />);
  await screen.findByText("共用业务工作面");
  act(()=>update({...binding,runId:"run",status:"RUNNING",observedAt:"2026-09-14T10:00:00Z",connected:true}));
  expect(screen.getByText("会话未绑定当前工作")).toBeInTheDocument();
  expect(screen.getByRole("button",{name:"打开关联Pair会话"})).toBeDisabled();
  expect(screen.queryByText("会话正在处理")).not.toBeInTheDocument();
});
it("does not broaden a selected-item request to the whole document while its context is unavailable",async()=>{
  bindingHarness.skip=true;const {host}=setup();render(<ReqPilotXReadonlyPanel host={host} selection={selection} documentName="source.pdf"/>);
  await screen.findByText("共用业务工作面");fireEvent.click(screen.getByRole("button",{name:"讨论当前条目"}));
  expect(screen.getByText("当前选择的来源尚未就绪，请稍后重试。")).toBeInTheDocument();expect(host.openSession).not.toHaveBeenCalled();
});
it("separates actual host progress from saved revisions and disconnects subscriptions on unmount", async () => {
  const {host,update,request,listeners} = setup(); const view = render(<ReqPilotXReadonlyPanel host={host} selection={selection} documentName="source.pdf" />);
  await screen.findByText("共用业务工作面"); const reads = request.mock.calls.length;
  expect(screen.getByText("宿主尚未报告当前工作的运行状态")).toBeInTheDocument();
  act(()=>update({...binding,runId:"run",status:"RUNNING",observedAt:"2026-09-14T10:00:00Z",connected:true,activityLabel:"读取第 2 页原文"}));
  expect(screen.getByText("会话正在处理")).toBeInTheDocument(); expect(screen.getByText("读取第 2 页原文")).toBeInTheDocument();
  expect(request).toHaveBeenCalledTimes(reads);
  act(()=>update({...binding,runId:"run",status:"RUNNING",observedAt:"2026-09-14T10:00:00Z",connected:false}));
  expect(screen.getByText("运行状态连接中断")).toBeInTheDocument();
  expect(screen.getByRole("combobox",{name:"核对版本"})).toHaveValue(selection.revisionId);
  view.unmount(); expect(listeners.size).toBe(0);
});
it("ignores another work's updates and preserves the pinned version when its own head advances", async () => {
  const {host,event,request} = setup(); render(<ReqPilotXReadonlyPanel host={host} selection={selection} documentName="source.pdf" />);
  await screen.findByText("共用业务工作面"); const reads = request.mock.calls.length;
  act(()=>event({...selection,documentId:"other",revisionId:"2".repeat(64)})); expect(request).toHaveBeenCalledTimes(reads);
  act(()=>event({...selection,revisionId:"2".repeat(64)}));
  await waitFor(()=>expect(request.mock.calls.length).toBeGreaterThan(reads));
  expect(screen.getByRole("combobox",{name:"核对版本"})).toHaveValue(selection.revisionId);
  expect(host.onSelectionChange).not.toHaveBeenCalled();
});
it("guards the readonly provider even when a caller invokes a mutation directly", async () => {
  const {host,request} = setup();
  const {result} = renderHook(()=>usePluginWorkbench({projectId:selection.projectId,documentId:selection.documentId,sessionId:selection.workRef,sourceSha256:selection.sourceSha256,revisionId:selection.revisionId,readClient:host.client,readOnly:true,pollIntervalMs:0,onRevision:vi.fn()}));
  await waitFor(()=>expect(result.current.reader).toBeTruthy()); const count = request.mock.calls.length;
  await expect(result.current.saveFeedback("test","node",["source"])).rejects.toThrow("只读");
  expect(request).toHaveBeenCalledTimes(count);
});
it("returns to structure when selecting a version that only contains Map",async()=>{
  const {host,state}=setup();state.versions[0].stages=state.versions[0].stages.filter(stage=>stage.stage==="MAP");
  render(<ReqPilotXReadonlyPanel host={host} selection={{...selection,revisionId:"2".repeat(64)}} documentName="source.pdf"/>);
  await screen.findByText("共用业务工作面");fireEvent.change(screen.getByRole("combobox",{name:"核对版本"}),{target:{value:"1".repeat(64)}});
  expect(host.onSelectionChange).toHaveBeenLastCalledWith({...selection,view:"structure"});
  expect(screen.getByRole("button",{name:"原文结构"})).toHaveAttribute("aria-current","page");
});
it("keeps the shared HTTP parser while blocking writes at the transport boundary", async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ok:true}),{headers:{"Content-Type":"application/json"}}));
  const client = createReadonlyHttpClient("service",path=>`https://service.test${path}`,fetcher);
  await expect(client.request("/api/result")).resolves.toEqual({ok:true});
  await expect(client.request("/api/result",{method:"POST",body:"{}"})).rejects.toThrow("只读");
  expect(fetcher).toHaveBeenCalledTimes(1);
});
it("rejects malformed run observations",()=>{
  expect(matchingExecution(binding,{...binding,runId:"",status:"SUCCEEDED",observedAt:"bad",connected:true})).toBeNull();
});
