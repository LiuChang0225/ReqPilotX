import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SourceLedgerTable } from "./SourceLedgerTable";
import type { SourceItemLedgerRowV1, SourceRequirementOutlineRecordV1 } from "../../shared/api/contracts";
import type { SourceItemDeliveryState } from "./SourceItemReasoning";
import { HostLocaleProvider } from "../../host/HostLocaleContext";
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

it("localizes the source ledger through the owned dictionary without touching source labels", () => {
  const row = { row_id: "row", source_item_id: "src", child_row_ids: [], structure_node_id: "section" } as unknown as SourceItemLedgerRowV1;
  const record = { source_record_id: "src", requirement_display_id: "REQ-0001", source_display_id: "SRC-0001", requirementness: "REQUIREMENT", display_label: "Customer verbatim label", pages: [1] } as unknown as SourceRequirementOutlineRecordV1;
  const reasoning = { assessments: { records: { src: { decision: "COMPLIANT" } } }, engineer_responses: { records: { src: { state: "CURRENT", decision: "NEEDS_CLARIFICATION" } } } } as unknown as SourceItemDeliveryState;
  render(<HostLocaleProvider t={key => key === "workbench.locale" ? "en" : key}>
    <SourceLedgerTable rows={[row]} nodes={new Map()} records={new Map([["src", record]])} selectedId="row" expandedIds={new Set()}
      onSelect={vi.fn()} onToggle={vi.fn()} titleFor={() => "Customer verbatim label"} kindFor={() => "Functional"} reasoning={reasoning} />
  </HostLocaleProvider>);
  expect(screen.getByRole("columnheader", { name: "Customer source number" })).toBeVisible();
  expect(screen.getByRole("columnheader", { name: "Response / Attention" })).toBeVisible();
  expect(screen.getByText("Pending clarification")).toBeVisible();
  expect(screen.getByText("Engineer opinion")).toBeVisible();
  expect(screen.getByText("Customer verbatim label")).toBeVisible();
});

it("projects source-only fields without analysis response columns", () => {
  const row = { row_id: "row", source_item_id: "src", child_row_ids: [], structure_node_id: "section" } as unknown as SourceItemLedgerRowV1;
  const record = { source_record_id: "src", requirement_display_id: "REQ-0001", source_display_id: "SRC-0001", requirementness: "REQUIREMENT", requirement_kinds: ["FUNCTIONAL"], display_label: "原文条目", pages: [2] } as unknown as SourceRequirementOutlineRecordV1;
  const reasoning = { assessments: { records: { src: { decision: "NEEDS_CLARIFICATION", evidence: [{ fact_id: "e1" }] } } }, engineer_responses: { records: {} } } as unknown as SourceItemDeliveryState;
  render(<SourceLedgerTable rows={[row]} nodes={new Map()} records={new Map([["src", record]])} selectedId="row" expandedIds={new Set()}
    onSelect={vi.fn()} onToggle={vi.fn()} titleFor={() => "原文条目"} kindFor={() => "功能"} reasoning={reasoning} lens="source" />);
  expect(screen.getByRole("columnheader", { name: "提取原文与层级" })).toBeVisible();
  expect(screen.getByRole("columnheader", { name: "来源 / SRC" })).toBeVisible();
  expect(screen.queryByRole("columnheader", { name: "响应 / 需处理" })).not.toBeInTheDocument();
  expect(screen.queryByText("待澄清")).not.toBeInTheDocument();
  expect(screen.getByText("SRC-0001")).toBeVisible();
});

