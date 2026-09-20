import { describe, expect, it } from "vitest";
import type { SourceRequirementBaselineOutlineV1 } from "../../shared/api/contracts";
import { filterSourceItemLedger, sourceReviewCounts, type SourceItemReviewContext } from "./sourceItemReviewContext";

function fixture() {
  const records = [
    { source_record_id: "a", source_display_id: "SRC-0001", requirement_display_id: "REQ-0001", display_label: "静态电流", label: "1", ancestor_path: ["产品", "性能"], requirementness: "REQUIREMENT", requirement_kinds: ["QUALITY"] },
    { source_record_id: "b", source_display_id: "SRC-0002", requirement_display_id: "REQ-0002", display_label: "振动试验", label: "2", ancestor_path: ["产品", "验证"], requirementness: "REQUIREMENT", requirement_kinds: ["QUALITY"] },
    { source_record_id: "c", source_display_id: "SRC-0003", requirement_display_id: "REQ-0003", display_label: "两周内交付", label: "3", ancestor_path: ["项目"], requirementness: "REQUIREMENT", requirement_kinds: ["PROJECT"] },
  ];
  const rows = [{ row_id: "root", parent_row_id: null, child_row_ids: ["section", "c"], source_item_id: null },
    { row_id: "section", parent_row_id: "root", child_row_ids: ["a", "b"], source_item_id: null },
    ...records.map(record => ({ row_id: record.source_record_id, source_item_id: record.source_record_id, parent_row_id: record.source_record_id === "c" ? "root" : "section", child_row_ids: [] }))];
  const outline = { records, ledger: { rows, root_row_ids: ["root"] } } as unknown as SourceRequirementBaselineOutlineV1;
  const context = { records: {
    a: { search_text: "25℃时静态电流不得超过5mA", topics: [], focus_areas: ["TECHNICAL_PARAMETERS"], needs_attention: false, changed_from_previous: true },
    b: { search_text: "试验方法与加速度判据", topics: ["VERIFICATION"], focus_areas: ["VERIFICATION"], needs_attention: true, changed_from_previous: null, derived_count: 2 },
    c: { search_text: "2周提交ISO9001认证报告", topics: [], focus_areas: [], needs_attention: false, changed_from_previous: false },
  } } as unknown as SourceItemReviewContext;
  return { outline, context };
}
const filters = { query: "", view: "ALL" as const, category: "", topic: "" };

describe("source review filtering", () => {
  it("keeps source ancestors and identities without promoting project numbers to parameters", () => {
    const { outline, context } = fixture(); const before = JSON.stringify(outline);
    const result = filterSourceItemLedger(outline, context, { ...filters, view: "PARAMETERS" });
    expect([...result.matched]).toEqual(["a"]);
    expect([...result.visible].sort()).toEqual(["a", "root", "section"]);
    expect(JSON.stringify(outline)).toBe(before);
  });
  it("searches bound source content and IDs together", () => {
    const { outline, context } = fixture();
    expect([...filterSourceItemLedger(outline, context, { ...filters, query: "SRC-0001 5ma" }).matched]).toEqual(["a"]);
    expect([...filterSourceItemLedger(outline, context, { ...filters, query: "加速度", topic: "VERIFICATION" }).matched]).toEqual(["b"]);
  });
  it("does not turn unknown history or all pending technical analyses into review issues", () => {
    const { outline, context } = fixture();
    expect([...filterSourceItemLedger(outline, context, { ...filters, view: "CHANGED" }).matched]).toEqual(["a"]);
    expect([...filterSourceItemLedger(outline, context, { ...filters, view: "ATTENTION" }).matched]).toEqual(["b"]);
  });
  it("retains all original rows when unfiltered and aggregates scope focus", () => {
    const { outline, context } = fixture();
    expect(filterSourceItemLedger(outline, context, filters).visible.size).toBe(5);
    expect(sourceReviewCounts(outline.ledger.rows, context).get("root")).toEqual({ focus: 2, attention: 1, changed: 1 });
  });
});

it("finds a derived identifier while retaining its source parent and document ancestry", () => {
  const {outline,context}=fixture();
  const result=filterSourceItemLedger(outline,context,{...filters,query:"REQ-0002-S02"},new Map([["b",[{search_text:"REQ-0002-S01 温度条件",requirement_kinds:["QUALITY"]},{search_text:"REQ-0002-S02 加速度边界",requirement_kinds:["QUALITY"]}]]]));
  expect([...result.matched]).toEqual(["b"]);expect([...result.visible].sort()).toEqual(["b","root","section"]);
});

it("keeps a matching child under its parent even when their categories differ", () => {
  const {outline,context}=fixture();outline.records[0].requirement_kinds=["FUNCTIONAL"];
  const related=new Map([["a",[{search_text:"REQ-0001-S01 Warmup TBD",requirement_kinds:["QUALITY"]},{search_text:"REQ-0001-S02 Supply voltage",requirement_kinds:["CONSTRAINT"]}]]]);
  const result=filterSourceItemLedger(outline,context,{...filters,query:"REQ-0001-S01",category:"QUALITY"},related);
  expect([...result.matched]).toEqual(["a"]);expect([...result.visible].sort()).toEqual(["a","root","section"]);
  expect(filterSourceItemLedger(outline,context,{...filters,query:"REQ-0001-S01",category:"CONSTRAINT"},related).matched.size).toBe(0);
  expect(outline.records[0].requirement_kinds).toEqual(["FUNCTIONAL"]);
});
it("searches explicit customer numbers even when the display label contains no number",()=>{
  const {outline,context}=fixture();outline.records[0].source_number="2.10";
  expect([...filterSourceItemLedger(outline,context,{...filters,query:"2.10"}).matched]).toEqual(["a"]);
});
