import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../../shared/api/client";
import type {
  JsonRecord,
  RequirementMatrixNode,
  RequirementMatrixSnapshot,
  RequirementSet,
} from "../../shared/api/contracts";
import {
  WorkspaceProvider,
  type WorkspaceContextValue,
} from "../workspace/WorkspaceContext";
import { ExportsPanel } from "./ExportsPanel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function matrixNode(
  id: string,
  overrides: Partial<RequirementMatrixNode> = {},
): RequirementMatrixNode {
  return {
    node_id: id,
    parent_id: null,
    child_ids: [],
    depth: 0,
    source_order: 0,
    display_role: "ATOMIC_REQUIREMENT",
    label: id,
    summary: "",
    content_blocks: [],
    table_refs: [],
    figure_refs: [],
    source_refs: [{ page_number: 1 }],
    page_start: 1,
    page_end: 1,
    structure_node_id: null,
    requirement_entry_id: "requirement-1",
    requirement_id: "requirement-1",
    requirement_code: "REQ-1",
    requirement_text: id,
    table_record_id: null,
    logical_table_ids: [],
    matrix_visibility: "VISIBLE",
    disposition: "ANALYZE",
    disposition_reason: "",
    analysis_eligibility: "ELIGIBLE",
    analysis_status: "NOT_STARTED",
    analysis_result_state: "NONE",
    ai_assessment: null,
    manual_decision: null,
    compliance_status: "",
    save_revision: 0,
    updated_at: "",
    ...overrides,
  };
}

function snapshot(nodes: RequirementMatrixNode[]): RequirementMatrixSnapshot {
  return {
    schema_version: "requirement-matrix-snapshot/v1",
    state: "READY",
    project_id: "project",
    document_id: "document",
    requirement_set_id: "set",
    requirement_set_status: "LOCKED",
    tree_sha256: "a".repeat(64),
    root_node_ids: nodes.map((node) => node.node_id),
    nodes,
    logical_tables: [],
    counts: {
      total_nodes: nodes.length,
      visible_nodes: nodes.length,
      system_non_business_nodes: 0,
      human_excluded_nodes: 0,
      analyze_nodes: nodes.filter((node) => node.disposition === "ANALYZE")
        .length,
      retained_nodes: nodes.filter((node) => node.disposition === "RETAIN")
        .length,
      excluded_nodes: 0,
      analysis_eligible_nodes: nodes.filter(
        (node) => node.analysis_eligibility === "ELIGIBLE",
      ).length,
      assessed_nodes: nodes.filter(
        (node) => node.analysis_status === "ASSESSED",
      ).length,
      decided_nodes: nodes.filter((node) => node.analysis_status === "DECIDED")
        .length,
      logical_tables: 0,
    },
  };
}

function workspaceValue(
  exports: JsonRecord[] = [],
  requirementSet: RequirementSet = {
    id: "set",
    version: 1,
    status: "LOCKED",
  },
): WorkspaceContextValue {
  const noop = async () => {};
  return {
    route: {
      task: "export",
      mode: "review",
      projectId: "project",
      documentId: "document",
      requirementSetId: "set",
    },
    updateRoute: vi.fn(),
    projects: [],
    documents: [],
    knowledge: [],
    workspace: null,
    requirementSets: {
      active_requirement_set_id: "set",
      requirement_sets: [requirementSet],
    },
    extraction: null,
    structure: null,
    structureOptimization: null,
    aiStatus: null,
    exports,
    readiness: null,
    aiSetup: null,
    runtimeCapabilities: [],
    processingProfile: null,
    loading: false,
    refreshing: false,
    error: null,
    notice: "",
    busyAction: "",
    selectedProject: null,
    selectedDocument: {
      id: "document",
      project_id: "project",
      file_name: "source.pdf",
    },
    refreshAll: noop,
    refreshProject: noop,
    refreshDocument: noop,
    refreshExtraction: noop,
    refreshStructure: noop,
    refreshStructureOptimization: noop,
    refreshAi: noop,
    refreshExports: noop,
    runAction: async (_label, action) => action(),
    setNotice: vi.fn(),
  };
}

function renderPanel(
  matrix: RequirementMatrixSnapshot,
  exports: JsonRecord[] = [],
  requirementSet?: RequirementSet,
) {
  vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
  return render(
    <WorkspaceProvider value={workspaceValue(exports, requirementSet)}>
      <ExportsPanel />
    </WorkspaceProvider>,
  );
}

