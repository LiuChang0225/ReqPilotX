import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SourceLedgerTable } from "./SourceLedgerTable";
import type { SourceItemLedgerRowV1, SourceRequirementOutlineRecordV1 } from "../../shared/api/contracts";
import type { SourceItemDeliveryState } from "./SourceItemReasoning";
afterEach(cleanup);
it("keeps source identity and engineer precedence in the table projection", () => {
  const row = { row_id: "row", source_item_id: "src", child_row_ids: [], structure_node_id: "section" } as unknown as SourceItemLedgerRowV1;
  const record = { source_record_id: "src", requirement_display_id: "REQ-0001", source_display_id: "SRC-0001", requirementness: "REQUIREMENT", display_label: "原文条目", pages: [1,2,4] } as unknown as SourceRequirementOutlineRecordV1;
  const reasoning = { assessments: { records: { src: { decision: "COMPLIANT" } } }, engineer_responses: { records: { src: { state: "CURRENT", decision: "NEEDS_CLARIFICATION" } } } } as unknown as SourceItemDeliveryState;
  const select = vi.fn();
  render(<SourceLedgerTable rows={[row]} nodes={new Map()} records={new Map([["src",record]])} selectedId="row" expandedIds={new Set()}
    onSelect={select} onToggle={vi.fn()} titleFor={() => "原文条目"} kindFor={() => "功能"} reasoning={reasoning} />);
  expect(screen.getByText("待澄清")).toBeInTheDocument();
  expect(screen.getByText("工程师意见")).toBeInTheDocument();
  expect(screen.queryByText("符合")).not.toBeInTheDocument();
  expect(screen.getByText("P1–P2、P4")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /原文条目/ }));
  expect(select).toHaveBeenCalledWith(row);
});

function derivedFixture() {
  const parent = { row_id:"parent-row", source_item_id:"src", child_row_ids:[], structure_node_id:"section", depth:2, parent_row_id:null, row_role:"SOURCE_ITEM", logical_table_scope_ids:[], is_table_structure:false, is_one_to_one_item:false, processing_state:"CLASSIFIED", direct_source_item_count:1, requirement_count:1, non_requirement_count:0 } as SourceItemLedgerRowV1;
  const following = { ...parent, row_id:"following-row",source_item_id:"next" };
  const record = {source_record_id:"src",source_display_id:"SRC-0001",requirement_display_id:"REQ-0001",requirementness:"REQUIREMENT",requirement_kinds:["FUNCTIONAL"],pages:[2],display_label:"完整源条款"} as unknown as SourceRequirementOutlineRecordV1;
  const reasoning = {decomposition:{records:{src:[{singular_requirement_id:"u1",requirement_display_id:"REQ-0001-S01",text:"第一项独立要求",source_excerpts:[{text:"第一项独立要求",page:2}]},
    {singular_requirement_id:"u2",requirement_display_id:"REQ-0001-S02",text:"第二项独立要求",requirement_kinds:["QUALITY"],source_excerpts:[{text:"第二项独立要求",page:3}]}]}},
    assessments:{records:{src:{decision:"COMPLIANT"},u1:{decision:"PENDING_EVIDENCE"},u2:{decision:"COMPLIANT"}}},
    engineer_responses:{records:{u2:{state:"CURRENT",decision:"NON_COMPLIANT"}}}} as unknown as SourceItemDeliveryState;
  const props = {rows:[parent,following],nodes:new Map(),records:new Map([["src",record],["next",{...record,source_record_id:"next",requirement_display_id:"REQ-0002"}]]),
    selectedId:"parent-row",expandedIds:new Set<string>(),onSelect:vi.fn(),onToggle:vi.fn(),onSelectDerived:vi.fn(),onToggleDerived:vi.fn(),
    titleFor:(row:SourceItemLedgerRowV1)=>row.source_item_id==="src"?"完整源条款":"下一条源条款",kindFor:(item:SourceRequirementOutlineRecordV1)=>item.requirement_kinds.join(" / "),reasoning};
  return {parent,reasoning,props};
}
it("places independent derived rows immediately below their parent and uses each unit's own response", () => {
  const {parent,reasoning,props}=derivedFixture(), before=JSON.stringify({parent,reasoning});
  const page=render(<SourceLedgerTable {...props} selectedDerivedId="u2"/>);
  expect([...page.container.querySelectorAll('tbody tr')].map(row=>row.getAttribute('data-ledger-row'))).toEqual(["parent-row","derived:u1","derived:u2","following-row"]);
  const child = screen.getByRole("button",{name:"REQ-0001-S02 第二项独立要求"});
  expect(child.closest('tr')).toHaveTextContent("不符合"); expect(child.closest('tr')).toHaveTextContent("工程师意见"); expect(child.closest('tr')).toHaveTextContent("QUALITY");
  expect(child.closest('tr')).toHaveTextContent("P3"); expect(child).toHaveAttribute('aria-current','true');
  expect(screen.getByText("完整源条款").closest('button')).not.toHaveAttribute('aria-current');
  fireEvent.click(child); expect(props.onSelectDerived).toHaveBeenCalledWith(parent,"u2");
  fireEvent.keyDown(screen.getByText("完整源条款").closest('button')!,{key:"ArrowDown"});
  expect(props.onSelectDerived).toHaveBeenLastCalledWith(parent,"u1");
  expect(screen.getByRole("button",{name:"REQ-0001-S01 第一项独立要求"})).toHaveFocus();
  expect(JSON.stringify({parent,reasoning})).toBe(before);
});
it("retains the parent context when filtering derived rows and lets the family collapse", () => {
  const {props}=derivedFixture();const page=render(<SourceLedgerTable {...props} visibleDerivedIds={new Set(["u2"])} />);
  expect(screen.getByText("完整源条款")).toBeVisible();expect(screen.queryByText("第一项独立要求")).not.toBeInTheDocument();
  expect(screen.getByText("第二项独立要求")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"收起 REQ-0001 的派生项"}));expect(props.onToggleDerived).toHaveBeenCalledWith("src");
  page.rerender(<SourceLedgerTable {...props} collapsedDerivedIds={new Set(["src"])} />);
  expect(screen.queryByText("第二项独立要求")).not.toBeInTheDocument();expect(screen.getByText("下一条源条款")).toBeVisible();
});
