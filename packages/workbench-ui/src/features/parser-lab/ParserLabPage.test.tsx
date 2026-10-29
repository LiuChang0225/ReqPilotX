import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ParserLabPage } from "./ParserLabPage";
import { resetPageCache } from "./pageCache";
import type { Run } from "./contracts";

const run:Run={run_id:"a".repeat(32),filename:"synthetic-contract.pdf",source_sha256:"f".repeat(64),parser:"pymupdf",parser_version:"contract",
  adapter_id:"pdf-native",adapter_version:"contract",status:"PARTIAL",started_at:"2026-10-28T00:00:00Z",config:{ocr_language:"eng"},
  pages:[1,2].map(page_number=>({page_number,bbox:[0,0,400,500],rotation:0,cropbox:[0,0,400,500],render_artifact:`page-${page_number}`})),
  diagnostics:[],artifacts:[{id:"image-1",path:"source-assets/image.png",media_type:"image/png",byte_count:20,sha256:"f".repeat(64)}],
  source_ir:{assets:[],nodes:[
    {source_node_id:"text-1",kind:"paragraph",source_order:0,raw_text:"Complete first-page text",native_locator:{page_number:1,bbox:[20,20,200,40]}},
    {source_node_id:"table-1",kind:"table",source_order:1,raw_text:"",native_locator:{page_number:1,bbox:[20,100,200,140]},
      table_payload:{row_count:1,column_count:1,cells:[{cell_id:"cell-1",row:0,column:0,row_span:1,column_span:1,raw_text:"12 Nm",native_locator:{page_number:1,bbox:[20,100,200,140]}}]}},
    {source_node_id:"image-node",kind:"image",source_order:2,raw_text:"",asset_ref:"image-1",native_locator:{page_number:1,bbox:[20,150,200,200]}},
    {source_node_id:"text-2",kind:"paragraph",source_order:3,raw_text:"Complete second-page text",native_locator:{page_number:2,bbox:[20,50,200,70]}},
  ]}};
const second:Run={...run,run_id:"b".repeat(32),parser:"pdfplumber",source_ir:{assets:[],nodes:[
  {source_node_id:"other-text",kind:"paragraph",source_order:0,raw_text:"Other parser exclusive text",native_locator:{page_number:1,bbox:[20,20,200,40]}},
]}};
function response(value:unknown){return {ok:true,json:async()=>value,blob:async()=>new Blob(["contract image"],{type:"image/png"})} as Response;}
function fetchValue(url:string){return response(url.endsWith("/parsers")?[{id:"pymupdf",name:"PyMuPDF",version:"contract",availability:"READY"},
  {id:"tesseract",name:"Tesseract",version:"contract",availability:"READY"},{id:"docling",name:"Docling",version:null,availability:"UNSUPPORTED"}]
  :url.endsWith("/runs")?[run,second]:url.endsWith(second.run_id)?second:run);}
beforeEach(()=>{
  vi.stubGlobal("fetch",vi.fn(async(input)=>fetchValue(String(input))));
  let blobs=0;URL.createObjectURL=vi.fn(()=>`blob:contract-${++blobs}`);URL.revokeObjectURL=vi.fn();
  resetPageCache();Element.prototype.scrollIntoView=vi.fn();
});
afterEach(()=>{cleanup();resetPageCache();vi.unstubAllGlobals();vi.restoreAllMocks();});
async function ready(){render(<ParserLabPage/>);await screen.findByRole("region",{name:"pymupdf 原位还原"});await screen.findByAltText("原 PDF 第 1 页，旋转 0°");}

