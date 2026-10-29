import { describe, expect, it } from "vitest";
import { KINDS, presentation } from "./presentation";
import type { Run } from "./contracts";

/** Constructed display contracts, never parser or document-quality evidence. */
export function ruleContract(): Run {
  return { run_id:"rules-contract", filename:"display-contract", parser:"pymupdf", parser_version:"contract", source_sha256:"f".repeat(64),
    adapter_id:"contract", adapter_version:"contract", status:"PARTIAL", started_at:"2026-10-28T00:00:00Z", diagnostics:[], artifacts:[],
    pages:[1,2,3].map(page_number=>({page_number,bbox:[0,0,400,500],cropbox:[0,0,400,500],rotation:0,render_artifact:`p${page_number}`})),
    source_ir:{assets:[],nodes:[1,2,3].flatMap(page=>[
      {source_node_id:`header-${page}`,kind:"paragraph",source_order:(page-1)*4,raw_text:"Repeated margin header",native_locator:{page_number:page,bbox:[20,10,250,25]}},
      {source_node_id:`body-${page}`,kind:"paragraph",source_order:(page-1)*4+1,raw_text:`Unique body ${page}`,native_locator:{page_number:page,bbox:[20,100,250,115]}},
      {source_node_id:`footer-${page}`,kind:"paragraph",source_order:(page-1)*4+2,raw_text:`Page ${page} / 3`,native_locator:{page_number:page,bbox:[20,470,100,485]}},
      {source_node_id:`watermark-${page}`,kind:"paragraph",source_order:(page-1)*4+3,raw_text:"DRAFT",native_locator:{page_number:page,bbox:[50,200,350,280]}},
    ])}};
}
function freeze(value:unknown) { if(value&&typeof value==="object"){Object.freeze(value);Object.values(value).forEach(freeze);} }
describe("display-only rules",()=>{
  it("keeps native semantic categories unknown and labels candidates with reasons",()=>{
    const run=ruleContract();freeze(run);const before=JSON.stringify(run);
    expect(presentation(run,"native").every(e=>e.kind==="unknown")).toBe(true);
    const rules=presentation(run,"rules");
    expect(rules.find(e=>e.id==="header-1")).toMatchObject({kind:"header",noise:true,origin:"二次规则 pl-repeat-margin/v1"});
    expect(rules.find(e=>e.id==="footer-2")?.kind).toBe("footer");
    expect(rules.find(e=>e.id==="watermark-3")?.kind).toBe("watermark");
    expect(rules.find(e=>e.id==="body-1")?.kind).toBe("body");
    expect(rules.every(e=>e.reasons.length&&e.reasons.at(-1)?.includes("pl-geometric-order/v1"))).toBe(true);
    expect(JSON.stringify(run)).toBe(before);
  });
  it("does not classify a one-page top line as repeated furniture or hide unknowns",()=>{
    const run=ruleContract();run.pages=run.pages.slice(0,1);run.source_ir!.nodes=run.source_ir!.nodes.filter(n=>n.native_locator.page_number===1);
    const rules=presentation(run,"rules");
    expect(rules.find(e=>e.id==="header-1")?.kind).toBe("body");
    expect(rules.some(e=>e.noise)).toBe(false);
  });
  it("uses different colors plus explicit labels for every content family",()=>{
    expect(new Set(Object.values(KINDS).map(k=>k.color)).size).toBe(8);
    expect(Object.values(KINDS).every(k=>k.label)).toBe(true);
  });
  it("folds proven table/text overlap only in the rule canvas projection",()=>{
    const run=ruleContract();
    run.source_ir!.nodes.push({source_node_id:"table",kind:"table",source_order:12,raw_text:"",native_locator:{page_number:1,bbox:[10,90,300,150]},
      table_payload:{row_count:1,column_count:1,cells:[{cell_id:"contract-cell",row:0,column:0,row_span:1,column_span:1,raw_text:"Unique body 1",native_locator:{page_number:1,bbox:[10,90,300,150]}}]}});
    const before=JSON.stringify(run);freeze(run);
    expect(presentation(run,"native").find(e=>e.id==="body-1")?.canvasOmitted).toBeUndefined();
    const candidate=presentation(run,"rules").find(e=>e.id==="body-1");
    expect(candidate?.canvasOmitted).toBe("pl-table-text-overlap/v1");
    expect(candidate?.text).toBe("Unique body 1");expect(JSON.stringify(run)).toBe(before);
  });
  it("recognizes Chinese page-counter templates only as repeated margin candidates",()=>{
    const run=ruleContract();
    for (const node of run.source_ir!.nodes) if(node.source_node_id.startsWith("footer")) node.raw_text=`第${node.native_locator.page_number}页 共3页`;
    expect(presentation(run,"rules").filter(e=>e.kind==="footer")).toHaveLength(3);
    expect(run.source_ir!.nodes.find(n=>n.source_node_id==="footer-2")?.raw_text).toBe("第2页 共3页");
  });
});
