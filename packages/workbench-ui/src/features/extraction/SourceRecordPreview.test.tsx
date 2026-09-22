import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SourceRecordPreview, needsSourcePreview, type SourcePreviewReader } from "./SourceRecordPreview";
import type { SourceRequirementOutlineRecordV1, SourceRequirementRecordDetailV1 } from "../../shared/api/contracts";
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
const record = {source_record_id:"s",source_number:"2.1",source_form:"TEXT",source_numbers:[]} as unknown as SourceRequirementOutlineRecordV1;
const detail = {source_sha256:"source",baseline_artifact_sha256:"baseline",document_map_artifact_sha256:"map",record:{source_record_id:"s",text_bindings:[{text:"The module shall report a fault.\nKeep the original paragraph."}]}} as SourceRequirementRecordDetailV1;
function reader(): SourcePreviewReader { return {scope:"work:v1",sourceSha256:"source",baselineSha256:"baseline",mapSha256:"map",loadRecord:vi.fn(async()=>detail),cache:new Map()}; }
it("only uses confirmed source-number labels, excluding tables and real titles",()=>{
  expect(needsSourcePreview("2.1",record)).toBe(true); expect(needsSourcePreview("2.1 Electrical",record)).toBe(false);
  expect(needsSourcePreview("2.1",{...record,source_form:"TABLE_BRANCH"})).toBe(false);
  expect(needsSourcePreview("2.1",{...record,source_number:""})).toBe(false);
});
it("loads only when visible and displays the exact bound paragraph without changing its label",async()=>{
  let notify!: IntersectionObserverCallback;
  vi.stubGlobal("IntersectionObserver",class { constructor(callback:IntersectionObserverCallback){notify=callback;} observe(){} disconnect(){} });
  const source = reader(); const before=JSON.stringify(record);
  render(<SourceRecordPreview label="2.1" record={record} reader={source}/>);
  expect(source.loadRecord).not.toHaveBeenCalled();
  await act(async()=>notify([{isIntersecting:true}] as IntersectionObserverEntry[],{} as IntersectionObserver));
  expect(screen.getByText(/The module shall report a fault/)).toHaveTextContent("Keep the original paragraph.");
  expect(screen.getByText("原文摘录")).toBeInTheDocument(); expect(JSON.stringify(record)).toBe(before);
});
it("rejects mismatched source versions and retains the original label",async()=>{
  const source=reader(); source.loadRecord=vi.fn(async()=>({...detail,baseline_artifact_sha256:"other"}));
  render(<SourceRecordPreview label="2.1" record={record} reader={source}/>);
  await act(async()=>{});
  expect(screen.getByText("2.1")).toBeInTheDocument(); expect(screen.queryByText("原文摘录")).not.toBeInTheDocument();
});
it("skips a separately bound original number before choosing the body excerpt",async()=>{
  const source=reader(); source.loadRecord=vi.fn(async()=>({...detail,record:{...detail.record,text_bindings:[{text:"2.1"} as never,...detail.record.text_bindings]}}));
  render(<SourceRecordPreview label="2.1" record={record} reader={source}/>);
  await screen.findByText("原文摘录");
  expect(screen.getByText(/The module shall report a fault/)).toBeInTheDocument();
  expect(screen.queryByText("2.1")).not.toBeInTheDocument();
});
it("does not show an old preview after changing work or baseline",async()=>{
  const old=reader(); const view=render(<SourceRecordPreview label="2.1" record={record} reader={old}/>);
  await screen.findByText("原文摘录");
  const next={...reader(),scope:"work:v2",baselineSha256:"new",loadRecord:vi.fn(()=>new Promise<SourceRequirementRecordDetailV1>(()=>{}))};
  view.rerender(<SourceRecordPreview label="2.1" record={record} reader={next}/>);
  expect(screen.getByText("2.1")).toBeInTheDocument(); expect(screen.queryByText("原文摘录")).not.toBeInTheDocument();
});
