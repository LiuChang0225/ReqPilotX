import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  RequirementMatrixLogicalTable,
  RequirementMatrixNode,
} from "../../shared/api/contracts";
import { normalizeRestorationPage, normalizeStructuredTable } from "../restoration/restorationModel";
import { MatrixSourceContent } from "./MatrixSourceContent";

afterEach(cleanup);

function matrixNode(
  nodeId: string,
  overrides: Partial<RequirementMatrixNode> = {},
): RequirementMatrixNode {
  return {
    node_id: nodeId,
    parent_id: null,
    child_ids: [],
    depth: 0,
    source_order: 0,
    display_role: "CLAUSE",
    label: nodeId,
    summary: "",
    content_blocks: [],
    table_refs: [],
    figure_refs: [],
    source_refs: [],
    page_start: 1,
    page_end: 1,
    structure_node_id: nodeId,
    requirement_entry_id: null,
    requirement_id: null,
    requirement_code: "",
    requirement_text: "",
    table_record_id: null,
    logical_table_ids: [],
    matrix_visibility: "VISIBLE",
    disposition: "RETAIN",
    disposition_reason: "",
    analysis_eligibility: "NOT_ELIGIBLE",
    analysis_status: "NOT_APPLICABLE",
    ai_assessment: null,
    manual_decision: null,
    compliance_status: "",
    save_revision: 0,
    updated_at: "",
    ...overrides,
  };
}