it("shows only identified requirements with ancestor paths and keeps structure context", () => {
  const ancestor = { row_id: "ancestor", source_item_id: null, child_row_ids: [], structure_node_id: "section", parent_row_id: null, direct_source_item_count: 2 } as unknown as SourceItemLedgerRowV1;
  const requirement = { ...ancestor, row_id: "req-row", source_item_id: "req" };
  const nonRequirement = { ...ancestor, row_id: "note-row", source_item_id: "note" };
  const records = new Map<string, SourceRequirementOutlineRecordV1>([
    ["req", { source_record_id: "req", source_display_id: "SRC-0001", requirement_display_id: "REQ-0001", requirementness: "REQUIREMENT", requirement_kinds: ["FUNCTIONAL"], ancestor_path: ["3 技术要求", "3.2 尺寸"], display_label: "识别出的需求", pages: [2] } as unknown as SourceRequirementOutlineRecordV1],
    ["note", { source_record_id: "note", source_display_id: "SRC-0002", requirement_display_id: null, requirementness: "NON_REQUIREMENT", requirement_kinds: [], ancestor_path: ["3 技术要求"], display_label: "仅保留原文的注释", pages: [3] } as unknown as SourceRequirementOutlineRecordV1],
  ]);
  render(<SourceLedgerTable rows={[ancestor, requirement, nonRequirement]} nodes={new Map([["section", { node_id: "section", label: "3 技术要求", title: "3 技术要求", pages: [] } as never]])}
    records={records} selectedId="req-row" expandedIds={new Set()} onSelect={vi.fn()} onToggle={vi.fn()}
    titleFor={row => records.get(row.source_item_id || "")?.display_label || "结构"} kindFor={item => item.requirement_kinds.join(" / ")}
    reasoning={null} lens="requirements" requirementsOnly />);
  expect(screen.getByText("识别出的需求")).toBeVisible();
  expect(screen.getByText("3 技术要求 / 3.2 尺寸")).toBeVisible();
  expect(screen.getByText("REQ-0001")).toBeVisible();
  expect(screen.queryByText("仅保留原文的注释")).not.toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "需求内容与层级" })).toBeVisible();
  expect(screen.queryByRole("columnheader", { name: "响应 / 需处理" })).not.toBeInTheDocument();
});

it("shows real cited-evidence status for an analysis decision", () => {
  const row = { row_id: "row", source_item_id: "src", child_row_ids: [], structure_node_id: "section" } as unknown as SourceItemLedgerRowV1;
  const record = { source_record_id: "src", requirement_display_id: "REQ-0001", source_display_id: "SRC-0001", requirementness: "REQUIREMENT", requirement_kinds: ["FUNCTIONAL"], display_label: "原文条目", pages: [1] } as unknown as SourceRequirementOutlineRecordV1;
  const reasoning = { assessments: { records: { src: { decision: "COMPLIANT", evidence: [{ fact_id: "e1" }, { fact_id: "e2" }] } } }, engineer_responses: { records: {} } } as unknown as SourceItemDeliveryState;
  render(<SourceLedgerTable rows={[row]} nodes={new Map()} records={new Map([["src", record]])} selectedId="row" expandedIds={new Set()}
    onSelect={vi.fn()} onToggle={vi.fn()} titleFor={() => "原文条目"} kindFor={() => "功能"} reasoning={reasoning} lens="analysis" />);
  expect(screen.getByText("引用依据 2 条")).toBeVisible();
});

it("does not project an unread source item as a requirement or as a non-requirement", () => {
  const ancestor = { row_id: "ancestor", source_item_id: null, child_row_ids: [], structure_node_id: "section" } as unknown as SourceItemLedgerRowV1;
  const unread = { ...ancestor, row_id: "unread-row", source_item_id: "unread" };
  render(<SourceLedgerTable rows={[ancestor, unread]} nodes={new Map()} records={new Map()} selectedId="unread-row" expandedIds={new Set()}
    onSelect={vi.fn()} onToggle={vi.fn()} titleFor={row => row.source_item_id === "unread" ? "未读取条目" : "祖先结构"} kindFor={() => ""} reasoning={null} lens="requirements" requirementsOnly />);
  expect(screen.queryByText("未读取条目")).not.toBeInTheDocument();
  expect(screen.queryByText("祖先结构")).not.toBeInTheDocument();
  expect(screen.queryByText("祖先层级 · 不单独计为需求")).not.toBeInTheDocument();
});