describe("ExportsPanel FORMAL readiness", () => {
  it.each([
    ["首次读取", { loading: true, refreshing: false }],
    ["跨文档刷新", { loading: false, refreshing: true }],
  ])(
    "%s确认清单时保持读取态，不把未知结果显示成空清单",
    async (_scenario, requestState) => {
      const value = workspaceValue();
      value.requirementSets = null;
      value.loading = requestState.loading;
      value.refreshing = requestState.refreshing;
      const matrixRequest = vi.spyOn(api, "requirementMatrix");

      render(
        <WorkspaceProvider value={value}>
          <ExportsPanel />
        </WorkspaceProvider>,
      );

      expect(await screen.findByRole("status", { name: "" })).toHaveTextContent(
        "正在读取确认清单",
      );
      expect(
        screen.queryByRole("heading", { name: "尚无确认清单" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "前往需求提取" }),
      ).not.toBeInTheDocument();
      expect(matrixRequest).not.toHaveBeenCalled();
    },
  );

  it("keeps an idle unknown confirmation-list response distinct from a true empty list", async () => {
    const value = workspaceValue();
    value.requirementSets = null;
    value.loading = false;
    value.refreshing = false;
    value.refreshDocument = vi.fn(async () => {});
    const matrixRequest = vi.spyOn(api, "requirementMatrix");
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("heading", {
        name: "确认清单暂不可读取",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "尚无确认清单" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试读取" }));
    expect(value.refreshDocument).toHaveBeenCalledOnce();
    expect(matrixRequest).not.toHaveBeenCalled();
  });

  it("shows only confirmation-list recovery when the document has no versions", async () => {
    const value = workspaceValue();
    value.route = { ...value.route, requirementSetId: "" };
    value.requirementSets = {
      active_requirement_set_id: "",
      requirement_sets: [],
    };
    const matrixRequest = vi.spyOn(api, "requirementMatrix");
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "尚无确认清单" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("有效结论覆盖")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("radio", { name: /DRAFT/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "导出需求矩阵" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "前往需求提取" }));
    expect(value.updateRoute).toHaveBeenCalledWith({
      task: "extraction",
      mode: "organize",
      checkpoint: "diagnose",
      requirementSetId: "",
    });
    expect(matrixRequest).not.toHaveBeenCalled();
  });

  it("keeps the version ledger and asks only for a selection when none is selected", async () => {
    const value = workspaceValue();
    value.route = { ...value.route, requirementSetId: "" };
    const matrixRequest = vi.spyOn(api, "requirementMatrix");
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "版本台账" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "选择需求版本" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("有效结论覆盖")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("radio", { name: /FORMAL/ }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "选择当前版本" }));
    expect(value.updateRoute).toHaveBeenCalledWith(
      { requirementSetId: "set" },
      { replace: true },
    );
    expect(matrixRequest).not.toHaveBeenCalled();
  });

  it("resets export mode and label when switching requirement sets", async () => {
    vi.spyOn(api, "requirementMatrix").mockImplementation(
      async (_projectId, _documentId, requirementSetId) => ({
        ...snapshot([]),
        requirement_set_id: requirementSetId,
      }),
    );
    const firstValue = workspaceValue();
    const user = userEvent.setup();
    const view = render(
      <WorkspaceProvider value={firstValue}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "导出需求矩阵" }),
      ).toBeEnabled(),
    );
    await user.click(screen.getByRole("radio", { name: /FORMAL/ }));
    await user.type(
      screen.getByRole("textbox", { name: "版本标识" }),
      "set-a-formal",
    );

    const secondValue = workspaceValue([], {
      id: "set-b",
      version: 2,
      status: "LOCKED",
    });
    secondValue.route = {
      ...secondValue.route,
      requirementSetId: "set-b",
    };
    secondValue.requirementSets = {
      active_requirement_set_id: "set-b",
      requirement_sets: [
        { id: "set-b", version: 2, status: "LOCKED" },
      ],
    };
    view.rerender(
      <WorkspaceProvider value={secondValue}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    expect(screen.getByRole("radio", { name: /DRAFT/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /FORMAL/ })).not.toBeChecked();
    expect(screen.getByRole("textbox", { name: "版本标识" })).toHaveValue(
      "",
    );

    view.rerender(
      <WorkspaceProvider value={firstValue}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );
    expect(screen.getByRole("radio", { name: /DRAFT/ })).toBeChecked();
    expect(screen.getByRole("textbox", { name: "版本标识" })).toHaveValue(
      "",
    );
  });

  it("resets export mode and label when switching documents", async () => {
    vi.spyOn(api, "requirementMatrix").mockImplementation(
      async (_projectId, documentId) => ({
        ...snapshot([]),
        document_id: documentId,
      }),
    );
    const firstValue = workspaceValue();
    const user = userEvent.setup();
    const view = render(
      <WorkspaceProvider value={firstValue}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "导出需求矩阵" }),
      ).toBeEnabled(),
    );
    await user.click(screen.getByRole("radio", { name: /FORMAL/ }));
    await user.type(
      screen.getByRole("textbox", { name: "版本标识" }),
      "document-a-formal",
    );

    const secondValue = workspaceValue();
    secondValue.route = {
      ...secondValue.route,
      documentId: "document-b",
    };
    secondValue.selectedDocument = {
      id: "document-b",
      project_id: "project",
      file_name: "source-b.pdf",
    };
    view.rerender(
      <WorkspaceProvider value={secondValue}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    expect(screen.getByRole("radio", { name: /DRAFT/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /FORMAL/ })).not.toBeChecked();
    expect(screen.getByRole("textbox", { name: "版本标识" })).toHaveValue(
      "",
    );
  });

  it("keeps FORMAL disabled when a locked AI-only set lacks stable preview proof", async () => {
    renderPanel(
      snapshot([
        matrixNode("AI 默认结论", {
          analysis_status: "ASSESSED",
          analysis_result_state: "CURRENT",
          ai_assessment: {
            decision: "ACCEPT",
            compliance_status: "COMPLIANT",
            reason: "证据充分",
            confidence: 0.9,
            assessment_mode: "EVIDENCE",
          },
        }),
      ]),
    );
    const user = userEvent.setup();

    expect(await screen.findByText("有效结论覆盖")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /FORMAL/ }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "导出需求矩阵" }),
      ).toBeDisabled(),
    );
    expect(screen.getByText(/缺少稳定解析预览证明/)).toBeInTheDocument();
    expect(screen.getByText(/当前终态 AI 结论默认采用/)).toBeInTheDocument();
    expect(screen.queryByText(/100% 人工结论/)).not.toBeInTheDocument();
  });

  it.each([
    ["preview generation id", { preview_generation_id: "preview-1" }],
    ["READY status at lock", { preview_status_at_lock: "READY" }],
  ])(
    "enables FORMAL when the set proves a stable preview by %s",
    async (_name, proof) => {
      renderPanel(
        snapshot([
          matrixNode("AI 默认结论", {
            analysis_status: "ASSESSED",
            analysis_result_state: "CURRENT",
            ai_assessment: {
              decision: "ACCEPT",
              compliance_status: "COMPLIANT",
              reason: "证据充分",
              confidence: 0.9,
              assessment_mode: "EVIDENCE",
            },
          }),
        ]),
        [],
        { id: "set", version: 1, status: "LOCKED", ...proof },
      );
      const user = userEvent.setup();

      await user.click(await screen.findByRole("radio", { name: /FORMAL/ }));
      expect(
        screen.getByRole("button", { name: "导出需求矩阵" }),
      ).toBeEnabled();
      expect(
        screen.queryByText(/缺少稳定解析预览证明/),
      ).not.toBeInTheDocument();
    },
  );

  it("keeps FORMAL disabled for a stale conclusion", async () => {
    renderPanel(
      snapshot([
        matrixNode("过期结论", {
          analysis_result_state: "STALE_REFERENCE",
          stale_ai_assessment: {
            decision: "ACCEPT",
            compliance_status: "COMPLIANT",
            reason: "旧版结论",
            confidence: 0.8,
            assessment_mode: "EVIDENCE",
          },
        }),
      ]),
      [],
      {
        id: "set",
        version: 1,
        status: "LOCKED",
        preview_generation_id: "preview-1",
      },
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("radio", { name: /FORMAL/ }));
    expect(screen.getByRole("button", { name: "导出需求矩阵" })).toBeDisabled();
    expect(screen.getByText(/有效结论覆盖 0%/)).toBeInTheDocument();
  });

  it("lists every FORMAL blocker in a stable recovery order", async () => {
    renderPanel(
      {
        ...snapshot([matrixNode("待分析 1"), matrixNode("待分析 2")]),
        state: "REQUIRES_REEXTRACTION",
      },
      [],
      { id: "set", version: 1, status: "DRAFT" },
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("radio", { name: /FORMAL/ }));
    const blockerList = screen.getByRole("list", {
      name: "FORMAL 导出阻断项",
    });
    expect(
      within(blockerList)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([
      expect.stringContaining("尚未锁定"),
      expect.stringContaining("缺少稳定解析预览证明"),
      expect.stringContaining("缺少有效结论 2 条"),
      expect.stringContaining("统一需求矩阵尚未 READY"),
    ]);
    expect(screen.getByRole("button", { name: "导出需求矩阵" })).toBeDisabled();
  });

  it("does not render 199 of 200 conclusions as 100 percent", async () => {
    const assessed = Array.from({ length: 199 }, (_, index) =>
      matrixNode(`assessed-${index}`, {
        analysis_status: "ASSESSED",
        analysis_result_state: "CURRENT",
        ai_assessment: {
          decision: "ACCEPT",
          compliance_status: "COMPLIANT",
          reason: "已完成",
          confidence: 0.9,
          assessment_mode: "EVIDENCE",
        },
      }),
    );
    renderPanel(snapshot([...assessed, matrixNode("pending-200")]), [], {
      id: "set",
      version: 1,
      status: "LOCKED",
      preview_generation_id: "preview-1",
    });
    const user = userEvent.setup();

    expect(await screen.findByText("99.5%")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /FORMAL/ }));
    expect(screen.getByRole("button", { name: "导出需求矩阵" })).toBeDisabled();
    expect(screen.getByText(/有效结论覆盖 99\.5%/)).toBeInTheDocument();
    expect(screen.queryByText(/有效结论覆盖 100%/)).not.toBeInTheDocument();
  });

  it("allows FORMAL for a locked ready matrix with zero analysis targets", async () => {
    renderPanel(
      snapshot([
        matrixNode("只保留上下文", {
          disposition: "RETAIN",
          analysis_eligibility: "NOT_ELIGIBLE",
          analysis_status: "NOT_APPLICABLE",
        }),
      ]),
      [],
      {
        id: "set",
        version: 1,
        status: "LOCKED",
        preview_generation_id: "preview-1",
      },
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("radio", { name: /FORMAL/ }));
    expect(screen.getByRole("button", { name: "导出需求矩阵" })).toBeEnabled();
    expect(
      screen.getByText(/0\/0 条需分析内容已有有效结论/),
    ).toBeInTheDocument();
  });

  it("labels historical human coverage as manual-change coverage", async () => {
    renderPanel(snapshot([]), [
      {
        id: "export-1",
        document_id: "document",
        file_name: "matrix.xlsx",
        status: "SUCCEEDED",
        export_mode: "FORMAL",
        human_coverage: 0.5,
        created_at: "2026-08-08T00:00:00Z",
      },
    ]);

    expect(await screen.findByText(/人工修改覆盖/)).toHaveTextContent(
      "人工修改覆盖 50%",
    );
    expect(screen.queryByText(/人工结论覆盖 50%/)).not.toBeInTheDocument();
  });

  it("clears comparison and latest-download state when project/document scope changes", async () => {
    const oldExport = {
      id: "old-export",
      document_id: "document",
      file_name: "old-history.xlsx",
      version_label: "old-history",
      status: "SUCCEEDED",
    };
    const newExport = {
      id: "new-export",
      document_id: "document-b",
      file_name: "new-history.xlsx",
      version_label: "new-history",
      status: "SUCCEEDED",
    };
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(snapshot([]));
    const createExport = vi
      .spyOn(api, "createExport")
      .mockResolvedValueOnce({
        download_url: "/downloads/old-result.xlsx",
        file_name: "old-result.xlsx",
      })
      .mockResolvedValueOnce({
        download_url: "/downloads/new-result.xlsx",
        file_name: "new-result.xlsx",
      });
    const firstValue = workspaceValue([oldExport]);
    const user = userEvent.setup();
    const view = render(
      <WorkspaceProvider value={firstValue}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    const compareSelect = await screen.findByRole("combobox", {
      name: "对比版本",
    });
    await user.selectOptions(compareSelect, "old-export");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "导出需求矩阵" }),
      ).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "导出需求矩阵" }));
    expect(
      await screen.findByRole("link", { name: "打开 old-result.xlsx" }),
    ).toBeInTheDocument();
    expect(createExport).toHaveBeenNthCalledWith(
      1,
      "project",
      expect.objectContaining({
        document_id: "document",
        compare_export_id: "old-export",
      }),
    );

    const secondValue = workspaceValue([newExport]);
    secondValue.route = {
      ...secondValue.route,
      projectId: "project-b",
      documentId: "document-b",
    };
    secondValue.selectedDocument = {
      id: "document-b",
      project_id: "project-b",
      file_name: "source-b.pdf",
    };
    view.rerender(
      <WorkspaceProvider value={secondValue}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    expect(
      screen.queryByRole("link", { name: "打开 old-result.xlsx" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "对比版本" })).toHaveValue("");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "导出需求矩阵" }),
      ).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "导出需求矩阵" }));

    expect(createExport).toHaveBeenNthCalledWith(
      2,
      "project-b",
      expect.objectContaining({
        document_id: "document-b",
        compare_export_id: null,
      }),
    );
    expect(
      await screen.findByRole("link", { name: "打开 new-result.xlsx" }),
    ).toBeInTheDocument();
  });

  it("does not reveal a late export result after the user switches documents", async () => {
    let resolveExport: ((result: JsonRecord) => void) | undefined;
    const pendingExport = new Promise<JsonRecord>((resolve) => {
      resolveExport = resolve;
    });
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(snapshot([]));
    vi.spyOn(api, "createExport").mockReturnValue(pendingExport);
    const firstValue = workspaceValue();
    firstValue.refreshExports = vi.fn(async () => {});
    const user = userEvent.setup();
    const view = render(
      <WorkspaceProvider value={firstValue}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "导出需求矩阵" }),
      ).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "导出需求矩阵" }));

    const secondValue = workspaceValue();
    secondValue.refreshExports = vi.fn(async () => {});
    secondValue.route = {
      ...secondValue.route,
      projectId: "project-b",
      documentId: "document-b",
    };
    secondValue.selectedDocument = {
      id: "document-b",
      project_id: "project-b",
      file_name: "source-b.pdf",
    };
    view.rerender(
      <WorkspaceProvider value={secondValue}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    await act(async () => {
      resolveExport?.({
        download_url: "/downloads/late-old-result.xlsx",
        file_name: "late-old-result.xlsx",
      });
      await pendingExport;
    });
    expect(
      screen.queryByRole("link", { name: "打开 late-old-result.xlsx" }),
    ).not.toBeInTheDocument();
    expect(firstValue.refreshExports).toHaveBeenCalledTimes(1);
  });

  it("does not reveal or refresh a late result after switching sets in the same document", async () => {
    let resolveExport: ((result: JsonRecord) => void) | undefined;
    const pendingExport = new Promise<JsonRecord>((resolve) => {
      resolveExport = resolve;
    });
    vi.spyOn(api, "requirementMatrix").mockImplementation(
      async (_projectId, _documentId, requirementSetId) => ({
        ...snapshot([]),
        requirement_set_id: requirementSetId,
      }),
    );
    vi.spyOn(api, "createExport").mockReturnValue(pendingExport);
    const firstValue = workspaceValue();
    firstValue.refreshExports = vi.fn(async () => {});
    const user = userEvent.setup();
    const view = render(
      <WorkspaceProvider value={firstValue}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "导出需求矩阵" }),
      ).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "导出需求矩阵" }));

    const secondValue = workspaceValue();
    secondValue.route = {
      ...secondValue.route,
      requirementSetId: "set-b",
    };
    secondValue.requirementSets = {
      active_requirement_set_id: "set-b",
      requirement_sets: [{ id: "set-b", version: 2, status: "LOCKED" }],
    };
    secondValue.refreshExports = vi.fn(async () => {});
    view.rerender(
      <WorkspaceProvider value={secondValue}>
        <ExportsPanel />
      </WorkspaceProvider>,
    );

    await act(async () => {
      resolveExport?.({
        download_url: "/downloads/late-set-a.xlsx",
        file_name: "late-set-a.xlsx",
      });
      await pendingExport;
    });

    expect(
      screen.queryByRole("link", { name: "打开 late-set-a.xlsx" }),
    ).not.toBeInTheDocument();
    expect(firstValue.refreshExports).toHaveBeenCalledTimes(1);
  });
});
