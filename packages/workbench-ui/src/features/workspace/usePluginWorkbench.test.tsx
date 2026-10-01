import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import type { SourceWorkbenchReadModel } from "../extraction/SourceWorkbenchReader";
import type { EngineerResponseView } from "../extraction/EngineerOpinion";
import { usePluginWorkbench } from "./usePluginWorkbench";
import type { PluginState } from "./documentWorkProvider";
vi.mock("../../shared/api/client", async original => ({ ...await original<object>(), request: vi.fn() }));
const source = "a".repeat(64), session = "plugin-"+"b".repeat(32), first = "1".repeat(64), saved = "2".repeat(64), newer = "3".repeat(64);
const basis = "4".repeat(64), token = "5".repeat(64);
const choice = {action:"SET" as const,decision:"COMPLIANT" as const,reason:"明确工作意见",conditions:"",evidence_requests:""};
let state: PluginState, response: EngineerResponseView, writes: Array<Record<string,unknown>>;
function responseView(revision = first): EngineerResponseView {
  return { available:true, source_sha256:source,session_id:session,revision_id:revision,current_revision_id:revision,version:token,baseline_sha256:basis,
    response_revision:0,response_sha256:"",records:{},history:[],units:{u:{unit_id:"u",source_item_id:"src",requirement_display_id:"REQ-0001",unit_context_sha256:"6".repeat(64)}} };
}
function model(revision: string) {
  return {revision_id:revision,source_sha256:source,reference:{artifact_id:"map"},outline:{baseline:{artifact_sha256:basis,revision:1}},reasoning:{engineer_responses:responseView(revision)},review:null,tables:{}} as unknown as SourceWorkbenchReadModel;
}
const options = {projectId:"p",documentId:"d",sourceSha256:source,sessionId:session,onRevision:vi.fn(),pollIntervalMs:0};
beforeEach(()=>{
  writes=[];localStorage.clear();vi.resetAllMocks();
  state={session_id:session,source_sha256:source,revision_id:first,revision_number:1,map_confirmation_required:false,feedback:[],stages:[],versions:[{revision_id:first,number:1,stages:[]}],
    capabilities:{save_engineer_response:{supported:true,enabled:true},save_delivery_version:{supported:true,enabled:true}}};
  response=responseView(saved);
  vi.mocked(request).mockImplementation(async(url,init)=>{
    if (init?.method === "POST") { writes.push(JSON.parse(String(init.body))); return response as never; }
    if (url.includes('/reader')) return model(new URL(url,'http://local').searchParams.get('revision')!) as never;
    if (url.includes('/engineer-responses')) return responseView(new URL(url,'http://local').searchParams.get('revision')!) as never;
    return structuredClone(state) as never;
  });
});
afterEach(()=>cleanup());
it("sends session/result/response CAS and follows the exact returned revision rather than a newer head",async()=>{
  const page=renderHook(()=>usePluginWorkbench(options));
  await waitFor(()=>expect(page.result.current.reader).toBeDefined());
  const draftScope=page.result.current.reader!.responseScope;
  response={...response,current_revision_id:newer,response_revision:1};
  state={...state,revision_id:newer,revision_number:3,versions:[...state.versions,{revision_id:saved,number:2,stages:[]},{revision_id:newer,number:3,stages:[]}]};
  await act(async()=>page.result.current.reader!.saveEngineerResponse!("u",choice));
  await waitFor(()=>expect(page.result.current.pinned).toBe(saved));
  expect(options.onRevision).toHaveBeenLastCalledWith(saved);
  expect(page.result.current.reader!.responseScope).toBe(draftScope);
  expect(writes[0]).toMatchObject({expected_source_sha256:source,expected_revision_id:first,expected_version:token,expected_baseline_sha256:basis,expected_response_revision:0,
    decisions:[{unit_id:"u",expected_unit_context_sha256:"6".repeat(64),...choice}]});
  expect(vi.mocked(request).mock.calls.every(([url])=>url.includes(`/plugin-workbench/${session}`))).toBe(true);
});
it("blocks response and delivery writes in unmanaged and historical panels",async()=>{
  const page=renderHook(({readOnly,revisionId})=>usePluginWorkbench({...options,readOnly,revisionId}),{initialProps:{readOnly:true,revisionId:first}});
  await waitFor(()=>expect(page.result.current.reader).toBeDefined());
  await expect(page.result.current.reader!.saveEngineerResponse!("u",choice)).rejects.toThrow("只读");
  expect(page.result.current.deliveryTransport.writable).toBe(false);
  state={...state,revision_id:newer,versions:[...state.versions,{revision_id:newer,number:2,stages:[]}]};
  page.rerender({readOnly:false,revisionId:first});
  act(()=>page.result.current.refresh());
  await waitFor(()=>expect(page.result.current.current).toBe(false));
  await expect(page.result.current.reader!.saveEngineerResponse!("u",choice)).rejects.toThrow("历史版本");
  expect(page.result.current.deliveryTransport.writable).toBe(false);expect(writes).toEqual([]);
});
it("retains an uncertain response command across remount and retry",async()=>{
  const implementation=vi.mocked(request).getMockImplementation()!;
  let fail=true;
  vi.mocked(request).mockImplementation(async(url,init)=>{
    if(init?.method==='POST'&&fail){fail=false;writes.push(JSON.parse(String(init.body)));throw new Error('interrupted');}
    return implementation(url,init);
  });
  let page=renderHook(()=>usePluginWorkbench(options));
  await waitFor(()=>expect(page.result.current.reader).toBeDefined());
  await act(async()=>{await expect(page.result.current.reader!.saveEngineerResponse!("u",choice)).rejects.toThrow('interrupted');});
  const id=writes[0].command_id;page.unmount();page=renderHook(()=>usePluginWorkbench(options));
  await waitFor(()=>expect(page.result.current.reader).toBeDefined());
  await act(async()=>page.result.current.reader!.saveEngineerResponse!("u",choice));
  expect(writes[1].command_id).toBe(id);
});
it("a late response cannot take over a newly selected version",async()=>{
  state={...state,versions:[...state.versions,{revision_id:newer,number:2,stages:[]}]};
  const implementation=vi.mocked(request).getMockImplementation()!;
  let resolve: (view: EngineerResponseView)=>void=()=>{};
  vi.mocked(request).mockImplementation((url,init)=>init?.method==='POST'?new Promise(done=>{resolve=done as typeof resolve;}):implementation(url,init));
  const page=renderHook(({revisionId})=>usePluginWorkbench({...options,revisionId}),{initialProps:{revisionId:first}});
  await waitFor(()=>expect(page.result.current.reader).toBeDefined());
  let pending: Promise<void>;
  act(()=>{pending=page.result.current.reader!.saveEngineerResponse!("u",choice);});
  page.rerender({revisionId:newer});
  await waitFor(()=>expect(page.result.current.reader?.revision_id).toBe(newer));
  await act(async()=>{resolve(response);await pending!;});
  expect(page.result.current.pinned).toBe(newer);expect(options.onRevision).not.toHaveBeenCalledWith(saved);
});
it("rejects a wrong-session response receipt without switching the selected result",async()=>{
  response={...response,session_id:'plugin-'+"c".repeat(32)};
  const page=renderHook(()=>usePluginWorkbench(options));
  await waitFor(()=>expect(page.result.current.reader).toBeDefined());
  await act(async()=>{await expect(page.result.current.reader!.saveEngineerResponse!("u",choice)).rejects.toThrow("不一致");});
  expect(page.result.current.pinned).toBe(first);
});