describe("MatrixSourceContent", () => {
  it("does not present an unlinked ancestor's body as a source item's governing content", () => {
    const ancestor = matrixNode("ancestor", { child_ids: ["item"], content_blocks: [{ type: "TEXT", text: "This condition applies only to clauses 2.1 through 2.4." }] });
    const item = matrixNode("item", { parent_id: "ancestor", source_kind: "SOURCE_ITEM", source_item: { source_item_id: "s1", requirementness: "REQUIREMENT" },
      display_text: "The electrical pin connections shall follow Figure 1.", content_blocks: [{ type: "TEXT", text: "The electrical pin connections shall follow Figure 1." }] });
    render(<MatrixSourceContent node={item} contextPath={[ancestor]} logicalTables={[]} pageSnapshot={normalizeRestorationPage(null, 1)}
      projectId="p" documentId="d" sourceSha256="source" onPageChange={() => {}} />);
    expect(screen.getByText("The electrical pin connections shall follow Figure 1.")).toBeInTheDocument();
    expect(screen.queryByText("This condition applies only to clauses 2.1 through 2.4.")).not.toBeInTheDocument();
  });
  it("uses the shared AI table display name without replacing source cells", () => {
    const block = { id: "physical", type: "TABLE", page: 1, row_count: 2, column_count: 1,
      cells: [{ id: "h", row: 0, column: 0, text: "原表项目", header: true }, { id: "v", row: 1, column: 0, text: "原表完整内容" }] };
    const item = matrixNode("table-owner", { display_role: "TABLE", logical_table_ids: ["logical"], content_blocks: [block] });
    const logical = { logical_table_id: "logical", projection_id: "logical", projection_mode: "WHOLE_TABLE", record_count: 0,
      physical_table_ids: ["physical"], node_ids: [item.node_id], evidence_binding_ids: [], projection: { tables: [block],
        title_presentation: { display_title: "合成测试清单", authority: "AGENT_DISPLAY_LABEL", map_title: "旧Map标签" } } };
    render(<MatrixSourceContent node={item} contextPath={[item]} logicalTables={[logical]}
      pageSnapshot={normalizeRestorationPage({}, 1)} projectId="project" documentId="document"
      sourceSha256={"a".repeat(64)} onPageChange={vi.fn()} />);
    expect(screen.getByText("合成测试清单")).toBeVisible();
    expect(screen.getByText(/显示名（AI）/)).toBeVisible();
    expect(screen.getByText("原表完整内容")).toBeVisible();
  });
  it("retains native continuation fragments bound by their top-level logical table identity", () => {
    const first = {id:"branch-page-1",type:"TABLE",page:1,row_count:2,column_count:1,
      cells:[{id:"branch-header",row:0,column:0,text:"部件",header:true},
        {id:"branch-first",row:1,column:0,text:"分支第一页内容"}],
      logical_table_id:"branch"};
    const second = {id:"branch-page-2",type:"TABLE",page:2,row_count:1,column_count:1,
      cells:[{id:"branch-last",row:0,column:0,text:"分支第二页内容"}],
      logical_table_id:"branch"};
    const item = matrixNode("branch-record", {display_role:"TABLE_RECORD", table_record_id:"branch-record",
      logical_table_ids:["branch"],source_kind:"SOURCE_ITEM"});
    const logical = {logical_table_id:"branch",projection_id:"branch",projection_mode:"RECORDS",
      record_count:1,physical_table_ids:[first.id,second.id],node_ids:[item.node_id],evidence_binding_ids:[],
      projection:{tables:[first,second],records:[{record_id:item.node_id,record_kind:"TABLE_BRANCH",
        row_bindings:[{table_id:first.id,row_index:1,cell_ids:["branch-first"]},
          {table_id:second.id,row_index:0,cell_ids:["branch-last"]}],
        column_bindings:[{table_id:first.id,column_index:0,cell_ids:["branch-first"]},
          {table_id:second.id,column_index:0,cell_ids:["branch-last"]}],
        header_bindings:[{table_id:first.id,cell_ids:["branch-header"],axis:"ROW",text:"部件"}]}]}};
    render(<MatrixSourceContent node={item} contextPath={[item]} logicalTables={[logical]}
      pageSnapshot={normalizeRestorationPage({},1)} projectId="project" documentId="document"
      sourceSha256="source" onPageChange={vi.fn()} />);
    expect(screen.getByText("分支第一页内容")).toBeVisible();
    expect(screen.getByText("分支第二页内容")).toBeVisible();
  });
  it("keeps cross-row image associations and shows the selected table rather than the chapter intro", () => {
    const block = {id:"table",type:"TABLE",page:1,row_count:2,column_count:1,
      cells:[{id:"header",row:0,column:0,text:"项目",page:1},{id:"body",row:1,column:0,text:"本表内容",page:1}],
      floating_images:[{id:"shared",page:1,bbox:[10,20,40,60],source_row_indexes:[0,1]}]};
    const normalized = normalizeStructuredTable(block);
    expect(normalized.floatingImages?.[0].sourceRowIndexes).toEqual([0,1]);
    const intro = matrixNode("intro",{display_text:"上级产品介绍。"});
    const table = matrixNode("table",{display_role:"TABLE",display_title:"原表",content_blocks:[block]});
    render(<MatrixSourceContent node={table} parentClause={intro} contextPath={[intro,table]}
      logicalTables={[]} pageSnapshot={normalizeRestorationPage({},1)} projectId="project" documentId="document"
      sourceSha256={"a".repeat(64)} onPageChange={vi.fn()} />);
    expect(screen.getByText("本表内容")).toBeVisible();
    expect(screen.queryByText("上级产品介绍。")).not.toBeInTheDocument();
    expect(screen.getByAltText("原 PDF 中跨越多个表格单元格的图片")).toBeVisible();
  });

  it("does not attach chapter introduction to every original item", () => {
    const intro = matrixNode("intro", { display_title:"产品概述", display_text:"这是可回查的通用背景。" });
    const item = matrixNode("item", { source_kind:"SOURCE_ITEM", candidate_review:{},
      display_title:"当前试验", display_text:"当前试验的完整要求。" });
    const view = render(<MatrixSourceContent node={item} contextPath={[intro,item]} parentClause={intro}
      logicalTables={[]} pageSnapshot={normalizeRestorationPage({},1)} projectId="project" documentId="document"
      sourceSha256="source" onPageChange={vi.fn()} />);
    expect(screen.getByText("当前试验的完整要求。")).toBeVisible();
    expect(screen.queryByText("这是可回查的通用背景。")).not.toBeInTheDocument();
    view.rerender(<MatrixSourceContent node={intro} contextPath={[intro]}
      logicalTables={[]} pageSnapshot={normalizeRestorationPage({},1)} projectId="project" documentId="document"
      sourceSha256="source" onPageChange={vi.fn()} />);
    expect(screen.getByText("这是可回查的通用背景。")).toBeVisible();
  });

  it("retains explicitly applicable parent conditions without broader chapter background", () => {
    const intro = matrixNode("intro", { display_text:"一般产品背景。" });
    const scope = matrixNode("scope", { display_text:"本组条款共享的适用条件。" });
    const item = matrixNode("item", { source_kind:"SOURCE_ITEM", display_text:"本条具体判据。",
      candidate_review:{applicabilityScope:"scope"} });
    const props = {contextPath:[intro,scope,item], logicalTables:[], pageSnapshot:normalizeRestorationPage({},1),
      projectId:"project",documentId:"document",sourceSha256:"source",onPageChange:vi.fn()};
    const view = render(<MatrixSourceContent {...props} node={item} />);
    expect(screen.getByText("本组条款共享的适用条件。")).toBeVisible();
    expect(screen.getByText("本条具体判据。")).toBeVisible();
    expect(screen.queryByText("一般产品背景。")).not.toBeInTheDocument();
    view.rerender(<MatrixSourceContent {...props} node={{...item,candidate_review:{applicabilityScope:"missing"}}} />);
    expect(screen.getByText("本条具体判据。")).toBeVisible();
    expect(screen.queryByText("本组条款共享的适用条件。")).not.toBeInTheDocument();
    expect(screen.queryByText("一般产品背景。")).not.toBeInTheDocument();
  });

  it("shows exact source excerpts separately from the derived text and full parent", async () => {
    const parent = matrixNode("parent", { display_title: "Agent拆分项", source_title_role: "AGENT_ASSIGNED", display_text: "完整父项，保留其他条件。" });
    const child = matrixNode("child", { parent_id: "parent", display_role: "ATOMIC_REQUIREMENT",
      display_text: "派生后的表达", source_excerpts: [
        { id:"direct", role:"DIRECT", text:"原文不得得出现卡滞。", pages:[8] },
        { id:"context", role:"CONTEXT", text:"详\n细要求见附件。", pages:[8,9] },
      ] });
    const onPageChange = vi.fn();
    render(<MatrixSourceContent node={child} contextPath={[parent, child]} parentClause={parent}
      logicalTables={[]} pageSnapshot={normalizeRestorationPage({}, 1)} projectId="project" documentId="document"
      sourceSha256="source" onPageChange={onPageChange} />);
    expect(screen.getByText("原文不得得出现卡滞。", {selector:"blockquote"})).toBeVisible();
    expect(screen.getByText("派生后的表达")).toBeVisible();
    expect(screen.getByText("完整父项，保留其他条件。")).toBeVisible();
    expect(screen.getByText("共用上下文")).toBeVisible();
    expect(screen.getByText("完整父条款 · Agent 拟名/编号")).toBeVisible();
    await userEvent.click(screen.getByRole("button", {name:"原文第 9 页"}));
    expect(onPageChange).toHaveBeenCalledWith(9);
  });

  it("shows an atomic requirement as one focused table record with inherited headers", () => {
    const clause = matrixNode("clause", {
      label: "4.1 温度要求",
      summary: "温度要求完整父条款",
      content_blocks: [
        { id: "parent-copy", kind: "text", text: "温度要求完整父条款" },
      ],
    });
    const atomic = matrixNode("atomic", {
      parent_id: clause.node_id,
      depth: 1,
      display_role: "ATOMIC_REQUIREMENT",
      label: "高温工况",
      source_kind: "REQUIREMENT",
      source_number: "4.1.1",
      display_title: "高温工况",
      requirement_code: "REQ-0041",
      requirement_text: "高温工况应达到 85°C。",
      summary: "高温工况 | 85°C",
      table_record_id: "record-high-temperature",
      logical_table_ids: ["logical-temperature"],
      disposition: "ANALYZE",
      analysis_eligibility: "ELIGIBLE",
      analysis_status: "NOT_STARTED",
    });
    const tableFact = {
      id: "physical-temperature",
      table_id: "physical-temperature",
      row_count: 2,
      column_count: 2,
      cells: [
        { id: "header-item", row: 0, column: 0, text: "项目", header: true },
        { id: "header-value", row: 0, column: 1, text: "要求", header: true },
        { id: "record-item", row: 1, column: 0, text: "高温工况" },
        { id: "record-value", row: 1, column: 1, text: "85°C" },
      ],
      continuation: {
        logical_table_id: "logical-temperature",
        segment_index: 0,
        segment_count: 1,
      },
    };
    const logicalTable: RequirementMatrixLogicalTable = {
      logical_table_id: "logical-temperature",
      projection_id: "projection-temperature",
      projection_mode: "RECORDS",
      record_count: 1,
      physical_table_ids: ["physical-temperature"],
      node_ids: [atomic.node_id],
      evidence_binding_ids: [],
      evidence_bindings: [],
      projection: {
        tables: [tableFact],
        records: [
          {
            record_id: "record-high-temperature",
            row_bindings: [
              {
                table_id: "physical-temperature",
                row_index: 1,
                cell_ids: ["record-item", "record-value"],
              },
            ],
            column_bindings: [
              {
                table_id: "physical-temperature",
                column_index: 0,
                cell_ids: ["record-item"],
              },
              {
                table_id: "physical-temperature",
                column_index: 1,
                cell_ids: ["record-value"],
              },
            ],
            header_bindings: [
              {
                table_id: "physical-temperature",
                cell_ids: ["header-item", "header-value"],
                axis: "ROW",
                text: "项目 / 要求",
              },
            ],
          },
        ],
      },
    };

    render(
      <MatrixSourceContent
        node={atomic}
        contextPath={[clause, atomic]}
        parentClause={clause}
        logicalTables={[logicalTable]}
        pageSnapshot={normalizeRestorationPage({}, 1)}
        projectId="project"
        documentId="document"
        sourceSha256={"a".repeat(64)}
        onPageChange={vi.fn()}
      />,
    );

    expect(screen.getByText("所属条款：4.1 温度要求")).toBeInTheDocument();
    expect(screen.queryByText("温度要求完整父条款", { selector: "p" }))
      .not.toBeInTheDocument();
    expect(screen.queryByText("高温工况 | 85°C")).not.toBeInTheDocument();
    expect(
      screen.getAllByRole("cell", { name: "85°C" }).some((cell) =>
        cell.classList.contains("is-record-focus-cell"),
      ),
    ).toBe(true);
    expect(screen.getAllByText("表头：项目 / 要求").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "查看完整原表" }))
      .toHaveAttribute("aria-expanded", "false");
  });

  it("keeps a table record focused by default and reveals the full source table on demand", async () => {
    const root = matrixNode("root", {
      child_ids: ["table"],
      display_role: "STRUCTURE",
      label: "9 技术要求",
    });
    const tableFact = {
      id: "physical-temperature",
      table_id: "physical-temperature",
      kind: "table",
      row_count: 3,
      column_count: 2,
      cells: [
        { id: "header-item", row: 0, column: 0, text: "项目", header: true },
        { id: "header-value", row: 0, column: 1, text: "要求", header: true },
        { id: "record-hot", row: 1, column: 0, text: "高温工况" },
        { id: "record-hot-value", row: 1, column: 1, text: "85°C" },
        { id: "record-cold", row: 2, column: 0, text: "低温工况" },
        { id: "record-cold-value", row: 2, column: 1, text: "-40°C" },
      ],
      continuation: {
        logical_table_id: "logical-temperature",
        segment_index: 0,
        segment_count: 1,
      },
    };
    const table = matrixNode("table", {
      parent_id: root.node_id,
      child_ids: ["record"],
      depth: 1,
      display_role: "TABLE",
      label: "9.1 温度工况表",
      content_blocks: [tableFact],
      logical_table_ids: ["logical-temperature"],
    });
    const record = matrixNode("record", {
      parent_id: table.node_id,
      child_ids: ["atomic"],
      depth: 2,
      display_role: "TABLE_RECORD",
      label: "高温工况记录",
      source_kind: "TABLE_RECORD",
      source_number: "1",
      display_title: "高温工况记录",
      summary: "高温工况 | 85°C",
      table_record_id: "record-hot",
      logical_table_ids: ["logical-temperature"],
    });
    const atomic = matrixNode("atomic", {
      parent_id: record.node_id,
      depth: 3,
      display_role: "ATOMIC_REQUIREMENT",
      label: "高温验证",
      source_kind: "REQUIREMENT",
      source_number: "9.1.1",
      display_title: "高温验证",
      requirement_code: "REQ-0091",
      requirement_text: "控制器应在 85°C 下通过高温验证。",
      table_record_id: "record-hot",
      logical_table_ids: ["logical-temperature"],
      disposition: "ANALYZE",
      analysis_eligibility: "ELIGIBLE",
      analysis_status: "NOT_STARTED",
    });
    const logicalTable: RequirementMatrixLogicalTable = {
      logical_table_id: "logical-temperature",
      projection_id: "projection-temperature",
      projection_mode: "RECORDS",
      record_count: 2,
      physical_table_ids: ["physical-temperature"],
      node_ids: [table.node_id, record.node_id, atomic.node_id],
      evidence_binding_ids: [],
      projection: {
        records: [
          {
            record_id: "record-hot",
            row_bindings: [
              {
                table_id: "physical-temperature",
                row_index: 1,
                cell_ids: ["record-hot", "record-hot-value"],
              },
            ],
            column_bindings: [
              {
                table_id: "physical-temperature",
                column_index: 0,
                cell_ids: ["record-hot"],
              },
              {
                table_id: "physical-temperature",
                column_index: 1,
                cell_ids: ["record-hot-value"],
              },
            ],
            header_bindings: [
              {
                table_id: "physical-temperature",
                cell_ids: ["header-item", "header-value"],
                axis: "ROW",
                text: "项目 / 要求",
              },
            ],
          },
        ],
      },
    };
    const common = {
      logicalTables: [logicalTable],
      pageSnapshot: normalizeRestorationPage({}, 1),
      projectId: "project",
      documentId: "document",
      sourceSha256: "a".repeat(64),
      onPageChange: vi.fn(),
      recordAtomicNodes: [atomic],
    };
    const view = render(
      <MatrixSourceContent
        {...common}
        node={record}
        contextPath={[root, table, record]}
      />,
    );

    expect(screen.queryByRole("cell", { name: "低温工况" }))
      .not.toBeInTheDocument();
    expect(screen.queryByText("高温工况 | 85°C")).not.toBeInTheDocument();
    expect(
      screen.getAllByRole("cell", { name: "85°C" }).some((cell) =>
        cell.classList.contains("is-record-focus-cell"),
      ),
    ).toBe(true);
    expect(screen.getByRole("region", { name: "记录内原子需求" })).toHaveTextContent(
      "控制器应在 85°C 下通过高温验证。",
    );
    const user = userEvent.setup();
    const completeTableToggle = screen.getByRole("button", {
      name: "查看完整原表",
    });
    expect(completeTableToggle).toHaveAttribute("aria-expanded", "false");
    await user.click(completeTableToggle);
    expect(screen.getByRole("cell", { name: "低温工况" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起完整原表" }))
      .toHaveAttribute("aria-expanded", "true");

    view.rerender(
      <MatrixSourceContent
        {...common}
        node={atomic}
        contextPath={[root, table, record, atomic]}
      />,
    );
    expect(screen.queryByRole("cell", { name: "低温工况" }))
      .not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "记录内原子需求" })).toHaveTextContent(
      "高温验证",
    );

    view.rerender(
      <MatrixSourceContent
        {...common}
        node={{ ...record, child_ids: [] }}
        contextPath={[root, table, { ...record, child_ids: [] }]}
        recordAtomicNodes={[]}
      />,
    );
    expect(screen.getByText(/当前原文条目保持整体/)).toBeInTheDocument();
  });

  it("projects frozen header bindings in restored table order", () => {
    const headerCells = [
      ["header-axis", 0, "活塞速度\n（m/s）\n电流(A)"],
      ["header-0052", 1, "0.052"],
      ["header-0131", 2, "0.131"],
      ["header-0262", 3, "0.262"],
      ["header-0393", 4, "0.393"],
      ["header-0524", 5, "0.524"],
      ["header-1047", 6, "1.047"],
    ] as const;
    const tableFact = {
      id: "physical-damping",
      table_id: "physical-damping",
      kind: "table",
      row_count: 2,
      column_count: 7,
      cells: [
        ...headerCells.map(([id, column, text]) => ({
          id,
          row: 0,
          column,
          text,
          header: true,
        })),
        { id: "record-label", row: 1, column: 0, text: "阻尼力" },
        { id: "record-value", row: 1, column: 1, text: "1200N" },
      ],
      continuation: {
        logical_table_id: "logical-damping",
        segment_index: 0,
        segment_count: 1,
      },
    };
    const table = matrixNode("table", {
      child_ids: ["record"],
      display_role: "TABLE",
      label: "9.16.2 阻尼特性",
      content_blocks: [tableFact],
      logical_table_ids: ["logical-damping"],
    });
    const record = matrixNode("record", {
      parent_id: table.node_id,
      depth: 1,
      display_role: "TABLE_RECORD",
      label: "阻尼力记录",
      table_record_id: "record-damping",
      logical_table_ids: ["logical-damping"],
    });
    const headerText = new Map<string, string>(
      headerCells.map(([id, , text]) => [id, text]),
    );
    const frozenCellIdOrder = [
      "header-0131",
      "header-0262",
      "header-0393",
      "header-0052",
      "header-1047",
      "header-axis",
      "header-0524",
    ];
    const logicalTable: RequirementMatrixLogicalTable = {
      logical_table_id: "logical-damping",
      projection_id: "projection-damping",
      projection_mode: "RECORDS",
      record_count: 1,
      physical_table_ids: ["physical-damping"],
      node_ids: [table.node_id, record.node_id],
      evidence_binding_ids: [],
      projection: {
        records: [
          {
            record_id: "record-damping",
            row_bindings: [
              {
                table_id: "physical-damping",
                row_index: 1,
                cell_ids: ["record-label", "record-value"],
              },
            ],
            column_bindings: [
              {
                table_id: "physical-damping",
                column_index: 0,
                cell_ids: ["record-label"],
              },
            ],
            header_bindings: frozenCellIdOrder.map((cellId) => ({
              table_id: "physical-damping",
              cell_ids: [cellId],
              axis: "COLUMN",
              level: 0,
              text: headerText.get(cellId),
            })),
          },
        ],
      },
    };

    render(
      <MatrixSourceContent
        node={record}
        contextPath={[table, record]}
        logicalTables={[logicalTable]}
        pageSnapshot={normalizeRestorationPage({}, 1)}
        projectId="project"
        documentId="document"
        sourceSha256={"a".repeat(64)}
        onPageChange={vi.fn()}
      />,
    );

    expect(
      screen.getAllByText(
        "表头：活塞速度 （m/s） 电流(A) / 0.052 / 0.131 / 0.262 / 0.393 / 0.524 / 1.047",
      ).length,
    ).toBeGreaterThan(0);
  });

  it("keeps continuation records attached to the complete logical table and prefers a repeated local header", async () => {
    const firstSegment = {
      id: "physical-specification-1",
      table_id: "physical-specification-1",
      kind: "table",
      row_count: 2,
      column_count: 2,
      cells: [
        { id: "first-header-index", row: 0, column: 0, text: "序号", header: true },
        { id: "first-header-value", row: 0, column: 1, text: "要求", header: true },
        { id: "first-record-index", row: 1, column: 0, text: "8" },
        { id: "first-record-value", row: 1, column: 1, text: "前段第 8 条" },
      ],
      continuation: {
        logical_table_id: "logical-specification",
        segment_index: 0,
        segment_count: 2,
      },
    };
    const secondSegment = {
      id: "physical-specification-2",
      table_id: "physical-specification-2",
      kind: "table",
      row_count: 2,
      column_count: 2,
      cells: [
        { id: "second-header-index", row: 0, column: 0, text: "序号" },
        { id: "second-header-value", row: 0, column: 1, text: "要求" },
        { id: "second-record-index", row: 1, column: 0, text: "17" },
        { id: "second-record-value", row: 1, column: 1, text: "后段第 17 条" },
      ],
      continuation: {
        logical_table_id: "logical-specification",
        segment_index: 1,
        segment_count: 2,
      },
    };
    const table = matrixNode("table", {
      child_ids: ["record-17"],
      display_role: "TABLE",
      label: "2.3 技术要求表",
      content_blocks: [firstSegment, secondSegment],
      logical_table_ids: ["logical-specification"],
    });
    const record = matrixNode("record-17", {
      parent_id: table.node_id,
      depth: 1,
      display_role: "TABLE_RECORD",
      label: "第 17 条",
      table_record_id: "record-17",
      logical_table_ids: ["logical-specification"],
    });
    const logicalTable: RequirementMatrixLogicalTable = {
      logical_table_id: "logical-specification",
      projection_id: "projection-specification",
      projection_mode: "RECORDS",
      record_count: 17,
      physical_table_ids: [
        "physical-specification-1",
        "physical-specification-2",
      ],
      node_ids: [table.node_id, record.node_id],
      evidence_binding_ids: [],
      projection: {
        tables: [firstSegment, secondSegment],
        records: [
          {
            record_id: "record-17",
            row_bindings: [
              {
                table_id: "physical-specification-2",
                row_index: 1,
                cell_ids: ["second-record-index", "second-record-value"],
              },
            ],
            column_bindings: [
              {
                table_id: "physical-specification-2",
                column_index: 0,
                cell_ids: ["second-record-index"],
              },
              {
                table_id: "physical-specification-2",
                column_index: 1,
                cell_ids: ["second-record-value"],
              },
            ],
            header_bindings: [
              {
                table_id: "physical-specification-1",
                cell_ids: ["first-header-index", "first-header-value"],
                axis: "ROW",
                text: "序号 / 要求",
              },
            ],
          },
        ],
      },
    };

    const view = render(
      <MatrixSourceContent
        node={record}
        contextPath={[table, record]}
        logicalTables={[logicalTable]}
        pageSnapshot={normalizeRestorationPage({}, 1)}
        projectId="project"
        documentId="document"
        sourceSha256={"a".repeat(64)}
        onPageChange={vi.fn()}
      />,
    );

    expect(screen.getAllByText("表头：序号 / 要求").length)
      .toBeGreaterThan(0);
    expect(
      screen.getAllByRole("cell", { name: "后段第 17 条" }).some((cell) =>
        cell.classList.contains("is-record-focus-cell"),
      ),
    ).toBe(true);
    expect(screen.queryByRole("cell", { name: "前段第 8 条" }))
      .not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "查看完整原表" }));
    expect(screen.getByRole("cell", { name: "前段第 8 条" }))
      .toBeInTheDocument();
    expect(screen.getAllByRole("cell", { name: "后段第 17 条" }).length)
      .toBeGreaterThan(1);
    expect(screen.getByText("2 段续表")).toBeInTheDocument();

    const continuationWithoutHeader = {
      ...secondSegment,
      row_count: 1,
      cells: [
        { ...secondSegment.cells[2], row: 0 },
        { ...secondSegment.cells[3], row: 0 },
      ],
    };
    const inheritedRecord = {
      ...record,
      node_id: "record-17-inherited-header",
      table_record_id: "record-17-inherited-header",
    };
    const inheritedTable = {
      ...table,
      child_ids: [inheritedRecord.node_id],
      content_blocks: [firstSegment, continuationWithoutHeader],
    };
    const inheritedLogicalTable: RequirementMatrixLogicalTable = {
      ...logicalTable,
      node_ids: [inheritedTable.node_id, inheritedRecord.node_id],
      projection: {
        tables: [firstSegment, continuationWithoutHeader],
        records: [
          {
            record_id: "record-17-inherited-header",
            row_bindings: [
              {
                table_id: "physical-specification-2",
                row_index: 0,
                cell_ids: ["second-record-index", "second-record-value"],
              },
            ],
            column_bindings: [
              {
                table_id: "physical-specification-2",
                column_index: 0,
                cell_ids: ["second-record-index"],
              },
              {
                table_id: "physical-specification-2",
                column_index: 1,
                cell_ids: ["second-record-value"],
              },
            ],
            header_bindings: [
              {
                table_id: "physical-specification-1",
                cell_ids: ["first-header-index", "first-header-value"],
                axis: "ROW",
                text: "序号 / 要求",
              },
            ],
          },
        ],
      },
    };

    view.rerender(
      <MatrixSourceContent
        node={inheritedRecord}
        contextPath={[inheritedTable, inheritedRecord]}
        logicalTables={[inheritedLogicalTable]}
        pageSnapshot={normalizeRestorationPage({}, 1)}
        projectId="project"
        documentId="document"
        sourceSha256={"a".repeat(64)}
        onPageChange={vi.fn()}
      />,
    );

    expect((await screen.findAllByText("表头：序号 / 要求")).length)
      .toBeGreaterThan(0);
    expect(screen.getByRole("columnheader", { name: "要求" }))
      .toBeInTheDocument();
    expect(
      screen.getAllByRole("cell", { name: "后段第 17 条" }).some((cell) =>
        cell.classList.contains("is-record-focus-cell"),
      ),
    ).toBe(true);
  });
});