it("keeps a non-requirement parent body when it is the real ancestor of a requirement", () => {
  const parent = { row_id: "parent-row", source_item_id: "parent", parent_row_id: null, child_row_ids: [], structure_node_id: "section" } as unknown as SourceItemLedgerRowV1;
  const child = { ...parent, row_id: "child-row", source_item_id: "child", parent_row_id: "parent-row" };
  const records = new Map<string, SourceRequirementOutlineRecordV1>([
    ["parent", { source_record_id: "parent", source_display_id: "SRC-0100", requirement_display_id: null, requirementness: "NON_REQUIREMENT", requirement_kinds: [], non_requirement_role: "CONTEXT", ancestor_path: ["3 技术要求"], display_label: "父级说明正文", pages: [1] } as unknown as SourceRequirementOutlineRecordV1],
    ["child", { source_record_id: "child", source_display_id: "SRC-0101", requirement_display_id: "REQ-0101", requirementness: "REQUIREMENT", requirement_kinds: ["FUNCTIONAL"], ancestor_path: ["3 技术要求", "3.1 父级说明"], display_label: "子项需求", pages: [2] } as unknown as SourceRequirementOutlineRecordV1],
  ]);
  render(<SourceLedgerTable rows={[parent, child]} nodes={new Map()} records={records} selectedId="child-row" expandedIds={new Set()}
    onSelect={vi.fn()} onToggle={vi.fn()} titleFor={row => records.get(row.source_item_id || "")?.display_label || "结构"}
    kindFor={item => item.requirement_kinds.join(" / ")} reasoning={null} lens="requirements" requirementsOnly ancestorIds={new Set(["parent-row"])} />);
  expect(screen.getByText("父级说明正文")).toBeVisible();
  const parentCell = screen.getByText("父级说明正文").closest(".ledger-title-cell") as HTMLElement;
  const childCell = screen.getByText("子项需求").closest(".ledger-title-cell") as HTMLElement;
  expect(parseFloat(childCell.style.paddingLeft)).toBeGreaterThan(parseFloat(parentCell.style.paddingLeft));
  expect(screen.getByText("来源上下文")).toBeVisible();
  expect(screen.getByText("子项需求")).toBeVisible();
  expect(screen.getByText("REQ-0101")).toBeVisible();
  expect(screen.queryByText("已识别需求 · 原文依据")).not.toBeInTheDocument();
});

it("keeps an ancestor row with a missing record as unread context, not as structure or a requirement", () => {
  const parent = { row_id: "parent-row", source_item_id: "missing-parent", parent_row_id: null, child_row_ids: [], structure_node_id: "section" } as unknown as SourceItemLedgerRowV1;
  const child = { ...parent, row_id: "child-row", source_item_id: "child", parent_row_id: "parent-row" };
  const records = new Map<string, SourceRequirementOutlineRecordV1>([
    ["child", { source_record_id: "child", source_display_id: "SRC-0101", requirement_display_id: "REQ-0101", requirementness: "REQUIREMENT", requirement_kinds: ["FUNCTIONAL"], ancestor_path: ["3 技术要求"], display_label: "子项需求", pages: [2] } as unknown as SourceRequirementOutlineRecordV1],
  ]);
  render(<SourceLedgerTable rows={[parent, child]} nodes={new Map()} records={records} selectedId="child-row" expandedIds={new Set()}
    onSelect={vi.fn()} onToggle={vi.fn()} titleFor={row => row.source_item_id === "missing-parent" ? "未读取父文" : "子项需求"}
    kindFor={() => ""} reasoning={null} lens="requirements" requirementsOnly />);
  expect(screen.getByText("未读取父文")).toBeVisible();
  expect(screen.getByText("尚未读取判定工件")).toBeVisible();
  expect(screen.queryByText("祖先层级 · 不单独计为需求")).not.toBeInTheDocument();
});