describe("Parser Lab visual inspection",()=>{
  it("switches all three views freely and reconstructs from text/table/assets only",async()=>{
    await ready();
    for(const name of ["PDF 对照","内容对照","PDF＋内容","内容对照","PDF 对照","PDF＋内容"]){
      fireEvent.click(screen.getByRole("button",{name}));expect(screen.getByRole("button",{name})).toHaveAttribute("aria-pressed","true");
    }
    const canvas=screen.getByRole("region",{name:"pymupdf 原位还原"});
    expect(within(canvas).getByText("Complete first-page text")).toBeVisible();
    expect(canvas.querySelectorAll("image")).toHaveLength(1);
    expect(canvas.querySelector("image")).toHaveAttribute("href",`/api/parser-lab/runs/${run.run_id}/artifacts/image-1`);
    expect(canvas.querySelector("img")).toBeNull();
    expect(canvas.textContent).toContain("并非原生字体恢复");
    fireEvent.click(screen.getByRole("button",{name:"结构化全文列表"}));
    expect(screen.getByText("Complete second-page text")).toBeVisible();expect(screen.getByText("12 Nm")).toBeVisible();
  });
  it("keeps page/zoom synchronized, supports thumbnails and keyboard, and reuses cached GETs",async()=>{
    await ready();await screen.findByAltText("第 2 页缩略图");
    const imageGets=()=>vi.mocked(fetch).mock.calls.filter(([url])=>String(url).includes("/artifacts/page-")).length;
    expect(imageGets()).toBe(2);
    fireEvent.click(screen.getByRole("button",{name:"下一页"}));
    await screen.findByAltText("原 PDF 第 2 页，旋转 0°");
    expect(screen.getByRole("region",{name:"pymupdf 原位还原"})).toHaveAttribute("data-page","2");
    expect(screen.queryByText("Complete first-page text")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"缩略图第 1 页"}));
    await screen.findByAltText("原 PDF 第 1 页，旋转 0°");
    const workspace=screen.getByLabelText(/解析核对工作面/);
    fireEvent.keyDown(workspace,{key:"End"});expect(screen.getByLabelText("页码跳转")).toHaveValue(2);
    fireEvent.keyDown(workspace,{key:"PageUp"});expect(screen.getByLabelText("页码跳转")).toHaveValue(1);
    fireEvent.change(screen.getByLabelText("页码跳转"),{target:{value:"2"}});
    fireEvent.change(screen.getByLabelText("同步缩放"),{target:{value:"1.5"}});
    await screen.findByAltText("原 PDF 第 2 页，旋转 0°");
    expect(screen.getByRole("region",{name:"pymupdf PDF"}).querySelector(".pl-paper")).toHaveStyle({width:"150%"});
    expect(screen.getByRole("region",{name:"pymupdf 原位还原"}).querySelector("svg")).toHaveStyle({width:"150%"});
    expect(imageGets()).toBe(2);expect(vi.mocked(fetch).mock.calls.every(([,init])=>!init?.method||init.method==="GET")).toBe(true);
  });
  it("locates cells bidirectionally between the PDF, white canvas and complete list",async()=>{
    await ready();
    fireEvent.click(screen.getByRole("button",{name:"定位 单元格 1,1"}));
    expect(screen.getByRole("button",{name:"还原定位单元格 1,1"})).toHaveAttribute("aria-pressed","true");
    fireEvent.click(screen.getByRole("button",{name:"结构化全文列表"}));
    expect(screen.getByText("12 Nm").closest("td")).toHaveClass("is-selected");
    fireEvent.click(screen.getByRole("button",{name:"4 · paragraph · p.2 ↗"}));
    await screen.findByAltText("原 PDF 第 2 页，旋转 0°");
    expect(screen.getByRole("button",{name:"定位 4 · 未分类"})).toHaveAttribute("aria-pressed","true");
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });
  it("does not expose old page images while an uncached page is pending",async()=>{
    let finish:((r:Response)=>void)|undefined;
    vi.mocked(fetch).mockImplementation(async(input)=>String(input).endsWith("/artifacts/page-2")?new Promise<Response>(r=>{finish=r;}):fetchValue(String(input)));
    await ready();fireEvent.click(screen.getByRole("button",{name:"下一页"}));
    expect(screen.queryByAltText("原 PDF 第 1 页，旋转 0°")).not.toBeInTheDocument();
    expect(screen.getByText("正在读取第 2 页缓存…")).toBeVisible();
    await act(async()=>{finish!(response(null));});await screen.findByAltText("原 PDF 第 2 页，旋转 0°");
  });
  it("ignores late profile responses and hides old results during a profile switch",async()=>{
    await ready();let finish:((r:Response)=>void)|undefined;
    vi.mocked(fetch).mockImplementation(async(input)=>String(input).endsWith(second.run_id)?new Promise<Response>(r=>{finish=r;}):fetchValue(String(input)));
    fireEvent.change(screen.getByLabelText("当前解析器运行"),{target:{value:second.run_id}});
    expect(screen.queryByRole("region",{name:"pymupdf PDF"})).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("当前解析器运行"),{target:{value:run.run_id}});
    await screen.findByRole("region",{name:"pymupdf PDF"});
    await act(async()=>{finish!(response(second));});
    expect(screen.getByLabelText("当前解析器运行")).toHaveValue(run.run_id);
    expect(screen.queryByText("Other parser exclusive text")).not.toBeInTheDocument();
  });
  it("keeps native/rule/profile display changes read-only and exposes a text+color legend",async()=>{
    const before=JSON.stringify(run);await ready();
    fireEvent.click(screen.getByRole("button",{name:"二次规则效果"}));
    expect(screen.getByRole("button",{name:"还原定位 1 · 正文候选"})).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox",{name:"显示噪声候选"}));
    fireEvent.change(screen.getByLabelText("下次运行解析器"),{target:{value:"tesseract"}});
    fireEvent.change(screen.getByLabelText("下次 OCR 语言"),{target:{value:"chi_sim"}});
    expect(screen.getByRole("region",{name:"pymupdf PDF"})).toBeVisible();
    expect(screen.getByLabelText("类型颜色图例").textContent).toContain("疑似水印");
    fireEvent.click(screen.getByRole("button",{name:"原生结果"}));
    expect(JSON.stringify(run)).toBe(before);expect(vi.mocked(fetch).mock.calls.some(([,init])=>init?.method==="POST")).toBe(false);
  });
  it("keeps same-source comparison separate and returns split view to a single parser",async()=>{
    await ready();fireEvent.click(screen.getByRole("button",{name:"内容对照"}));
    fireEvent.change(screen.getByLabelText("同源对照"),{target:{value:second.run_id}});
    await screen.findByText("Other parser exclusive text");
    fireEvent.click(screen.getByRole("button",{name:"PDF＋内容"}));expect(screen.queryByText("Other parser exclusive text")).not.toBeInTheDocument();
  });
  it("does not pad a failed result with the original page image",async()=>{
    const failed={...run,status:"FAILED",pages:[],source_ir:null,diagnostics:[{code:"CORRUPT",message:"Invalid real input"}]};
    vi.mocked(fetch).mockImplementation(async input=>response(String(input).endsWith("/parsers")?[]:String(input).endsWith("/runs")?[failed]:failed));
    render(<ParserLabPage/>);await screen.findByText("Invalid real input");
    const canvas=screen.getByRole("region",{name:"pymupdf 原位还原"});expect(canvas.querySelector("img,image")).toBeNull();
  });
  it("uses existing line coordinates and Unicode source offsets without inventing glyph boxes",async()=>{
    const example:Run={...run,source_ir:{...run.source_ir!,nodes:[{...run.source_ir!.nodes[0],raw_text:"🧭\nValue",
      pdf_text_lines:[{start_offset:0,end_offset:1,bbox:[20,20,40,35]},{start_offset:2,end_offset:7,bbox:[120,20,160,35]}]}]}};
    vi.mocked(fetch).mockImplementation(async input=>response(String(input).endsWith("/parsers")?[]:String(input).endsWith("/runs")?[example]:example));
    render(<ParserLabPage/>);await screen.findByRole("region",{name:"pymupdf 原位还原"});
    const group=screen.getByRole("region",{name:"pymupdf 原位还原"}).querySelector('[data-source-id="text-1"]')!;
    const lines=group.querySelectorAll("foreignObject");expect(lines).toHaveLength(2);
    expect(lines[0]).toHaveAttribute("x","20");expect(lines[0].textContent).toBe("🧭");
    expect(lines[1]).toHaveAttribute("x","120");expect(lines[1].textContent).toBe("Value");
  });
  it("hides repeated-margin candidates only in the rule projection and preserves source text",async()=>{
    const example:Run={...run,source_ir:{...run.source_ir!,nodes:[...run.source_ir!.nodes,...[1,2].map(page=>({
      source_node_id:`contract-header-${page}`,kind:"paragraph",source_order:page+3,raw_text:"Repeated header contract",
      native_locator:{page_number:page,bbox:[20,5,200,20] as [number,number,number,number]},
    }))]}};
    const before=JSON.stringify(example);
    vi.mocked(fetch).mockImplementation(async input=>response(String(input).endsWith("/parsers")?[]:String(input).endsWith("/runs")?[example]:example));
    render(<ParserLabPage/>);await screen.findByRole("region",{name:"pymupdf 原位还原"});
    fireEvent.click(screen.getByRole("button",{name:"结构化全文列表"}));expect(screen.getAllByText("Repeated header contract")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button",{name:"二次规则效果"}));
    fireEvent.click(screen.getByRole("checkbox",{name:"显示噪声候选"}));expect(screen.queryByText("Repeated header contract")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"原生结果"}));expect(screen.getAllByText("Repeated header contract")).toHaveLength(2);
    expect(JSON.stringify(example)).toBe(before);
  });
});
