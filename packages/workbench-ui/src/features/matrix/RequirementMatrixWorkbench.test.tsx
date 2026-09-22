import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../../shared/api/client";
import type {
  JsonRecord,
  RequirementMatrixNode,
  RequirementMatrixSnapshot,
  RequirementSetLockReadiness,
} from "../../shared/api/contracts";
import {
  WorkspaceProvider,
  type WorkspaceContextValue,
} from "../workspace/WorkspaceContext";
import {
  profileReady,
  RequirementMatrixWorkbench,
} from "./RequirementMatrixWorkbench";

describe("profileReady", () => {
  it("trusts an explicit false readiness instead of legacy configured flags", () => {
    expect(
      profileReady({
        id: "aliyun",
        ready: false,
        llm_ready: true,
        retrieval_ready: true,
      }),
    ).toBe(false);
  });

  it("uses legacy flags only when ready is absent", () => {
    expect(
      profileReady({
        id: "legacy",
        llm_ready: true,
        retrieval_ready: true,
      }),
    ).toBe(true);
  });
});

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
    this.dispatchEvent(new Event("close"));
  };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function installResizeObserver({
  treeWidth,
  workbenchWidth = 1200,
}: {
  treeWidth: number;
  workbenchWidth?: number;
}) {
  class TestResizeObserver {
    private readonly callback: ResizeObserverCallback;

    constructor(callback: ResizeObserverCallback) {
      this.callback = callback;
    }

    observe = (element: Element) => {
      const width = element.classList.contains("matrix-tree-pane")
        ? treeWidth
        : element.classList.contains("requirement-matrix-workbench")
          ? workbenchWidth
          : 560;
      const height = element.classList.contains("matrix-treegrid-scroll")
        ? 560
        : 800;
      this.callback(
        [
          {
            target: element,
            contentRect: {
              width,
              height,
              x: 0,
              y: 0,
              top: 0,
              right: width,
              bottom: height,
              left: 0,
              toJSON: () => ({}),
            },
          } as ResizeObserverEntry,
        ],
        this as unknown as ResizeObserver,
      );
    };

    unobserve = vi.fn();
    disconnect = vi.fn();
  }

  vi.stubGlobal("ResizeObserver", TestResizeObserver);
}

function node(
  nodeId: string,
  overrides: Partial<RequirementMatrixNode> = {},
): RequirementMatrixNode {
  return {
    node_id: nodeId,
    parent_id: null,
    child_ids: [],
    depth: 0,
    source_order: 0,
    display_role: "STRUCTURE",
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

function fixture() {
  const root = node("root", {
    child_ids: ["clause"],
    label: "4 技术要求",
    source_kind: "SECTION",
    source_number: "4",
    display_title: "技术要求",
  });
  const clause = node("clause", {
    parent_id: "root",
    child_ids: ["atomic-1", "atomic-2"],
    depth: 1,
    source_order: 1,
    display_role: "CLAUSE",
    label: "4.1 热性能",
    source_kind: "CLAUSE",
    source_number: "4.1",
    display_title: "热性能",
    display_text: "父条款完整正文：控制器应满足全部热性能要求。",
    summary: "父条款完整正文：控制器应满足全部热性能要求。",
    content_blocks: [
      {
        id: "clause-copy",
        kind: "text",
        text: "父条款完整正文：控制器应满足全部热性能要求。",
      },
    ],
  });
  const atomic1 = node("atomic-1", {
    parent_id: "clause",
    depth: 2,
    source_order: 2,
    display_role: "ATOMIC_REQUIREMENT",
    label: "高温验证",
    source_kind: "REQUIREMENT",
    source_number: "4.1.1",
    display_title: "高温验证",
    display_text: "控制器应通过 85°C 高温验证。",
    requirement_code: "REQ-0041",
    requirement_id: "requirement-1",
    requirement_text: "控制器应通过 85°C 高温验证。",
    disposition: "ANALYZE",
    analysis_eligibility: "ELIGIBLE",
    analysis_status: "ASSESSED",
  });
  const atomic2 = node("atomic-2", {
    parent_id: "clause",
    depth: 2,
    source_order: 3,
    display_role: "ATOMIC_REQUIREMENT",
    label: "低温验证",
    source_kind: "REQUIREMENT",
    source_number: "4.1.2",
    display_title: "低温验证",
    display_text: "控制器应通过低温验证。",
    requirement_code: "REQ-0042",
    requirement_id: "requirement-2",
    requirement_text: "控制器应通过低温验证。",
    disposition: "ANALYZE",
    analysis_eligibility: "ELIGIBLE",
    analysis_status: "NOT_STARTED",
  });
  const nodes = [root, clause, atomic1, atomic2];
  const matrix: RequirementMatrixSnapshot = {
    schema_version: "requirement-matrix-snapshot/v1",
    state: "READY",
    project_id: "project",
    document_id: "document",
    requirement_set_id: "set",
    requirement_set_status: "LOCKED",
    tree_sha256: "a".repeat(64),
    root_node_ids: ["root"],
    nodes,
    logical_tables: [],
    counts: {
      total_nodes: 4,
      visible_nodes: 4,
      system_non_business_nodes: 0,
      human_excluded_nodes: 0,
      analyze_nodes: 2,
      retained_nodes: 2,
      excluded_nodes: 0,
      analysis_eligible_nodes: 2,
      assessed_nodes: 1,
      decided_nodes: 0,
      logical_tables: 0,
    },
  };
  return { matrix, root, clause, atomic1, atomic2 };
}

function fixtureWithBusinessExclusion() {
  const base = fixture();
  const excluded = node("excluded-business", {
    parent_id: "root",
    depth: 1,
    source_order: 4,
    display_role: "CONTEXT",
    source_kind: "DIRECT_CONTENT",
    label: "报价说明",
    matrix_visibility: "HUMAN_EXCLUDED",
    disposition: "EXCLUDE",
  });
  const system = node("system-cover", {
    parent_id: "root",
    depth: 1,
    source_order: 5,
    display_role: "CONTEXT",
    label: "系统封面",
    matrix_visibility: "SYSTEM_NON_BUSINESS",
  });
  const root = {
    ...base.root,
    child_ids: [...base.root.child_ids, excluded.node_id, system.node_id],
  };
  const matrix: RequirementMatrixSnapshot = {
    ...base.matrix,
    nodes: [root, base.clause, base.atomic1, base.atomic2, excluded, system],
    counts: {
      ...base.matrix.counts,
      total_nodes: 6,
      visible_nodes: 4,
      system_non_business_nodes: 1,
      human_excluded_nodes: 1,
      excluded_nodes: 1,
    },
  };
  return { ...base, matrix, root, excluded, system };
}

function installAnalysisScopePreview(matrix: RequirementMatrixSnapshot) {
  const targetNodes = matrix.nodes.filter(
    (item) =>
      item.matrix_visibility === "VISIBLE" &&
      item.disposition === "ANALYZE" &&
      item.analysis_eligibility === "ELIGIBLE",
  );
  return vi
    .spyOn(api, "previewAiScope")
    .mockImplementation(async (_projectId, _documentId, request) => {
      if (request.scope === "FAILED_ONLY") {
        throw new Error("v1 fixture does not support failed-only preview");
      }
      const requestedIds = request.scope === "ALL" ? [] : request.node_ids;
      const requestedSet = new Set(requestedIds);
      const selectedTargets =
        request.scope === "ALL"
          ? targetNodes
          : targetNodes.filter((item) => requestedSet.has(item.node_id));
      const skippedCount =
        request.scope === "ALL"
          ? Math.max(matrix.counts.visible_nodes - selectedTargets.length, 0)
          : Math.max(requestedIds.length - selectedTargets.length, 0);
      const skippedReasonCounts: Record<string, number> = skippedCount
        ? { NOT_ELIGIBLE: skippedCount }
        : {};
      return {
        schema_version: "requirement-analysis-scope-preview/v1",
        command_id: request.command_id,
        requirement_set_id: request.requirement_set_id,
        requirement_set_version: 1,
        tree_sha256: request.expected_tree_sha256,
        scope: request.scope,
        scope_sha256: "c".repeat(64),
        requested_node_ids: requestedIds,
        target_node_ids: selectedTargets.map((item) => item.node_id),
        target_requirement_ids: selectedTargets.map((item) =>
          String(item.requirement_id || ""),
        ),
        requested_count:
          request.scope === "ALL"
            ? matrix.counts.visible_nodes
            : requestedIds.length,
        target_count: selectedTargets.length,
        skipped_count: skippedCount,
        skipped_reason_counts: skippedReasonCounts,
        target_examples: selectedTargets.map((item) => ({
          node_id: item.node_id,
          requirement_id: String(item.requirement_id || ""),
          requirement_code: item.requirement_code,
          label: item.label,
        })),
        skipped_examples: [],
        publication_mode: "ATOMIC_BATCH",
      };
    });
}

function lockReadiness(
  overrides: Omit<Partial<RequirementSetLockReadiness>, "counts"> & {
    counts?: Partial<RequirementSetLockReadiness["counts"]>;
  } = {},
): RequirementSetLockReadiness {
  const { counts, ...rest } = overrides;
  return {
    schema_version: "requirement-set-lock-readiness/v1",
    readiness_sha256: "readiness-1",
    can_lock: true,
    next_destination: "ANALYSIS",
    count_metadata: {
      content_classification: "AVAILABLE",
      entry_composition: "AVAILABLE",
      source: "FROZEN_TREE",
    },
    counts: {
      visible_content: 4,
      analysis_targets: 2,
      retained_visible: 2,
      excluded_visible: 0,
      skipped_visible: 2,
      technical_source_entries: 2,
      non_technical_source_entries: 1,
      information_source_entries: 1,
      source_entries: 2,
      atomic_requirements: 2,
      ...counts,
    },
    hard_blockers: [],
    warnings: [],
    ...rest,
  };
}

function workspaceValue(requirementSetId = "set"): WorkspaceContextValue {
  const noop = async () => {};
  return {
    route: {
      task: "analysis",
      mode: "review",
      projectId: "project",
      documentId: "document",
      requirementSetId,
    },
    updateRoute: vi.fn(),
    projects: [],
    documents: [],
    knowledge: [],
    workspace: null,
    requirementSets: {
      active_requirement_set_id: requirementSetId,
      requirement_sets: [
        { id: requirementSetId, version_label: "版本 1", status: "LOCKED" },
      ],
    },
    extraction: null,
    structure: null,
    structureOptimization: null,
    aiStatus: {
      capabilities: {
        schema_version: "ai-analysis-capabilities/v1",
        analysis_scope_contract: "requirement-analysis-scope/v1",
        scope_preview: true,
        scoped_start: true,
        atomic_batch_publication: true,
      },
    },
    exports: [],
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
      page_count: 1,
      source_sha256: "b".repeat(64),
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

function recoverableWorkspace(
  job: JsonRecord | null = null,
): WorkspaceContextValue {
  const value = workspaceValue();
  value.aiStatus = {
    capabilities: {
      schema_version: "ai-analysis-capabilities/v2",
      analysis_scope_contract: "requirement-analysis-scope/v2",
      scope_preview: true,
      scoped_start: true,
      per_node_recoverable_publication: true,
      node_execution_status: true,
      failed_only_retry: true,
      controlled_parallelism: true,
    },
    ...(job ? { job } : {}),
  };
  return value;
}

function nodeExecution(
  nodeValue: RequirementMatrixNode,
  status:
    "WAITING" | "RUNNING" | "PUBLISHING" | "SUCCEEDED" | "FAILED" | "CANCELLED",
  overrides: JsonRecord = {},
): JsonRecord {
  return {
    execution_id: `execution-${nodeValue.node_id}`,
    target_id: `target-${nodeValue.node_id}`,
    job_id: "recoverable-job",
    node_id: nodeValue.node_id,
    requirement_id: nodeValue.requirement_id,
    requirement_code: nodeValue.requirement_code,
    source_order: nodeValue.source_order,
    attempt_no: 1,
    status,
    stage: status.toLowerCase(),
    published: status === "SUCCEEDED",
    retryable: status === "FAILED",
    error_code: status === "FAILED" ? "provider_timeout" : null,
    error_message: status === "FAILED" ? "模型响应超时" : null,
    started_at: status === "WAITING" ? null : "2026-08-08T08:00:00Z",
    finished_at: ["SUCCEEDED", "FAILED", "CANCELLED"].includes(status)
      ? "2026-08-08T08:01:00Z"
      : null,
    ...overrides,
  };
}

function installReviewMatrixApis(
  matrix: RequirementMatrixSnapshot,
  selectedNode: RequirementMatrixNode,
) {
  vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
  vi.spyOn(api, "sourcePage").mockResolvedValue({});
  vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
  vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
    schema_version: "requirement-matrix-node-detail/v1",
    project_id: matrix.project_id,
    document_id: matrix.document_id,
    requirement_set_id: matrix.requirement_set_id,
    node: selectedNode,
    path: [selectedNode],
    tree_node: {},
    requirement: null,
    assessment: null,
    logical_tables: [],
  });
}

function editableReviewWorkspace(): WorkspaceContextValue {
  const value = workspaceValue();
  value.route = { ...value.route, task: "extraction", mode: "review" };
  value.requirementSets = {
    active_requirement_set_id: "set",
    requirement_sets: [{ id: "set", version_label: "版本 1", status: "DRAFT" }],
  };
  return value;
}

describe("RequirementMatrixWorkbench", () => {
  it("keeps the requirement-set selector fixed while a set-bound action is running", async () => {
    const { matrix, root } = fixture();
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(
      { ...matrix, requirement_set_status: "DRAFT" },
      root,
    );
    const value = editableReviewWorkspace();
    value.busyAction = "锁定确认清单";

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("combobox", { name: "需求版本" }),
    ).toBeDisabled();
  });

  it("keeps a late single-row save from overwriting a newer requirement-set save", async () => {
    const { matrix, atomic1 } = fixture();
    const matrixA: RequirementMatrixSnapshot = {
      ...matrix,
      requirement_set_id: "set-a",
      requirement_set_status: "DRAFT",
    };
    const atomicB = {
      ...atomic1,
      label: "B 版本高温验证",
      display_title: "B 版本高温验证",
      display_text: "B 版本要求。",
      requirement_text: "B 版本要求。",
    };
    const matrixB: RequirementMatrixSnapshot = {
      ...matrix,
      requirement_set_id: "set-b",
      requirement_set_status: "DRAFT",
      tree_sha256: "b".repeat(64),
      nodes: matrix.nodes.map((candidate) =>
        candidate.node_id === atomicB.node_id ? atomicB : candidate,
      ),
    };
    const lateA = deferred<JsonRecord>();
    const currentB = deferred<JsonRecord>();
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    vi.spyOn(api, "requirementMatrix").mockImplementation(
      async (_projectId, _documentId, requirementSetId) =>
        requirementSetId === "set-a" ? matrixA : matrixB,
    );
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockImplementation(
      async (_projectId, _documentId, requirementSetId, nodeId) => {
        const scopedMatrix = requirementSetId === "set-a" ? matrixA : matrixB;
        const scopedNode =
          scopedMatrix.nodes.find((candidate) => candidate.node_id === nodeId) ||
          scopedMatrix.nodes[0]!;
        return {
          schema_version: "requirement-matrix-node-detail/v1",
          project_id: "project",
          document_id: "document",
          requirement_set_id: requirementSetId,
          node: scopedNode,
          path: [scopedNode],
          tree_node: {},
          requirement: null,
          assessment: null,
          logical_tables: [],
        };
      },
    );
    const saveRequest = vi
      .spyOn(api, "updateRequirementMatrixDisposition")
      .mockImplementation(
        async (_projectId, _documentId, requirementSetId) =>
          requirementSetId === "set-a" ? lateA.promise : currentB.promise,
      );
    const workspaceFor = (requirementSetId: string) => {
      const value = editableReviewWorkspace();
      value.route = { ...value.route, requirementSetId };
      value.requirementSets = {
        active_requirement_set_id: requirementSetId,
        requirement_sets: [
          { id: "set-a", version_label: "版本 A", status: "DRAFT" },
          { id: "set-b", version_label: "版本 B", status: "DRAFT" },
        ],
      };
      return value;
    };
    const user = userEvent.setup();
    const view = render(
      <WorkspaceProvider value={workspaceFor("set-a")}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("treeitem", { name: /高温验证/ }));
    await user.click(screen.getByRole("button", { name: "确认处置" }));
    await user.click(screen.getByRole("radio", { name: /仅保留/ }));
    await user.click(screen.getByRole("button", { name: /^保存/ }));
    await waitFor(() => expect(saveRequest).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("combobox", { name: "需求版本" })).toBeDisabled();

    view.rerender(
      <WorkspaceProvider value={workspaceFor("set-b")}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );
    await user.click(
      await screen.findByRole("treeitem", { name: /B 版本高温验证/ }),
    );
    await user.click(screen.getByRole("button", { name: "确认处置" }));
    await user.click(screen.getByRole("radio", { name: /仅保留/ }));
    await user.click(screen.getByRole("button", { name: /^保存/ }));
    await waitFor(() => expect(saveRequest).toHaveBeenCalledTimes(2));

    await act(async () => {
      lateA.resolve({
        node: {
          ...atomic1,
          label: "A 迟到污染",
          display_title: "A 迟到污染",
          disposition: "RETAIN",
          save_revision: 1,
        },
        counts: matrixA.counts,
      });
      await Promise.resolve();
    });
    expect(
      screen.queryByRole("treeitem", { name: /A 迟到污染/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /保存中/ })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "需求版本" })).toBeDisabled();

    await act(async () => {
      currentB.resolve({
        node: {
          ...atomicB,
          label: "B 当前已保存",
          display_title: "B 当前已保存",
          disposition: "RETAIN",
          save_revision: 1,
        },
        counts: matrixB.counts,
      });
      await Promise.resolve();
    });
    expect(
      await screen.findByRole("treeitem", { name: /B 当前已保存/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "需求版本" })).toBeEnabled();
  });

  it("ignores late batch apply and undo responses after the requirement set changes", async () => {
    const base = fixture();
    const scopedMatrix = (
      requirementSetId: string,
      prefix: string,
      treeSha256: string,
    ): RequirementMatrixSnapshot => ({
      ...base.matrix,
      requirement_set_id: requirementSetId,
      requirement_set_status: "DRAFT",
      tree_sha256: treeSha256,
      nodes: base.matrix.nodes.map((candidate) =>
        candidate.node_id === base.atomic1.node_id ||
        candidate.node_id === base.atomic2.node_id
          ? {
              ...candidate,
              label: `${prefix} ${candidate.label}`,
              display_title: `${prefix} ${candidate.display_title}`,
              disposition: "RETAIN",
              analysis_status: "NOT_STARTED",
            }
          : candidate,
      ),
      counts: {
        ...base.matrix.counts,
        analyze_nodes: 0,
        retained_nodes: 4,
        assessed_nodes: 0,
      },
    });
    const matrixA = scopedMatrix("set-a", "A", "a".repeat(64));
    const matrixB = scopedMatrix("set-b", "B", "b".repeat(64));
    const aApply = deferred<JsonRecord>();
    const bApply = deferred<JsonRecord>();
    const bUndo = deferred<JsonRecord>();
    const aCurrentApply = deferred<JsonRecord>();
    let aRequestCount = 0;
    let bRequestCount = 0;
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    vi.spyOn(api, "requirementMatrix").mockImplementation(
      async (_projectId, _documentId, requirementSetId) =>
        requirementSetId === "set-a" ? matrixA : matrixB,
    );
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockImplementation(
      async (_projectId, _documentId, requirementSetId, nodeId) => {
        const matrix = requirementSetId === "set-a" ? matrixA : matrixB;
        const scopedNode =
          matrix.nodes.find((candidate) => candidate.node_id === nodeId) ||
          matrix.nodes[0]!;
        return {
          schema_version: "requirement-matrix-node-detail/v1",
          project_id: "project",
          document_id: "document",
          requirement_set_id: requirementSetId,
          node: scopedNode,
          path: [scopedNode],
          tree_node: {},
          requirement: null,
          assessment: null,
          logical_tables: [],
        };
      },
    );
    const batchRequest = vi
      .spyOn(api, "updateRequirementMatrixDispositions")
      .mockImplementation(
        async (_projectId, _documentId, requirementSetId) => {
          if (requirementSetId === "set-a") {
            aRequestCount += 1;
            return aRequestCount === 1 ? aApply.promise : aCurrentApply.promise;
          }
          bRequestCount += 1;
          return bRequestCount === 1 ? bApply.promise : bUndo.promise;
        },
      );
    const workspaceFor = (requirementSetId: string) => {
      const value = editableReviewWorkspace();
      value.route = { ...value.route, requirementSetId };
      value.requirementSets = {
        active_requirement_set_id: requirementSetId,
        requirement_sets: [
          { id: "set-a", version_label: "版本 A", status: "DRAFT" },
          { id: "set-b", version_label: "版本 B", status: "DRAFT" },
        ],
      };
      return value;
    };
    const changedNodes = (
      matrix: RequirementMatrixSnapshot,
      prefix: string,
      disposition: "ANALYZE" | "RETAIN",
      revision: number,
    ) =>
      matrix.nodes
        .filter((candidate) =>
          [base.atomic1.node_id, base.atomic2.node_id].includes(
            candidate.node_id,
          ),
        )
        .map((candidate) => ({
          ...candidate,
          label: `${prefix} ${candidate.label}`,
          display_title: `${prefix} ${candidate.display_title}`,
          disposition,
          save_revision: revision,
        }));
    const user = userEvent.setup();
    const view = render(
      <WorkspaceProvider value={workspaceFor("set-a")}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "批量处理" }));
    await user.click(screen.getByRole("button", { name: "确认批量应用" }));
    await waitFor(() => expect(batchRequest).toHaveBeenCalledTimes(1));

    view.rerender(
      <WorkspaceProvider value={workspaceFor("set-b")}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );
    await screen.findByRole("treeitem", { name: /B 高温验证/ });
    await user.click(screen.getByRole("button", { name: "批量处理" }));
    await user.click(screen.getByRole("button", { name: "确认批量应用" }));
    await waitFor(() => expect(batchRequest).toHaveBeenCalledTimes(2));

    await act(async () => {
      aApply.resolve({
        nodes: changedNodes(matrixA, "A 迟到污染", "ANALYZE", 1),
        counts: { ...matrixA.counts, analyze_nodes: 2, retained_nodes: 2 },
      });
      await Promise.resolve();
    });
    expect(
      screen.getByRole("button", { name: "正在应用…" }),
    ).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "需求版本" })).toBeDisabled();
    expect(screen.queryByText(/A 迟到污染/)).not.toBeInTheDocument();

    await act(async () => {
      bApply.resolve({
        nodes: changedNodes(matrixB, "B 当前", "ANALYZE", 1),
        counts: { ...matrixB.counts, analyze_nodes: 2, retained_nodes: 2 },
      });
      await Promise.resolve();
    });
    await screen.findByText("已批量处理 2 项");
    await user.click(screen.getByRole("button", { name: "撤销本次批量处理" }));
    await waitFor(() => expect(batchRequest).toHaveBeenCalledTimes(3));

    view.rerender(
      <WorkspaceProvider value={workspaceFor("set-a")}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );
    await screen.findByRole("treeitem", { name: /A 高温验证/ });
    await user.click(screen.getByRole("button", { name: "批量处理" }));
    await user.click(screen.getByRole("button", { name: "确认批量应用" }));
    await waitFor(() => expect(batchRequest).toHaveBeenCalledTimes(4));

    await act(async () => {
      bUndo.resolve({
        nodes: changedNodes(matrixB, "B 迟到撤销污染", "RETAIN", 2),
        counts: matrixB.counts,
      });
      await Promise.resolve();
    });
    expect(
      screen.getByRole("button", { name: "正在应用…" }),
    ).toBeDisabled();
    expect(screen.queryByText(/B 迟到撤销污染/)).not.toBeInTheDocument();

    await act(async () => {
      aCurrentApply.resolve({
        nodes: changedNodes(matrixA, "A 当前", "ANALYZE", 1),
        counts: { ...matrixA.counts, analyze_nodes: 2, retained_nodes: 2 },
      });
      await Promise.resolve();
    });
    expect(await screen.findByText("已批量处理 2 项")).toBeInTheDocument();
  });

  it("routes an analysis page without a confirmation list back to parsing diagnostics", async () => {
    const value = workspaceValue("");
    value.requirementSets = {
      active_requirement_set_id: "",
      requirement_sets: [],
    };
    const matrixRequest = vi.spyOn(api, "requirementMatrix");
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "尚无确认清单" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "前往需求提取" }));
    expect(value.updateRoute).toHaveBeenCalledWith({
      task: "extraction",
      mode: "organize",
      checkpoint: "diagnose",
      requirementSetId: "",
    });
    expect(matrixRequest).not.toHaveBeenCalled();
  });

  it("offers the same recovery when a selected version has no matrix rows", async () => {
    const { matrix } = fixture();
    vi.spyOn(api, "requirementMatrix").mockResolvedValue({
      ...matrix,
      requirement_set_id: "empty-matrix-set",
      root_node_ids: [],
      nodes: [],
      counts: {
        ...matrix.counts,
        total_nodes: 0,
        visible_nodes: 0,
        analyze_nodes: 0,
        retained_nodes: 0,
        analysis_eligible_nodes: 0,
        assessed_nodes: 0,
      },
    });
    const value = workspaceValue("empty-matrix-set");
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "需求矩阵尚未生成" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "前往需求提取" }));
    expect(value.updateRoute).toHaveBeenCalledWith({
      task: "extraction",
      mode: "organize",
      checkpoint: "diagnose",
      requirementSetId: "",
    });
  });

  it("recovers a missing route selection to the active confirmation list", async () => {
    const { matrix } = fixture();
    const recoveredMatrix = {
      ...matrix,
      requirement_set_id: "active-recovery-set",
      root_node_ids: [],
      nodes: [],
    };
    const matrixRequest = vi
      .spyOn(api, "requirementMatrix")
      .mockResolvedValue(recoveredMatrix);
    const value = workspaceValue("active-recovery-set");
    value.route = { ...value.route, requirementSetId: "" };

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await waitFor(() =>
      expect(value.updateRoute).toHaveBeenCalledWith(
        { requirementSetId: "active-recovery-set" },
        { replace: true },
      ),
    );
    expect(matrixRequest).toHaveBeenCalledWith(
      "project",
      "document",
      "active-recovery-set",
    );
    expect(
      screen.queryByRole("heading", { name: "尚无确认清单" }),
    ).not.toBeInTheDocument();
  });

  it("uses the extraction three-pane reading contract for confirmation", async () => {
    const { matrix, root } = fixture();
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });

    const { container } = render(
      <WorkspaceProvider value={workspaceValue()}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("tree", { name: "确认需求层级树" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "确认需求完整内容" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("complementary", { name: "确认需求原 PDF" }),
    ).toBeInTheDocument();
    expect(container.querySelector(".matrix-workbench-grid")).toHaveClass(
      "is-review-extraction",
    );
    expect(
      screen.queryByRole("columnheader", { name: "AI 建议" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "人工结论" }),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("确认清单统计")).toHaveTextContent(
      "2 项需分析",
    );
    expect(screen.getByLabelText("确认清单统计")).toHaveTextContent(
      "2 项不分析",
    );
    expect(
      screen.getByRole("button", { name: "全部收起" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "定位当前" }),
    ).toBeInTheDocument();
  });

  it("counts and shows every business row by default while hiding system content", async () => {
    const { matrix, root } = fixtureWithBusinessExclusion();
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(matrix, root);

    render(
      <WorkspaceProvider value={workspaceValue()}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("treeitem", { name: /报价说明/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("系统封面")).not.toBeInTheDocument();
    expect(screen.getByLabelText("确认清单统计")).toHaveTextContent(
      "5 项清单内容",
    );
    expect(screen.getByLabelText("确认清单统计")).toHaveTextContent(
      "3 项不分析",
    );
  });

  it("keeps excluded business rows selectable from the analysis page", async () => {
    const { matrix, root } = fixtureWithBusinessExclusion();
    const analysisMatrix = {
      ...matrix,
      requirement_set_id: "excluded-filter-set",
    };
    installResizeObserver({ treeWidth: 620, workbenchWidth: 1200 });
    installReviewMatrixApis(analysisMatrix, root);
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspaceValue("excluded-filter-set")}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("row", { name: /报价说明/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("系统封面")).not.toBeInTheDocument();
    await user.click(screen.getByText("更多筛选"));
    await user.click(screen.getByRole("button", { name: "已排除" }));
    expect(
      await screen.findByRole("row", { name: /报价说明/ }),
    ).toHaveAttribute("aria-selected", "false");
  });

  it("shows only contract-valid current AI conclusions in the inspector", async () => {
    const base = fixture();
    const aiAssessment = {
      decision: "ACCEPT",
      compliance_status: "COMPLIANT",
      reason: "可采用的 AI 理由",
      confidence: 0.9,
      assessment_mode: "EVIDENCE",
    };
    const notStarted = node("inspector-not-started", {
      parent_id: base.root.node_id,
      depth: 1,
      source_order: 1,
      display_role: "ATOMIC_REQUIREMENT",
      display_title: "未发布 AI 负载",
      label: "未发布 AI 负载",
      source_number: "1",
      requirement_id: "requirement-not-started",
      requirement_entry_id: "requirement-not-started",
      disposition: "ANALYZE",
      analysis_eligibility: "ELIGIBLE",
      analysis_status: "NOT_STARTED",
      analysis_result_state: "CURRENT",
      ai_assessment: {
        ...aiAssessment,
        reason: "不应展示的未发布理由",
      },
    });
    const reasonOnly = node("inspector-reason-only", {
      ...notStarted,
      node_id: "inspector-reason-only",
      source_order: 2,
      display_title: "只有说明的 AI 负载",
      label: "只有说明的 AI 负载",
      source_number: "2",
      requirement_id: "requirement-reason-only",
      requirement_entry_id: "requirement-reason-only",
      analysis_status: "ASSESSED",
      ai_assessment: {
        ...aiAssessment,
        decision: "",
        compliance_status: "",
        reason: "不应展示的 reason-only 理由",
      },
    });
    const aiDefault = node("inspector-ai-default", {
      ...notStarted,
      node_id: "inspector-ai-default",
      source_order: 3,
      display_title: "AI 默认结论",
      label: "AI 默认结论",
      source_number: "3",
      requirement_id: "requirement-ai-default",
      requirement_entry_id: "requirement-ai-default",
      analysis_status: "ASSESSED",
      ai_assessment: aiAssessment,
    });
    const humanOverride = node("inspector-human-override", {
      ...aiDefault,
      node_id: "inspector-human-override",
      source_order: 4,
      display_title: "人工修改 AI 结论",
      label: "人工修改 AI 结论",
      source_number: "4",
      requirement_id: "requirement-human-override",
      requirement_entry_id: "requirement-human-override",
      analysis_status: "DECIDED",
      manual_decision: {
        decision: "REJECT",
        reason: "人工修正",
        decided_at: "2026-08-08T00:00:00Z",
      },
    });
    const stale = node("inspector-stale", {
      ...notStarted,
      node_id: "inspector-stale",
      source_order: 5,
      display_title: "历史 AI 参考",
      label: "历史 AI 参考",
      source_number: "5",
      requirement_id: "requirement-stale",
      requirement_entry_id: "requirement-stale",
      analysis_result_state: undefined,
      ai_assessment: null,
      stale_ai_assessment: aiAssessment,
    });
    const root = {
      ...base.root,
      child_ids: [
        notStarted.node_id,
        reasonOnly.node_id,
        aiDefault.node_id,
        humanOverride.node_id,
        stale.node_id,
      ],
    };
    const nodes = [
      root,
      notStarted,
      reasonOnly,
      aiDefault,
      humanOverride,
      stale,
    ];
    const matrix: RequirementMatrixSnapshot = {
      ...base.matrix,
      requirement_set_id: "inspector-contract-set",
      nodes,
      counts: {
        ...base.matrix.counts,
        total_nodes: nodes.length,
        visible_nodes: nodes.length,
        analyze_nodes: 5,
        retained_nodes: 1,
        analysis_eligible_nodes: 5,
        assessed_nodes: 2,
        decided_nodes: 1,
        stale_result_nodes: 1,
      },
    };
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockImplementation(
      async (_projectId, _documentId, _setId, nodeId) => {
        const current = nodes.find(
          (candidate) => candidate.node_id === nodeId,
        )!;
        return {
          schema_version: "requirement-matrix-node-detail/v1",
          project_id: "project",
          document_id: "document",
          requirement_set_id: "inspector-contract-set",
          node: current,
          path: current.node_id === root.node_id ? [root] : [root, current],
          tree_node: {},
          requirement: null,
          assessment: {
            evidence_findings: [
              {
                finding: `详情证据 ${current.node_id}`,
                rationale: `详情证据说明 ${current.node_id}`,
              },
            ],
            sub_judgments: [{ label: `详情分项判断 ${current.node_id}` }],
            structured_information_gap: {
              missing_items: [{ item: `详情待补充 ${current.node_id}` }],
            },
          },
          logical_tables: [],
        };
      },
    );
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspaceValue("inspector-contract-set")}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("row", { name: /未发布 AI 负载/ }),
    );
    expect(screen.getByText("尚无可采用的当前 AI 结论")).toBeInTheDocument();
    expect(screen.queryByText("AI 结论 · 默认采用")).not.toBeInTheDocument();
    expect(screen.queryByText("不应展示的未发布理由")).not.toBeInTheDocument();
    expect(screen.queryByText("90%")).not.toBeInTheDocument();
    expect(
      screen.queryByText("详情证据 inspector-not-started"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("详情分项判断 inspector-not-started"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("详情待补充 inspector-not-started"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("row", { name: /只有说明的 AI 负载/ }));
    expect(screen.getByText("尚无可采用的当前 AI 结论")).toBeInTheDocument();
    expect(
      screen.queryByText("不应展示的 reason-only 理由"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("详情证据 inspector-reason-only"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("row", { name: /AI 默认结论/ }));
    expect(screen.getByText("AI 结论 · 默认采用")).toBeInTheDocument();
    expect(screen.getByText("可采用的 AI 理由")).toBeInTheDocument();
    expect(screen.getByText("90%")).toBeInTheDocument();
    expect(
      screen.getByText("详情证据 inspector-ai-default"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("详情分项判断 inspector-ai-default"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("详情待补充 inspector-ai-default"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("row", { name: /人工修改 AI 结论/ }));
    expect(screen.getByText("AI 原结论 · 已被人工修改")).toBeInTheDocument();
    expect(screen.getByText("可采用的 AI 理由")).toBeInTheDocument();
    expect(
      screen.getByText("详情证据 inspector-human-override"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("row", { name: /历史 AI 参考/ }));
    expect(screen.getByText("上版 AI 建议：ACCEPT")).toBeInTheDocument();
    expect(screen.getByText("尚无可采用的当前 AI 结论")).toBeInTheDocument();
    expect(
      screen.queryByText("详情证据 inspector-stale"),
    ).not.toBeInTheDocument();
  });

  it("explains where excluded business content remains available", async () => {
    const { matrix, root } = fixture();
    const draftMatrix = { ...matrix, requirement_set_status: "DRAFT" };
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(draftMatrix, root);
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={editableReviewWorkspace()}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("treeitem", { name: /高温验证/ }));
    await user.click(screen.getByRole("button", { name: "确认处置" }));

    expect(
      screen.getByText(
        "保留来源与版本记录，可在“已排除”筛选中查看；不进入分析。",
      ),
    ).toBeInTheDocument();
  });

  it("enters analysis only after the active confirmation list is locked", async () => {
    const { matrix, root } = fixture();
    const draftMatrix = {
      ...matrix,
      requirement_set_status: "DRAFT",
    };
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(draftMatrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const readinessRequest = vi
      .spyOn(api, "lockReadiness")
      .mockResolvedValue(lockReadiness());
    const lockRequest = vi.spyOn(api, "lockExtraction").mockResolvedValue({
      requirement_set_id: "set",
      status: "LOCKED",
      next_destination: "ANALYSIS",
      counts: lockReadiness().counts,
      locked_requirement_count: 2,
    });
    const startAi = vi.spyOn(api, "startAi").mockResolvedValue({});
    const value = workspaceValue();
    value.route = { ...value.route, task: "extraction", mode: "review" };
    value.requirementSets = {
      active_requirement_set_id: "set",
      requirement_sets: [
        { id: "set", version_label: "版本 1", status: "DRAFT" },
      ],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    const openLock = await screen.findByRole("button", {
      name: "锁定 2 条需求并进入分析",
    });
    expect(readinessRequest).not.toHaveBeenCalled();
    await user.click(openLock);
    expect(lockRequest).not.toHaveBeenCalled();
    expect(readinessRequest).toHaveBeenCalledWith("project", "document", "set");
    expect(
      screen.getByRole("heading", { name: "锁定 2 条需求并进入分析" }),
    ).toBeInTheDocument();
    expect(screen.getByText("技术内容 2")).toBeInTheDocument();
    expect(screen.getByText("拆分后需求 2")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "锁定并进入分析 · 2 项" }),
    );
    expect(lockRequest).toHaveBeenCalledWith(
      "project",
      "document",
      "set",
      "readiness-1",
    );
    await waitFor(() =>
      expect(value.updateRoute).toHaveBeenCalledWith({
        task: "analysis",
        requirementSetId: "set",
      }),
    );
    expect(startAi).not.toHaveBeenCalled();
  });

  it("marks unavailable lock-count metadata as unrecorded without claiming version history", async () => {
    const { matrix, root } = fixture();
    const draftMatrix = { ...matrix, requirement_set_status: "DRAFT" };
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(draftMatrix, root);
    vi.spyOn(api, "lockReadiness").mockResolvedValue(
      lockReadiness({
        count_metadata: {
          content_classification: "LEGACY_UNAVAILABLE",
          entry_composition: "LEGACY_UNAVAILABLE",
          source: "LEGACY_MATRIX",
        },
        counts: {
          technical_source_entries: 0,
          non_technical_source_entries: 0,
          information_source_entries: 0,
          source_entries: 0,
          atomic_requirements: 0,
        },
      }),
    );
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={editableReviewWorkspace()}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "锁定 2 条需求并进入分析" }),
    );
    const details = await screen.findByLabelText("锁定清单明细");
    expect(within(details).getAllByText("未记录")).toHaveLength(2);
    expect(within(details).queryByText("技术内容 0")).not.toBeInTheDocument();
    expect(within(details).queryByText("原文条目 0")).not.toBeInTheDocument();
    expect(screen.getByText("明细来源未证明")).toBeInTheDocument();
    expect(
      screen.getByText(/当前版本没有提供可验证的分类与条目构成来源/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/历史版本/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "锁定并进入分析 · 2 项" }),
    ).toBeEnabled();
  });

  it("keeps real zero lock counts visible when metadata is available", async () => {
    const { matrix, root } = fixture();
    const draftMatrix = { ...matrix, requirement_set_status: "DRAFT" };
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(draftMatrix, root);
    vi.spyOn(api, "lockReadiness").mockResolvedValue(
      lockReadiness({
        count_metadata: {
          content_classification: "AVAILABLE",
          entry_composition: "AVAILABLE",
          source: "FROZEN_TREE",
        },
        counts: {
          technical_source_entries: 0,
          non_technical_source_entries: 0,
          information_source_entries: 0,
          source_entries: 0,
          atomic_requirements: 0,
        },
      }),
    );
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={editableReviewWorkspace()}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "锁定 2 条需求并进入分析" }),
    );
    const details = await screen.findByLabelText("锁定清单明细");
    expect(within(details).getByText("技术内容 0")).toBeInTheDocument();
    expect(within(details).getByText("原文条目 0")).toBeInTheDocument();
    expect(within(details).queryByText("未记录")).not.toBeInTheDocument();
  });

  it("does not present compatibility zeroes as facts when count metadata is absent", async () => {
    const { matrix, root } = fixture();
    const draftMatrix = { ...matrix, requirement_set_status: "DRAFT" };
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(draftMatrix, root);
    vi.spyOn(api, "lockReadiness").mockResolvedValue(
      lockReadiness({
        count_metadata: undefined,
        counts: {
          technical_source_entries: 0,
          non_technical_source_entries: 0,
          information_source_entries: 0,
          source_entries: 0,
          atomic_requirements: 0,
        },
      }),
    );
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={editableReviewWorkspace()}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "锁定 2 条需求并进入分析" }),
    );
    const details = await screen.findByLabelText("锁定清单明细");
    expect(within(details).getAllByText("未记录")).toHaveLength(2);
    expect(within(details).queryByText("技术内容 0")).not.toBeInTheDocument();
    expect(within(details).queryByText("原文条目 0")).not.toBeInTheDocument();
    expect(screen.getByText("明细来源未证明")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "锁定并进入分析 · 2 项" }),
    ).toBeEnabled();
  });

  it("stays on confirmation when locking fails", async () => {
    const { matrix, root } = fixture();
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    vi.spyOn(api, "requirementMatrix").mockResolvedValue({
      ...matrix,
      requirement_set_status: "DRAFT",
    });
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    vi.spyOn(api, "lockReadiness").mockResolvedValue(lockReadiness());
    vi.spyOn(api, "lockExtraction").mockRejectedValue(new Error("锁定失败"));
    const value = workspaceValue();
    value.route = { ...value.route, task: "extraction", mode: "review" };
    value.requirementSets = {
      active_requirement_set_id: "set",
      requirement_sets: [
        { id: "set", version_label: "版本 1", status: "DRAFT" },
      ],
    };
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "锁定 2 条需求并进入分析" }),
    );
    await user.click(
      screen.getByRole("button", { name: "锁定并进入分析 · 2 项" }),
    );
    await waitFor(() => expect(api.lockExtraction).toHaveBeenCalled());
    expect(value.updateRoute).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toHaveAttribute("open");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("清单未能锁定，当前内容没有改变");
    await waitFor(() => expect(alert).toHaveFocus());
    expect(
      screen.getByRole("button", { name: "重新读取摘要" }),
    ).toBeInTheDocument();
  });

  it("shows server blockers and warnings without allowing an unsafe lock", async () => {
    const { matrix, root } = fixture();
    const draftMatrix = { ...matrix, requirement_set_status: "DRAFT" };
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(draftMatrix, root);
    vi.spyOn(api, "lockReadiness").mockResolvedValue(
      lockReadiness({
        can_lock: false,
        hard_blockers: [{ message: "确认清单缺少可验证来源。" }],
        warnings: ["第 8 页存在已知解析风险，可带风险继续。"],
      }),
    );
    const lockRequest = vi.spyOn(api, "lockExtraction").mockResolvedValue({});
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={editableReviewWorkspace()}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "锁定 2 条需求并进入分析" }),
    );
    expect(
      await screen.findByText("确认清单缺少可验证来源。"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("第 8 页存在已知解析风险，可带风险继续。"),
    ).toBeInTheDocument();
    const confirm = screen.getByRole("button", {
      name: "锁定并进入分析 · 2 项",
    });
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(lockRequest).not.toHaveBeenCalled();
  });

  it("keeps a changed checklist in the lock dialog and reloads its summary", async () => {
    const { matrix, root } = fixture();
    const draftMatrix = { ...matrix, requirement_set_status: "DRAFT" };
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(draftMatrix, root);
    const readinessRequest = vi
      .spyOn(api, "lockReadiness")
      .mockResolvedValue(lockReadiness());
    vi.spyOn(api, "lockExtraction").mockRejectedValue(
      new ApiError("conflict", 409, { code: "READINESS_CHANGED" }),
    );
    const value = editableReviewWorkspace();
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "锁定 2 条需求并进入分析" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "锁定并进入分析 · 2 项" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "清单内容已发生变化，请刷新摘要后重新核对锁定范围",
    );
    expect(screen.getByRole("dialog")).toHaveAttribute("open");
    expect(value.updateRoute).not.toHaveBeenCalled();
    const staleConfirm = screen.getByRole("button", {
      name: "锁定并进入分析 · 2 项",
    });
    expect(staleConfirm).toBeDisabled();
    await user.click(staleConfirm);
    expect(api.lockExtraction).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "重新读取摘要" }));
    await waitFor(() => expect(readinessRequest).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(staleConfirm).toBeEnabled();
    await waitFor(() => expect(staleConfirm).toHaveFocus());
  });

  it("translates code-only lock findings into recoverable user guidance", async () => {
    const { matrix, root } = fixture();
    const draftMatrix = { ...matrix, requirement_set_status: "DRAFT" };
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(draftMatrix, root);
    vi.spyOn(api, "lockReadiness").mockResolvedValue(
      lockReadiness({
        can_lock: false,
        hard_blockers: [
          { code: "REQUIREMENT_SET_SELECTION_CHANGED" },
          { code: "REQUIREMENT_MATRIX_REQUIRES_REEXTRACTION" },
          { code: "REQUIREMENT_MATRIX_COUNT_MISMATCH" },
        ],
        warnings: [{ code: "SOURCE_CLASSIFICATION_CONFLICT" }],
      }),
    );
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={editableReviewWorkspace()}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "锁定 2 条需求并进入分析" }),
    );

    expect(
      await screen.findByText(
        "当前确认清单已不是活动版本，请关闭对话框并刷新后重新选择。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "当前需求矩阵无法验证，请返回需求提取重新提取后再锁定。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "清单统计与冻结分析范围不一致，请重新读取摘要；若仍出现，请重新提取。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "同一原文条目的内容分类不一致，建议返回清单整理核对；你也可以带风险继续。",
      ),
    ).toBeInTheDocument();
  });

  it("prevents dismissing the lock dialog while the lock request is running", async () => {
    const { matrix, root } = fixture();
    const draftMatrix = { ...matrix, requirement_set_status: "DRAFT" };
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(draftMatrix, root);
    vi.spyOn(api, "lockReadiness").mockResolvedValue(lockReadiness());
    let resolveLock!: (
      value: Awaited<ReturnType<typeof api.lockExtraction>>,
    ) => void;
    vi.spyOn(api, "lockExtraction").mockReturnValue(
      new Promise((resolve) => {
        resolveLock = resolve;
      }),
    );
    const value = editableReviewWorkspace();
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "锁定 2 条需求并进入分析" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "锁定并进入分析 · 2 项" }),
    );
    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(dialog).toHaveAttribute("aria-busy", "true"));
    expect(screen.getByRole("button", { name: "关闭锁定确认" })).toBeDisabled();
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    fireEvent.click(dialog);
    expect(dialog).toHaveAttribute("open");

    await act(async () => {
      resolveLock({
        requirement_set_id: "set",
        status: "LOCKED",
        next_destination: "ANALYSIS",
        counts: lockReadiness().counts,
        locked_requirement_count: 2,
      });
    });
    await waitFor(() =>
      expect(value.updateRoute).toHaveBeenCalledWith({
        task: "analysis",
        requirementSetId: "set",
      }),
    );
  });

  it("locks a zero-analysis list without entering an empty analysis page", async () => {
    const { matrix, root } = fixture();
    const zeroTargetMatrix: RequirementMatrixSnapshot = {
      ...matrix,
      requirement_set_status: "DRAFT",
      nodes: matrix.nodes.map((item) => ({
        ...item,
        disposition: "RETAIN",
      })),
      counts: {
        ...matrix.counts,
        analyze_nodes: 0,
        // Persisted Matrix counts also include a hidden document root.  The
        // review summary must still partition only human-visible content.
        retained_nodes: matrix.counts.visible_nodes + 1,
      },
    };
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(zeroTargetMatrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    vi.spyOn(api, "lockReadiness").mockResolvedValue(
      lockReadiness({
        next_destination: "MATRIX",
        counts: {
          analysis_targets: 0,
          retained_visible: 4,
          skipped_visible: 4,
        },
      }),
    );
    const lockRequest = vi.spyOn(api, "lockExtraction").mockResolvedValue({
      requirement_set_id: "set",
      status: "LOCKED",
      next_destination: "MATRIX",
      locked_requirement_count: 0,
    });
    const deriveRequest = vi
      .spyOn(api, "deriveRequirementSet")
      .mockResolvedValue({
        project_id: "project",
        document_id: "document",
        requirement_set: { id: "set-draft", status: "DRAFT" },
        created: true,
        impact_receipt: null,
        affected_result_count: 0,
      });
    const value = workspaceValue();
    value.route = { ...value.route, task: "extraction", mode: "review" };
    value.requirementSets = {
      active_requirement_set_id: "set",
      requirement_sets: [
        { id: "set", version_label: "版本 1", status: "DRAFT" },
      ],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("4 项不分析")).toBeInTheDocument();
    expect(screen.queryByText("5 项不分析")).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "锁定清单并查看矩阵" }),
    );
    expect(lockRequest).not.toHaveBeenCalled();
    expect(
      await screen.findByText("当前没有需要分析的内容"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "锁定清单" }));

    await waitFor(() => expect(lockRequest).toHaveBeenCalled());
    expect(value.updateRoute).not.toHaveBeenCalledWith(
      expect.objectContaining({ task: "analysis" }),
    );
    expect(
      await screen.findByText("清单已锁定，没有待分析项"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "解锁并修改" }));
    expect(deriveRequest).not.toHaveBeenCalled();
    expect(
      within(screen.getByRole("dialog")).getAllByRole("heading", {
        name: "解锁并修改",
      }),
    ).not.toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "保持锁定" }));

    await user.click(screen.getByRole("button", { name: "前往矩阵导出" }));
    expect(value.updateRoute).toHaveBeenCalledWith({
      task: "export",
      requirementSetId: "set",
    });
  });

  it("uses the server lock receipt when the frozen analysis count changed", async () => {
    const { matrix, root } = fixture();
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    vi.spyOn(api, "requirementMatrix").mockResolvedValue({
      ...matrix,
      requirement_set_status: "DRAFT",
    });
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    vi.spyOn(api, "lockReadiness").mockResolvedValue(lockReadiness());
    vi.spyOn(api, "lockExtraction").mockResolvedValue({
      requirement_set_id: "set",
      status: "LOCKED",
      next_destination: "MATRIX",
      counts: {
        ...lockReadiness().counts,
        analysis_targets: 0,
      },
      locked_requirement_count: 0,
    });
    const value = workspaceValue();
    value.route = { ...value.route, task: "extraction", mode: "review" };
    value.requirementSets = {
      active_requirement_set_id: "set",
      requirement_sets: [
        { id: "set", version_label: "版本 1", status: "DRAFT" },
      ],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "锁定 2 条需求并进入分析" }),
    );
    await screen.findByText("锁定不会自动运行分析");
    await user.click(
      screen.getByRole("button", { name: "锁定并进入分析 · 2 项" }),
    );

    expect(value.updateRoute).not.toHaveBeenCalledWith(
      expect.objectContaining({ task: "analysis" }),
    );
    expect(
      await screen.findByText("清单已锁定，没有待分析项"),
    ).toBeInTheDocument();
  });

  it("derives a new editable confirmation list from the active locked version", async () => {
    const { matrix, root } = fixture();
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const deriveRequest = vi
      .spyOn(api, "deriveRequirementSet")
      .mockResolvedValue({
        project_id: "project",
        document_id: "document",
        requirement_set: { id: "set-draft", status: "DRAFT" },
        created: true,
        impact_receipt: {
          schema_version: "requirement-set-impact-receipt/v1",
          stale_result_node_ids: [],
          stale_result_count: 0,
        },
        affected_result_count: 0,
      });
    const value = workspaceValue();
    value.route = { ...value.route, task: "extraction", mode: "review" };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "解锁并修改" }));
    expect(deriveRequest).not.toHaveBeenCalled();
    expect(screen.getByText("当前已锁定清单会继续保留")).toBeInTheDocument();
    expect(
      screen.getByText(/未修改内容的分析结果会继续使用/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "解锁并继续修改" }));
    expect(deriveRequest).toHaveBeenCalledWith("project", "document", "set");
    await waitFor(() =>
      expect(value.updateRoute).toHaveBeenCalledWith({
        task: "extraction",
        mode: "review",
        checkpoint: "review",
        requirementSetId: "set-draft",
      }),
    );
  });

  it("keeps the locked checklist open when unlocking fails", async () => {
    const { matrix, root } = fixture();
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(matrix, root);
    vi.spyOn(api, "deriveRequirementSet").mockRejectedValue(
      new Error("derive failed"),
    );
    const value = workspaceValue();
    value.route = { ...value.route, task: "extraction", mode: "review" };
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "解锁并修改" }));
    await user.click(screen.getByRole("button", { name: "解锁并继续修改" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "清单未能解锁，当前已锁定清单没有改变",
    );
    expect(screen.getByRole("dialog")).toHaveAttribute("open");
    expect(value.updateRoute).not.toHaveBeenCalled();
  });

  it("blocks a repeated unlock after conflict and offers an explicit refresh", async () => {
    const { matrix, root } = fixture();
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(matrix, root);
    const deriveRequest = vi
      .spyOn(api, "deriveRequirementSet")
      .mockRejectedValue(
        new ApiError("conflict", 409, { code: "ACTIVE_SET_CHANGED" }),
      );
    const value = workspaceValue();
    value.route = { ...value.route, task: "extraction", mode: "review" };
    value.refreshDocument = vi.fn(async () => {});
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "解锁并修改" }));
    await user.click(screen.getByRole("button", { name: "解锁并继续修改" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "当前清单已发生变化",
    );
    const retryUnlock = screen.getByRole("button", {
      name: "解锁并继续修改",
    });
    expect(retryUnlock).toBeDisabled();
    await user.click(retryUnlock);
    expect(deriveRequest).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "关闭并重新检查" }));
    await waitFor(() => expect(value.refreshDocument).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("provides predictable hierarchy expansion controls in confirmation", async () => {
    const { matrix, root } = fixture();
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspaceValue()}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await screen.findByRole("treeitem", { name: /4，技术要求/ });
    await user.click(screen.getByRole("button", { name: "全部收起" }));
    expect(
      screen.queryByRole("treeitem", { name: /4\.1，热性能/ }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByText("展开", { selector: "summary" }));
    expect(
      screen.getByRole("button", { name: "展开 2 层" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "展开 3 层" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "全部展开" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "只保留当前路径" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "展开 3 层" }));
    expect(
      await screen.findByRole("treeitem", { name: /4\.1\.1，高温验证/ }),
    ).toBeInTheDocument();
  });

  it("keeps deep source numbers complete in the confirmation tree", async () => {
    const { matrix, root, atomic1 } = fixture();
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    const deepNumber = "5.1.8.2.3";
    const deepNode = { ...atomic1, source_number: deepNumber };
    const deepMatrix = {
      ...matrix,
      nodes: matrix.nodes.map((candidate) =>
        candidate.node_id === atomic1.node_id ? deepNode : candidate,
      ),
    };
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(deepMatrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });

    render(
      <WorkspaceProvider value={workspaceValue()}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    const deepRow = await screen.findByRole("treeitem", {
      name: new RegExp(deepNumber.replaceAll(".", "\\.")),
    });
    const number = deepRow.querySelector(".matrix-review-tree-number");
    expect(number).toHaveTextContent(deepNumber);
    expect(number).toHaveAttribute("title", deepNumber);
  });

  it("uses the actual matrix pane width to collapse analysis-only columns", async () => {
    const { matrix, root } = fixture();
    installResizeObserver({ treeWidth: 620, workbenchWidth: 1064 });
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });

    render(
      <WorkspaceProvider value={workspaceValue()}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    const grid = await screen.findByRole("treegrid", {
      name: "需求层级与分析状态",
    });
    await waitFor(() => expect(grid).toHaveAttribute("aria-colcount", "3"));
    expect(
      screen.queryByRole("columnheader", { name: "AI 建议" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "人工结论" }),
    ).not.toBeInTheDocument();
  });

  it("preserves momentum scrolling and keeps results visible after the list shrinks", async () => {
    const base = fixture().matrix;
    const nodes = Array.from({ length: 120 }, (_, index) =>
      node(`node-${index + 1}`, {
        source_order: index,
        label: `${index + 1} 长文档条目${index === 0 ? " 唯一核对样本" : ""}`,
      }),
    );
    const matrix: RequirementMatrixSnapshot = {
      ...base,
      requirement_set_id: "scroll-set",
      root_node_ids: nodes.map((candidate) => candidate.node_id),
      nodes,
      counts: {
        ...base.counts,
        total_nodes: nodes.length,
        visible_nodes: nodes.length,
        analyze_nodes: 0,
        retained_nodes: nodes.length,
        analysis_eligible_nodes: 0,
        assessed_nodes: 0,
      },
    };
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockImplementation(
      async (_projectId, _documentId, _setId, nodeId) => ({
        schema_version: "requirement-matrix-node-detail/v1",
        project_id: "project",
        document_id: "document",
        requirement_set_id: "scroll-set",
        node:
          nodes.find((candidate) => candidate.node_id === nodeId) || nodes[0],
        path: [],
        tree_node: {},
        requirement: null,
        assessment: null,
        logical_tables: [],
      }),
    );
    let animationFrameId = 0;
    const animationFrames = new Map<number, FrameRequestCallback>();
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      animationFrameId += 1;
      animationFrames.set(animationFrameId, callback);
      return animationFrameId;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation((frameId) => {
      animationFrames.delete(frameId);
    });
    const flushAnimationFrames = () => {
      const pending = Array.from(animationFrames.values());
      animationFrames.clear();
      pending.forEach((callback) => callback(performance.now()));
    };

    const view = render(
      <WorkspaceProvider value={workspaceValue("scroll-set")}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    await screen.findByRole("row", { name: /^1 长文档条目 唯一核对样本，/ });
    act(flushAnimationFrames);
    const scroller = view.container.querySelector<HTMLDivElement>(
      ".matrix-treegrid-scroll",
    );
    expect(scroller).not.toBeNull();

    scroller!.scrollTop = 640;
    fireEvent.scroll(scroller!);
    act(flushAnimationFrames);
    scroller!.scrollTop = 704;
    act(flushAnimationFrames);

    expect(scroller).toHaveProperty("scrollTop", 704);

    scroller!.scrollTop = 4000;
    fireEvent.scroll(scroller!);
    act(flushAnimationFrames);
    const search = screen.getByRole("searchbox", { name: "搜索需求矩阵" });
    fireEvent.change(search, { target: { value: "唯一核对样本" } });
    await screen.findByRole("row", { name: /^1 长文档条目 唯一核对样本，/ });
    expect(scroller).toHaveProperty("scrollTop", 0);
    fireEvent.change(search, { target: { value: "" } });
    await screen.findByRole("row", { name: /^1 长文档条目 唯一核对样本，/ });
    expect(scroller).toHaveProperty("scrollTop", 0);
  });

  it("renders source number, readable content and hierarchy as separate columns", async () => {
    const { matrix, root } = fixture();
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });

    render(
      <WorkspaceProvider value={workspaceValue()}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("columnheader", { name: "编号" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "内容" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "层级" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "编号与内容" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "内容性质" }),
    ).not.toBeInTheDocument();

    const atomicRow = await screen.findByRole("row", {
      name: /4\.1\.1，高温验证，原子需求/,
    });
    const cells = within(atomicRow).getAllByRole("gridcell");
    expect(cells[0]).toHaveTextContent(/^4\.1\.1$/);
    expect(cells[1]).toHaveTextContent("高温验证");
    expect(cells[1]).toHaveTextContent("控制器应通过 85°C 高温验证。");
    expect(cells[2]).toHaveTextContent(/^原子需求$/);
    expect(atomicRow).not.toHaveTextContent("REQ-0041");
  });

  it("does not ask to rerun stale history that is no longer an analysis target", async () => {
    const { matrix, root, atomic1 } = fixture();
    const currentAssessment = {
      decision: "ACCEPT",
      compliance_status: "COMPLIANT",
      reason: "上版已分析",
      confidence: 0.9,
      assessment_mode: "EVIDENCE",
    };
    const currentAtomic = {
      ...atomic1,
      analysis_result_state: "CURRENT" as const,
      ai_assessment: currentAssessment,
    };
    const reviewMatrix: RequirementMatrixSnapshot = {
      ...matrix,
      requirement_set_status: "DRAFT",
      nodes: matrix.nodes.map((candidate) =>
        candidate.node_id === atomic1.node_id ? currentAtomic : candidate,
      ),
    };
    const staleRetained = {
      ...currentAtomic,
      disposition: "RETAIN" as const,
      analysis_status: "NOT_APPLICABLE" as const,
      analysis_result_state: "STALE_REFERENCE" as const,
      ai_assessment: null,
      stale_ai_assessment: currentAssessment,
      save_revision: 1,
    };
    installResizeObserver({ treeWidth: 350, workbenchWidth: 1200 });
    installReviewMatrixApis(reviewMatrix, root);
    const dispositionRequest = vi
      .spyOn(api, "updateRequirementMatrixDisposition")
      .mockResolvedValue({
        node: staleRetained,
        counts: {
          ...reviewMatrix.counts,
          analyze_nodes: 1,
          retained_nodes: 3,
          assessed_nodes: 0,
          stale_result_nodes: 1,
        },
        impact_receipt: {
          stale_result_node_ids: [atomic1.node_id],
          stale_result_count: 1,
        },
      });
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={editableReviewWorkspace()}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("treeitem", { name: /高温验证/ }));
    await user.click(screen.getByRole("button", { name: "确认处置" }));
    await user.click(screen.getByRole("radio", { name: /仅保留/ }));
    await user.click(screen.getByRole("button", { name: /^保存/ }));

    await waitFor(() => expect(dispositionRequest).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("本次修改后没有分析结果需要重跑"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("本次修改后有 1 条分析结果需要重跑"),
    ).not.toBeInTheDocument();
  });

  it("previews, applies and can undo a scoped batch disposition", async () => {
    const { matrix, root, atomic1, atomic2 } = fixture();
    const retainedAtomic1 = {
      ...atomic1,
      disposition: "RETAIN" as const,
      analysis_status: "NOT_STARTED" as const,
    };
    const retainedAtomic2 = {
      ...atomic2,
      disposition: "RETAIN" as const,
    };
    const reviewMatrix: RequirementMatrixSnapshot = {
      ...matrix,
      requirement_set_status: "DRAFT",
      nodes: matrix.nodes.map((candidate) => {
        if (candidate.node_id === atomic1.node_id) return retainedAtomic1;
        if (candidate.node_id === atomic2.node_id) return retainedAtomic2;
        return candidate;
      }),
      counts: {
        ...matrix.counts,
        analyze_nodes: 0,
        retained_nodes: 4,
        assessed_nodes: 0,
      },
    };
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(reviewMatrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const batchRequest = vi
      .spyOn(api, "updateRequirementMatrixDispositions")
      .mockResolvedValueOnce({
        nodes: [
          {
            ...retainedAtomic1,
            disposition: "ANALYZE",
            analysis_result_state: "STALE_REFERENCE",
            save_revision: 1,
          },
          {
            ...retainedAtomic2,
            disposition: "ANALYZE",
            analysis_result_state: "STALE_REFERENCE",
            save_revision: 1,
          },
        ],
        counts: {
          ...reviewMatrix.counts,
          analyze_nodes: 2,
          retained_nodes: 2,
        },
        impact_receipt: {
          stale_result_node_ids: [atomic1.node_id, atomic2.node_id],
          stale_result_count: 2,
        },
      })
      .mockResolvedValueOnce({
        nodes: [
          { ...retainedAtomic1, save_revision: 2 },
          { ...retainedAtomic2, save_revision: 2 },
        ],
        counts: reviewMatrix.counts,
        impact_receipt: {
          stale_result_node_ids: [],
          stale_result_count: 0,
        },
      });
    const value = workspaceValue();
    value.requirementSets = {
      active_requirement_set_id: "set",
      requirement_sets: [
        { id: "set", version_label: "版本 1", status: "DRAFT" },
      ],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "批量处理" }));
    const preview = screen.getByRole("region", { name: "批量处理正式清单" });
    expect(within(preview).getByLabelText("作用范围")).toHaveValue("filter");
    expect(within(preview).getByLabelText("统一处理为")).toHaveValue("ANALYZE");
    expect(preview).toHaveTextContent("2项将改为“需分析”");
    expect(preview).toHaveTextContent("2 项保持不变");

    await user.click(
      within(preview).getByRole("button", { name: "确认批量应用" }),
    );
    await waitFor(() => expect(batchRequest).toHaveBeenCalledTimes(1));
    expect(batchRequest.mock.calls[0]?.[3]).toMatchObject({
      schema_version: "requirement-matrix-disposition-batch-patch/v1",
      expected_tree_sha256: matrix.tree_sha256,
      items: [
        {
          node_id: atomic1.node_id,
          disposition: "ANALYZE",
          expected_revision: 0,
        },
        {
          node_id: atomic2.node_id,
          disposition: "ANALYZE",
          expected_revision: 0,
        },
      ],
    });
    expect(await screen.findByText("已批量处理 2 项")).toBeInTheDocument();
    expect(
      screen.getByText("本次修改后有 2 条分析结果需要重跑"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/未受影响的分析与人工结论继续保留/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "撤销本次批量处理" }));
    await waitFor(() => expect(batchRequest).toHaveBeenCalledTimes(2));
    expect(batchRequest.mock.calls[1]?.[3]).toMatchObject({
      expected_tree_sha256: matrix.tree_sha256,
      items: [
        {
          node_id: atomic1.node_id,
          disposition: "RETAIN",
          expected_revision: 1,
        },
        {
          node_id: atomic2.node_id,
          disposition: "RETAIN",
          expected_revision: 1,
        },
      ],
    });
    expect(await screen.findByText("已撤销 2 项批量处理")).toBeInTheDocument();
    expect(
      screen.getByText("本次修改后没有分析结果需要重跑"),
    ).toBeInTheDocument();
  });

  it("keeps descending requirement-set IDs aligned with their real versions", async () => {
    const { matrix, root } = fixture();
    const value = workspaceValue("set-v8");
    value.requirementSets = {
      active_requirement_set_id: "set-v8",
      requirement_sets: [
        { id: "set-v8", version: 8, status: "LOCKED" },
        { id: "set-v7", version_number: 7, status: "LOCKED" },
      ],
    };
    vi.spyOn(api, "requirementMatrix").mockResolvedValue({
      ...matrix,
      requirement_set_id: "set-v8",
    });
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set-v8",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    const versionSelect = await screen.findByRole("combobox", {
      name: "需求版本",
    });
    expect(screen.getByRole("option", { name: "版本 8 · 当前" })).toHaveValue(
      "set-v8",
    );
    expect(screen.getByRole("option", { name: "版本 7" })).toHaveValue(
      "set-v7",
    );
    await user.selectOptions(versionSelect, "set-v7");
    expect(value.updateRoute).toHaveBeenCalledWith({
      requirementSetId: "set-v7",
    });
  });

  it("keeps hierarchy containers as read-only rollups instead of per-row forms", async () => {
    const { matrix, root } = fixture();
    const rootWithReason = {
      ...root,
      disposition_reason: "保留完整章节层级",
    };
    const reviewMatrix = {
      ...matrix,
      requirement_set_status: "DRAFT",
      nodes: matrix.nodes.map((candidate) =>
        candidate.node_id === root.node_id ? rootWithReason : candidate,
      ),
    };
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(reviewMatrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: rootWithReason,
      path: [rootWithReason],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const dispositionRequest = vi
      .spyOn(api, "updateRequirementMatrixDisposition")
      .mockResolvedValue({
        node: { ...rootWithReason, save_revision: 1 },
        counts: reviewMatrix.counts,
      });
    const value = workspaceValue();
    value.requirementSets = {
      active_requirement_set_id: "set",
      requirement_sets: [
        { id: "set", version_label: "版本 1", status: "DRAFT" },
      ],
    };

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "确认处置" }));
    expect(await screen.findByText("内容分组无需单独设置")).toBeInTheDocument();
    expect(
      screen.queryByRole("radio", { name: /仅保留/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("处置说明（可选）")).not.toBeInTheDocument();
    expect(
      screen.getByText(/使用清单上方的批量处理一次应用到当前范围/),
    ).toBeInTheDocument();
    await user.click(screen.getByText("更多", { selector: "summary" }));
    expect(screen.getByRole("button", { name: "已排除" })).toBeInTheDocument();
    expect(dispositionRequest).not.toHaveBeenCalled();
  });

  it("refreshes the live matrix when the current AI job reaches a terminal state", async () => {
    const { matrix, root } = fixture();
    const matrixRequest = vi
      .spyOn(api, "requirementMatrix")
      .mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const running = workspaceValue();
    running.aiStatus = {
      job: { id: "ai-job", status: "RUNNING", requirement_set_id: "set" },
    };
    const view = render(
      <WorkspaceProvider value={running}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    await waitFor(() => expect(matrixRequest).toHaveBeenCalledTimes(1));

    const completed = workspaceValue();
    completed.aiStatus = {
      job: { id: "ai-job", status: "SUCCEEDED", requirement_set_id: "set" },
    };
    view.rerender(
      <WorkspaceProvider value={completed}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await waitFor(() => expect(matrixRequest).toHaveBeenCalledTimes(2));
  });

  it("keeps historical locked versions read-only instead of offering an invalid AI run", async () => {
    const { matrix, root } = fixture();
    vi.spyOn(api, "requirementMatrix").mockResolvedValue({
      ...matrix,
      requirement_set_id: "history",
    });
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "history",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const value = workspaceValue("history");
    value.requirementSets = {
      active_requirement_set_id: "set",
      requirement_sets: [
        { id: "set", version_label: "版本 2", status: "LOCKED" },
        { id: "history", version_label: "版本 1", status: "LOCKED" },
      ],
    };

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(await screen.findByText("历史清单只读")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "分析全部 2 项" }),
    ).not.toBeInTheDocument();
  });

  it("only enables AI analysis for a ready configured profile", async () => {
    const { matrix, root } = fixture();
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const startAi = vi
      .spyOn(api, "startAi")
      .mockResolvedValue({ status: "QUEUED" });
    const previewAi = installAnalysisScopePreview(matrix);
    const value = workspaceValue();
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByText("4 项可见内容 · 2 项分析范围"),
    ).toBeInTheDocument();
    const runButton = await screen.findByRole("button", {
      name: "分析全部 2 项",
    });
    expect(runButton).toBeEnabled();
    expect(screen.queryByText(/智能分析未配置/)).not.toBeInTheDocument();
    await user.click(runButton);
    expect(startAi).not.toHaveBeenCalled();
    expect(
      await screen.findByRole("heading", { name: "全部分析项 · 2 项" }),
    ).toBeInTheDocument();
    expect(previewAi).toHaveBeenCalledTimes(1);
    expect(screen.getByText("本阶段按整批原子发布")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "开始分析 2 项" }));
    await waitFor(() =>
      expect(startAi).toHaveBeenCalledWith(
        "project",
        "document",
        expect.objectContaining({
          schema_version: "requirement-analysis-scope-request/v1",
          requirement_set_id: "set",
          expected_tree_sha256: "a".repeat(64),
          scope: "ALL",
          node_ids: [],
          expected_scope_sha256: "c".repeat(64),
          profile_id: "ready-profile",
        }),
      ),
    );
  });

  it("shows only the confirmed legacy all action when scoped capability is absent", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    const previewAi = vi.spyOn(api, "previewAiScope");
    const startAi = vi.spyOn(api, "startAi");
    const startLegacy = vi
      .spyOn(api, "startAiLegacy")
      .mockResolvedValue({ status: "PENDING" });
    const value = workspaceValue();
    value.aiStatus = { capabilities: {} };
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "分析当前项" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /分析所选/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/已选 \d+ 项/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("checkbox", { name: /选择分析/ }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "分析全部 2 项" }));
    expect(previewAi).not.toHaveBeenCalled();
    expect(startAi).not.toHaveBeenCalled();
    expect(
      screen.getByRole("heading", { name: "分析全部 2 项" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/当前服务端未回显精确范围能力/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "开始分析全部 2 项" }));
    await waitFor(() =>
      expect(startLegacy).toHaveBeenCalledWith(
        "project",
        "document",
        "set",
        "ready-profile",
      ),
    );
  });

  it("uses the v2 scope contract for a normal recoverable analysis run", async () => {
    const { matrix, root, atomic1, atomic2 } = fixture();
    installReviewMatrixApis(matrix, root);
    const previewAi = vi
      .spyOn(api, "previewAiScope")
      .mockImplementation(async (_projectId, _documentId, request) => {
        if (request.scope === "FAILED_ONLY") {
          throw new Error("expected a normal scope");
        }
        return {
          schema_version: "requirement-analysis-scope-preview/v2",
          command_id: request.command_id,
          requirement_set_id: request.requirement_set_id,
          requirement_set_version: 1,
          tree_sha256: request.expected_tree_sha256,
          scope: request.scope,
          scope_sha256: "e".repeat(64),
          requested_node_ids: request.node_ids,
          target_node_ids: [atomic1.node_id, atomic2.node_id],
          target_requirement_ids: [
            String(atomic1.requirement_id),
            String(atomic2.requirement_id),
          ],
          requested_count: 2,
          target_count: 2,
          skipped_count: 0,
          skipped_reason_counts: {},
          target_examples: [],
          skipped_examples: [],
          publication_mode: "PER_NODE_RECOVERABLE",
          source_job_id: null,
          failed_snapshot_sha256: "",
          source_failed_count: 0,
          already_current_count: 0,
          retry_target_count: 0,
          retry_already_current_count: 0,
          retry_skipped_count: 0,
        };
      });
    const startAi = vi
      .spyOn(api, "startAi")
      .mockResolvedValue({ status: "PENDING" });
    const value = recoverableWorkspace();
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    await user.click(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    );

    expect(previewAi).toHaveBeenCalledWith(
      "project",
      "document",
      expect.objectContaining({
        schema_version: "requirement-analysis-scope-request/v2",
        scope: "ALL",
        node_ids: [],
      }),
    );
    expect(await screen.findByText("成功结果会逐项保留")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "开始分析 2 项" }));
    await waitFor(() =>
      expect(startAi).toHaveBeenCalledWith(
        "project",
        "document",
        expect.objectContaining({
          schema_version: "requirement-analysis-scope-request/v2",
          expected_scope_sha256: "e".repeat(64),
        }),
      ),
    );
  });

  it("fails closed while analysis capability is unknown, then reveals scoped actions", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    const unknown = workspaceValue();
    unknown.aiStatus = null;
    unknown.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };

    const view = render(
      <WorkspaceProvider value={unknown}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "分析能力尚未确认" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "分析全部 2 项" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "分析当前项" }),
    ).not.toBeInTheDocument();

    const scoped = workspaceValue();
    scoped.aiSetup = unknown.aiSetup;
    view.rerender(
      <WorkspaceProvider value={scoped}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "分析当前项" }),
    ).toBeInTheDocument();
  });

  it("fails closed on an incomplete declared v2 capability instead of using legacy start", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    const value = workspaceValue();
    value.aiStatus = {
      capabilities: {
        schema_version: "ai-analysis-capabilities/v2",
        analysis_scope_contract: "requirement-analysis-scope/v2",
        scope_preview: true,
        scoped_start: true,
        per_node_recoverable_publication: true,
        node_execution_status: true,
        failed_only_retry: true,
        controlled_parallelism: false,
      },
    };
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", ready: true }],
    };

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "分析能力尚未确认" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "分析全部 2 项" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("checkbox", { name: /选择分析/ }),
    ).not.toBeInTheDocument();
  });

  it("closes a legacy confirmation if scoped capability arrives before confirmation", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    const startLegacy = vi.spyOn(api, "startAiLegacy");
    const legacy = workspaceValue();
    legacy.aiStatus = { capabilities: {} };
    legacy.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    const view = render(
      <WorkspaceProvider value={legacy}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    await user.click(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    );
    expect(
      screen.getByRole("button", { name: "开始分析全部 2 项" }),
    ).toBeInTheDocument();

    const scoped = workspaceValue();
    scoped.aiSetup = legacy.aiSetup;
    view.rerender(
      <WorkspaceProvider value={scoped}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "开始分析全部 2 项" }),
      ).not.toBeInTheDocument(),
    );
    expect(startLegacy).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "分析当前项" }),
    ).toBeInTheDocument();
  });

  it("revokes a scoped confirmation if capability becomes unknown before start", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    installAnalysisScopePreview(matrix);
    const startAi = vi.spyOn(api, "startAi");
    const scoped = workspaceValue();
    scoped.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    const view = render(
      <WorkspaceProvider value={scoped}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    await user.click(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    );
    expect(
      await screen.findByRole("button", { name: "开始分析 2 项" }),
    ).toBeInTheDocument();

    const unknown = workspaceValue();
    unknown.aiStatus = null;
    unknown.aiSetup = scoped.aiSetup;
    view.rerender(
      <WorkspaceProvider value={unknown}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "开始分析 2 项" }),
      ).not.toBeInTheDocument(),
    );
    expect(startAi).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "分析能力尚未确认" }),
    ).toBeDisabled();
  });

  it("closes preview and reloads the matrix when its tree identity changed", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    const matrixRequest = vi.mocked(api.requirementMatrix);
    vi.spyOn(api, "previewAiScope").mockRejectedValue(
      new ApiError("结构树已变化", 409, {
        detail: { code: "analysis_tree_changed" },
      }),
    );
    const value = workspaceValue();
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    await user.click(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    );

    await waitFor(() => expect(matrixRequest).toHaveBeenCalledTimes(2));
    expect(
      screen.queryByRole("heading", { name: /确认分析范围/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重新核对范围" }),
    ).not.toBeInTheDocument();
  });

  it("retries an uncertain start with the same command and freezes dialog dismissal while pending", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    installAnalysisScopePreview(matrix);
    let rejectFirst!: (reason?: unknown) => void;
    const firstStart = new Promise<Record<string, unknown>>(
      (_resolve, reject) => {
        rejectFirst = reject;
      },
    );
    const startAi = vi
      .spyOn(api, "startAi")
      .mockReturnValueOnce(firstStart)
      .mockResolvedValueOnce({ status: "PENDING" });
    const value = workspaceValue();
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    );
    await user.click(screen.getByRole("button", { name: "开始分析 2 项" }));
    expect(screen.getByRole("button", { name: "正在启动…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "关闭分析确认" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();

    await act(async () => rejectFirst(new Error("response lost")));
    expect(await screen.findByText(/同一操作 ID 重试/)).toBeInTheDocument();
    const firstPayload = startAi.mock.calls[0]?.[2];
    await user.click(screen.getByRole("button", { name: "重试启动 2 项" }));
    await waitFor(() => expect(startAi).toHaveBeenCalledTimes(2));
    expect(startAi.mock.calls[1]?.[2]).toEqual(firstPayload);
  });

  it("leaves confirmation and follows the active job on an already-running conflict", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    installAnalysisScopePreview(matrix);
    vi.spyOn(api, "startAi").mockRejectedValue(
      new ApiError("已有任务运行", 409, {
        detail: { code: "analysis_job_already_running" },
      }),
    );
    const value = workspaceValue();
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    value.refreshAi = vi.fn(async () => {});
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    await user.click(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    );
    await user.click(screen.getByRole("button", { name: "开始分析 2 项" }));

    await waitFor(() => expect(value.refreshAi).toHaveBeenCalledTimes(1));
    expect(
      screen.queryByRole("heading", { name: /全部分析项/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重新核对范围" }),
    ).not.toBeInTheDocument();
  });

  it("closes stale confirmation and refreshes the document on set-lineage conflict", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    installAnalysisScopePreview(matrix);
    vi.spyOn(api, "startAi").mockRejectedValue(
      new ApiError("活动清单已变化", 409, {
        detail: { code: "requirement_set_selection_changed" },
      }),
    );
    const value = workspaceValue();
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    value.refreshDocument = vi.fn(async () => {});
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    await user.click(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    );
    await user.click(screen.getByRole("button", { name: "开始分析 2 项" }));

    await waitFor(() => expect(value.refreshDocument).toHaveBeenCalledTimes(1));
    expect(
      screen.queryByRole("heading", { name: /全部分析项/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重新核对范围" }),
    ).not.toBeInTheDocument();
  });

  it("closes start confirmation and reloads the matrix when scope membership changed", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    const matrixRequest = vi.mocked(api.requirementMatrix);
    installAnalysisScopePreview(matrix);
    vi.spyOn(api, "startAi").mockRejectedValue(
      new ApiError("分析范围已变化", 409, {
        detail: { code: "analysis_scope_changed" },
      }),
    );
    const value = workspaceValue();
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    await user.click(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    );
    await user.click(screen.getByRole("button", { name: "开始分析 2 项" }));

    await waitFor(() => expect(matrixRequest).toHaveBeenCalledTimes(2));
    expect(
      screen.queryByRole("heading", { name: /全部分析项/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重新核对范围" }),
    ).not.toBeInTheDocument();
  });

  it("does not offer same-command retry for a deterministic client error", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    installAnalysisScopePreview(matrix);
    vi.spyOn(api, "startAi").mockRejectedValue(
      new ApiError("当前分析方案不可运行", 422, {
        detail: { code: "analysis_profile_not_ready" },
      }),
    );
    const value = workspaceValue();
    value.runAction = async (_label, action) => {
      try {
        return await action();
      } catch {
        return null;
      }
    };
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    await user.click(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    );
    await user.click(screen.getByRole("button", { name: "开始分析 2 项" }));

    expect(await screen.findByText("当前分析方案不可运行")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /重试启动/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "开始分析 2 项" }),
    ).toBeDisabled();
  });

  it("previews and starts the exact current analysis leaf without treating its branch as scope", async () => {
    const { matrix, root, atomic1 } = fixture();
    const scopedMatrix = {
      ...matrix,
      requirement_set_id: "current-set",
    };
    installReviewMatrixApis(scopedMatrix, root);
    const previewAi = installAnalysisScopePreview(scopedMatrix);
    const startAi = vi
      .spyOn(api, "startAi")
      .mockResolvedValue({ status: "PENDING" });
    const value = workspaceValue("current-set");
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    const currentButton = await screen.findByRole("button", {
      name: "分析当前项",
    });
    expect(currentButton).toBeDisabled();
    await user.click(screen.getByRole("row", { name: /4\.1\.1，高温验证/ }));
    expect(currentButton).toBeEnabled();
    await user.click(currentButton);

    expect(
      await screen.findByRole("heading", { name: "当前项 · 1 项" }),
    ).toBeInTheDocument();
    expect(startAi).not.toHaveBeenCalled();
    expect(previewAi).toHaveBeenCalledWith(
      "project",
      "document",
      expect.objectContaining({
        scope: "CURRENT",
        node_ids: [atomic1.node_id],
      }),
    );
    await user.click(screen.getByRole("button", { name: "开始分析 1 项" }));
    await waitFor(() =>
      expect(startAi).toHaveBeenCalledWith(
        "project",
        "document",
        expect.objectContaining({
          scope: "CURRENT",
          node_ids: [atomic1.node_id],
          expected_scope_sha256: "c".repeat(64),
        }),
      ),
    );
  });

  it("keeps selected analysis leaves independent from row focus and sends canonical source order", async () => {
    const { matrix, root, atomic1, atomic2 } = fixture();
    const scopedMatrix = {
      ...matrix,
      requirement_set_id: "selected-set",
    };
    installReviewMatrixApis(scopedMatrix, root);
    const previewAi = installAnalysisScopePreview(scopedMatrix);
    const value = workspaceValue("selected-set");
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("checkbox", { name: "选择分析 REQ-0042" }),
    );
    await user.click(
      screen.getByRole("checkbox", { name: "选择分析 REQ-0041" }),
    );
    expect(screen.getByRole("button", { name: "分析当前项" })).toBeDisabled();
    expect(screen.getByText("已选 2 项")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "待处理" }));
    expect(
      screen.queryByRole("checkbox", { name: "选择分析 REQ-0041" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("已选 2 项")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "分析所选 2 项" }));

    expect(
      await screen.findByRole("heading", { name: "所选项 · 2 项" }),
    ).toBeInTheDocument();
    expect(previewAi).toHaveBeenCalledWith(
      "project",
      "document",
      expect.objectContaining({
        scope: "SELECTED",
        node_ids: [atomic1.node_id, atomic2.node_id],
      }),
    );
  });

  it("shows controlled parallel v2 progress and overlays each row without changing source order", async () => {
    const { matrix, root, atomic1, atomic2 } = fixture();
    installReviewMatrixApis(matrix, root);
    const value = recoverableWorkspace({
      id: "recoverable-job",
      status: "RUNNING",
      requirement_set_id: "set",
      scope: "ALL",
      publication_mode: "PER_NODE_RECOVERABLE",
      current_stage: "assessment",
      execution_counts: {
        total: 2,
        waiting: 0,
        running: 2,
        publishing: 0,
        succeeded: 0,
        failed: 0,
        cancelled: 0,
        retryable_failed: 0,
        settled: 0,
      },
      node_executions: [
        nodeExecution(atomic1, "RUNNING"),
        nodeExecution(atomic2, "RUNNING"),
      ],
    });

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    const progress = await screen.findByRole("region", {
      name: "需求分析进度",
    });
    expect(within(progress).getByText("已处理 0")).toBeInTheDocument();
    expect(within(progress).getByText("成功 0")).toBeInTheDocument();
    expect(within(progress).getByText("失败 0")).toBeInTheDocument();
    expect(within(progress).getByText("运行中 2")).toBeInTheDocument();
    expect(within(progress).getByText("等待 0")).toBeInTheDocument();
    expect(within(progress).getByText("已发布 0")).toBeInTheDocument();
    expect(
      within(progress).getByRole("progressbar", {
        name: "已处理 0 项，共 2 项",
      }),
    ).toHaveValue(0);

    const firstRow = screen.getByRole("row", { name: /4\.1\.1，高温验证/ });
    const secondRow = screen.getByRole("row", { name: /4\.1\.2，低温验证/ });
    expect(within(firstRow).getByText("分析中")).toBeInTheDocument();
    expect(within(secondRow).getByText("分析中")).toBeInTheDocument();
    const renderedRows = screen.getAllByRole("row");
    expect(renderedRows.indexOf(firstRow)).toBeLessThan(
      renderedRows.indexOf(secondRow),
    );
  });

  it("previews failed-only v2 scope and starts it only after confirmation", async () => {
    const { matrix, root, atomic1, atomic2 } = fixture();
    installReviewMatrixApis(matrix, root);
    const previewAi = vi
      .spyOn(api, "previewAiScope")
      .mockImplementation(async (_projectId, _documentId, request) => {
        if (request.scope !== "FAILED_ONLY") {
          throw new Error("expected failed-only request");
        }
        return {
          schema_version: "requirement-analysis-scope-preview/v2",
          command_id: request.command_id,
          requirement_set_id: request.requirement_set_id,
          requirement_set_version: 1,
          tree_sha256: request.expected_tree_sha256,
          scope: "FAILED_ONLY",
          scope_sha256: "d".repeat(64),
          requested_node_ids: [],
          target_node_ids: [atomic2.node_id],
          target_requirement_ids: [String(atomic2.requirement_id)],
          requested_count: 1,
          target_count: 1,
          skipped_count: 0,
          skipped_reason_counts: {},
          target_examples: [
            {
              node_id: atomic2.node_id,
              requirement_id: String(atomic2.requirement_id),
              requirement_code: atomic2.requirement_code,
              label: atomic2.label,
            },
          ],
          skipped_examples: [],
          publication_mode: "PER_NODE_RECOVERABLE",
          source_job_id: request.source_job_id,
          failed_snapshot_sha256: "f".repeat(64),
          source_failed_count: 2,
          already_current_count: 0,
          retry_target_count: 1,
          retry_already_current_count: 0,
          retry_skipped_count: 1,
        };
      });
    const startAi = vi
      .spyOn(api, "startAi")
      .mockResolvedValue({ status: "PENDING" });
    const value = recoverableWorkspace({
      id: "recoverable-job",
      status: "COMPLETED",
      requirement_set_id: "set",
      scope: "ALL",
      publication_mode: "PER_NODE_RECOVERABLE",
      current_stage: "completed",
      execution_counts: {
        total: 2,
        waiting: 0,
        running: 0,
        publishing: 0,
        succeeded: 1,
        failed: 1,
        cancelled: 0,
        retryable_failed: 1,
        settled: 2,
      },
      node_executions: [
        nodeExecution(atomic1, "SUCCEEDED"),
        nodeExecution(atomic2, "FAILED"),
      ],
    });
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", label: "本地智能分析", ready: true }],
    };
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: "重试失败 1 项" }),
    );
    expect(startAi).not.toHaveBeenCalled();
    expect(
      await screen.findByRole("heading", { name: "仅重试失败项 · 1 项" }),
    ).toBeInTheDocument();
    expect(screen.getByText("只重试仍失败的项目")).toBeInTheDocument();
    expect(previewAi).toHaveBeenCalledWith(
      "project",
      "document",
      expect.objectContaining({
        schema_version: "requirement-analysis-scope-request/v2",
        scope: "FAILED_ONLY",
        source_job_id: "recoverable-job",
        node_ids: [],
      }),
    );
    const dialog = screen.getByRole("dialog", { name: "确认分析范围" });
    expect(within(dialog).getByText("REQ-0042")).toBeInTheDocument();
    expect(within(dialog).queryByText("REQ-0041")).not.toBeInTheDocument();
    expect(within(dialog).getByText("已不再适用")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "开始重试 1 项" }));
    await waitFor(() =>
      expect(startAi).toHaveBeenCalledWith(
        "project",
        "document",
        expect.objectContaining({
          schema_version: "requirement-analysis-scope-request/v2",
          scope: "FAILED_ONLY",
          source_job_id: "recoverable-job",
          node_ids: [],
          expected_scope_sha256: "d".repeat(64),
        }),
      ),
    );
  });

  it("refreshes published v2 results while parallel work continues", async () => {
    const { matrix, root, atomic1, atomic2 } = fixture();
    const matrixRequest = vi
      .spyOn(api, "requirementMatrix")
      .mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const initial = recoverableWorkspace({
      id: "recoverable-job",
      status: "RUNNING",
      requirement_set_id: "set",
      scope: "ALL",
      publication_mode: "PER_NODE_RECOVERABLE",
      execution_counts: {
        total: 2,
        waiting: 1,
        running: 1,
        publishing: 0,
        succeeded: 0,
        failed: 0,
        cancelled: 0,
        retryable_failed: 0,
        settled: 0,
      },
      node_executions: [
        nodeExecution(atomic1, "RUNNING"),
        nodeExecution(atomic2, "WAITING"),
      ],
    });
    const view = render(
      <WorkspaceProvider value={initial}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    await waitFor(() => expect(matrixRequest).toHaveBeenCalledTimes(1));

    const published = recoverableWorkspace({
      id: "recoverable-job",
      status: "RUNNING",
      requirement_set_id: "set",
      scope: "ALL",
      publication_mode: "PER_NODE_RECOVERABLE",
      execution_counts: {
        total: 2,
        waiting: 0,
        running: 1,
        publishing: 0,
        succeeded: 1,
        failed: 0,
        cancelled: 0,
        retryable_failed: 0,
        settled: 1,
      },
      node_executions: [
        nodeExecution(atomic1, "SUCCEEDED"),
        nodeExecution(atomic2, "RUNNING"),
      ],
    });
    view.rerender(
      <WorkspaceProvider value={published}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await waitFor(() => expect(matrixRequest).toHaveBeenCalledTimes(2));
    expect(
      within(screen.getByRole("region", { name: "需求分析进度" })).getByText(
        "已发布 1",
      ),
    ).toBeInTheDocument();
  });

  it("fails closed on malformed v2 execution state instead of falling back to legacy start", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    const value = recoverableWorkspace({
      id: "recoverable-job",
      status: "COMPLETED",
      requirement_set_id: "set",
      publication_mode: "PER_NODE_RECOVERABLE",
      execution_counts: { total: 2 },
      node_executions: [],
    });
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", ready: true }],
    };

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "分析状态需要刷新" }),
    ).toBeDisabled();
    expect(screen.getByText("逐项分析状态暂时不完整")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "分析全部 2 项" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /重试失败/ }),
    ).not.toBeInTheDocument();
  });

  it("keeps a migrated terminal atomic job readable after v2 capability is enabled", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    const value = recoverableWorkspace({
      id: "atomic-history-job",
      status: "COMPLETED",
      requirement_set_id: "set",
      scope: "ALL",
      publication_mode: "ATOMIC_BATCH",
      total_requirements: 2,
      computed_requirements: 2,
      published_requirements: 2,
      current_stage: "completed",
    });
    value.aiSetup = {
      default_profile_id: "ready-profile",
      profiles: [{ id: "ready-profile", ready: true }],
    };

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "分析状态需要刷新" }),
    ).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "需求分析进度" })).getByText(
        "本次已发布 2/2",
      ),
    ).toBeInTheDocument();
  });

  it("shows the existing server-owned AI progress without publishing partial rows", async () => {
    const { matrix, root } = fixture();
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const value = workspaceValue();
    value.aiStatus = {
      job: {
        id: "ai-job",
        status: "RUNNING",
        requirement_set_id: "set",
        total_requirements: 2,
        completed_requirements: 1,
        published_requirements: 0,
        scope: "SELECTED",
        current_requirement_code: "REQ-0042",
        current_stage: "screening",
        last_message: "正在筛选 REQ-0042（2/2）",
      },
    };

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    const progress = await screen.findByRole("region", {
      name: "需求分析进度",
    });
    expect(within(progress).getByText("正在筛选")).toBeInTheDocument();
    expect(
      within(progress).getByText("所选项 · 本次已计算 1/2"),
    ).toBeInTheDocument();
    expect(within(progress).getByText("等待 0")).toBeInTheDocument();
    expect(within(progress).getByText("运行中 1")).toBeInTheDocument();
    expect(within(progress).getByText("本次已计算 1")).toBeInTheDocument();
    expect(within(progress).getByText("本次已发布 0")).toBeInTheDocument();
    expect(within(progress).getByText("当前 REQ-0042")).toBeInTheDocument();
    expect(within(progress).getByText("本次已发布 0/2")).toBeInTheDocument();
    expect(
      within(progress).getByRole("progressbar", {
        name: "本次已计算 1 项，共 2 项",
      }),
    ).toHaveValue(1);
  });

  it("freezes stopping during atomic publication and reports published progress separately", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    const value = workspaceValue();
    value.aiStatus = {
      job: {
        id: "publishing-job",
        status: "PUBLISHING",
        requirement_set_id: "set",
        scope: "ALL",
        total_requirements: 2,
        computed_requirements: 2,
        published_requirements: 0,
        current_stage: "publishing",
        can_cancel: false,
      },
    };

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "正在发布" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "停止分析" }),
    ).not.toBeInTheDocument();
    const progress = screen.getByRole("region", { name: "需求分析进度" });
    expect(within(progress).getByText("本次已发布 0/2")).toBeInTheDocument();
    expect(within(progress).getByText("本次已计算 2")).toBeInTheDocument();
  });

  it("labels a submitted cancellation as stopping instead of publishing", async () => {
    const { matrix, root } = fixture();
    installReviewMatrixApis(matrix, root);
    const value = workspaceValue();
    value.aiStatus = {
      job: {
        id: "cancelling-job",
        status: "CANCEL_REQUESTED",
        requirement_set_id: "set",
        scope: "ALL",
        total_requirements: 2,
        computed_requirements: 1,
        published_requirements: 0,
        current_stage: "cancelling",
        can_cancel: false,
      },
    };

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "正在停止" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "正在发布" }),
    ).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "需求分析进度" })).getByText(
        "正在停止",
      ),
    ).toBeInTheDocument();
  });

  it("does not attach another requirement set's AI job to the current version", async () => {
    const { matrix, root } = fixture();
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const value = workspaceValue();
    value.aiStatus = {
      job: {
        id: "other-set-job",
        status: "RUNNING",
        requirement_set_id: "other-set",
        total_requirements: 100,
        completed_requirements: 12,
      },
    };

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    expect(
      await screen.findByRole("button", { name: "分析全部 2 项" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "停止分析" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "需求分析进度" }),
    ).not.toBeInTheDocument();
  });

  it("can stop a v2 job during per-node publication and keeps published successes", async () => {
    const { matrix, root, atomic1, atomic2 } = fixture();
    installReviewMatrixApis(matrix, root);
    const stopAi = vi.spyOn(api, "stopAi").mockResolvedValue({
      status: "CANCEL_REQUESTED",
    });
    const value = recoverableWorkspace({
      id: "recoverable-job",
      status: "RUNNING",
      requirement_set_id: "set",
      scope: "ALL",
      publication_mode: "PER_NODE_RECOVERABLE",
      current_stage: "assessment",
      can_cancel: true,
      execution_counts: {
        total: 2,
        waiting: 0,
        running: 1,
        publishing: 1,
        succeeded: 0,
        failed: 0,
        cancelled: 0,
        retryable_failed: 0,
        settled: 0,
      },
      node_executions: [
        nodeExecution(atomic1, "PUBLISHING"),
        nodeExecution(atomic2, "RUNNING"),
      ],
    });
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "停止分析" }));
    expect(
      screen.getByRole("heading", { name: "停止本次分析？" }),
    ).toBeInTheDocument();
    expect(screen.getByText("已发布成功项会保留")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认停止分析" }));
    await waitFor(() =>
      expect(stopAi).toHaveBeenCalledWith(
        "project",
        "document",
        "recoverable-job",
      ),
    );
  });

  it("confirms before stopping an atomic batch and labels terminal remainder honestly", async () => {
    const { matrix, root } = fixture();
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockResolvedValue({
      schema_version: "requirement-matrix-node-detail/v1",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "set",
      node: root,
      path: [root],
      tree_node: {},
      requirement: null,
      assessment: null,
      logical_tables: [],
    });
    const stopAi = vi
      .spyOn(api, "stopAi")
      .mockResolvedValue({ status: "CANCEL_REQUESTED" });
    const value = workspaceValue();
    value.aiStatus = {
      job: {
        id: "current-job",
        status: "RUNNING",
        requirement_set_id: "set",
        total_requirements: 2,
        completed_requirements: 1,
      },
    };
    const user = userEvent.setup();

    const view = render(
      <WorkspaceProvider value={value}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "停止分析" }));
    expect(stopAi).not.toHaveBeenCalled();
    expect(
      screen.getByRole("heading", { name: "停止本次整批分析？" }),
    ).toBeInTheDocument();
    expect(screen.getByText("部分计算结果不会发布")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认停止分析" }));
    await waitFor(() =>
      expect(stopAi).toHaveBeenCalledWith("project", "document", "current-job"),
    );

    const stopped = workspaceValue();
    stopped.aiStatus = {
      job: {
        id: "current-job",
        status: "CANCELLED",
        requirement_set_id: "set",
        total_requirements: 2,
        completed_requirements: 1,
      },
    };
    view.rerender(
      <WorkspaceProvider value={stopped}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );
    const progress = await screen.findByRole("region", {
      name: "需求分析进度",
    });
    expect(within(progress).getByText("未完成 1")).toBeInTheDocument();
    expect(within(progress).queryByText("等待 1")).not.toBeInTheDocument();
  });

  it("keeps the initial matrix lightweight and fetches a logical table only after selection", async () => {
    const root = node("table-root", {
      child_ids: ["whole-table-5-1"],
      label: "5 参数表",
    });
    const tableNode = node("whole-table-5-1", {
      parent_id: "table-root",
      depth: 1,
      source_order: 1,
      display_role: "TABLE",
      label: "表 5-1 性能参数",
      page_start: 15,
      page_end: 16,
      structure_node_id: "logical-5-1",
      logical_table_ids: ["logical-5-1"],
    });
    const matrix: RequirementMatrixSnapshot = {
      schema_version: "requirement-matrix-snapshot/v1",
      state: "READY",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "table-set",
      requirement_set_status: "LOCKED",
      tree_sha256: "c".repeat(64),
      root_node_ids: [root.node_id],
      nodes: [root, tableNode],
      logical_tables: [
        {
          logical_table_id: "logical-5-1",
          projection_id: "projection-5-1",
          projection_mode: "RECORDS",
          record_count: 500,
          physical_table_ids: ["physical-5-1"],
          node_ids: [tableNode.node_id],
          evidence_binding_ids: [],
        },
      ],
      counts: {
        total_nodes: 2,
        visible_nodes: 2,
        system_non_business_nodes: 0,
        human_excluded_nodes: 0,
        analyze_nodes: 0,
        retained_nodes: 2,
        excluded_nodes: 0,
        analysis_eligible_nodes: 0,
        assessed_nodes: 0,
        decided_nodes: 0,
        logical_tables: 1,
      },
    };
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    const geometryFocus = vi
      .spyOn(api, "geometryFocus")
      .mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockImplementation(
      async (_projectId, _documentId, _setId, nodeId) => ({
        schema_version: "requirement-matrix-node-detail/v1",
        project_id: "project",
        document_id: "document",
        requirement_set_id: "table-set",
        node: nodeId === root.node_id ? root : tableNode,
        path: nodeId === root.node_id ? [root] : [root, tableNode],
        tree_node: {},
        requirement: null,
        assessment: null,
        logical_tables: [],
      }),
    );
    const tableDetail = vi
      .spyOn(api, "requirementMatrixLogicalTable")
      .mockResolvedValue({
        schema_version: "requirement-matrix-logical-table-detail/v1",
        project_id: "project",
        document_id: "document",
        requirement_set_id: "table-set",
        logical_table: {
          ...matrix.logical_tables[0],
          projection: { records: [], tables: [] },
          evidence_bindings: [],
          structured_segments: [
            {
              id: "physical-5-1-a",
              kind: "table",
              page_start: 15,
              page_end: 15,
              location: {
                page_number: 15,
                bbox: [71.9, 384.4, 546.0, 763.4],
              },
            },
            {
              id: "physical-5-1-b",
              kind: "table",
              page_start: 16,
              page_end: 16,
              location: {
                page_number: 16,
                bbox: [71.4, 76.7, 546.0, 346.0],
              },
            },
          ],
        },
        nodes: [tableNode],
      });
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspaceValue("table-set")}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    const row = await screen.findByRole("treeitem", {
      name: /表 5-1 性能参数/,
    });
    expect(tableDetail).not.toHaveBeenCalled();
    await user.click(row);
    await waitFor(() =>
      expect(tableDetail).toHaveBeenCalledWith(
        "project",
        "document",
        "table-set",
        "logical-5-1",
      ),
    );
    expect(geometryFocus).not.toHaveBeenCalledWith(
      "project",
      "document",
      "logical-5-1",
      "table-set",
    );
    expect(geometryFocus).not.toHaveBeenCalledWith(
      "project",
      "document",
      tableNode.node_id,
      "table-set",
    );
    expect(await screen.findByText("原 PDF · 第 15 页")).toBeInTheDocument();
    await act(async () => undefined);
    const page15Frame = screen.getByTitle(
      "带当前内容框选的原 PDF",
    ) as HTMLIFrameElement;
    const page15PostMessage = vi.spyOn(
      page15Frame.contentWindow!,
      "postMessage",
    );
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "pdf-viewer-ready" },
        origin: window.location.origin,
        source: page15Frame.contentWindow,
      }),
    );
    expect(page15PostMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "REQPILOTX_PDF_NAVIGATE",
        page: 15,
        overlays: expect.arrayContaining([
          expect.objectContaining({
            id: "matrix-table-segment-physical-5-1-a",
            type: "TABLE",
            bbox: [71.9, 384.4, 546.0, 763.4],
            precision: "TABLE_SEGMENT_EXACT",
          }),
        ]),
      }),
      window.location.origin,
    );

    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("原 PDF · 第 16 页")).toBeInTheDocument();
    const page16Frame = screen.getByTitle(
      "带当前内容框选的原 PDF",
    ) as HTMLIFrameElement;
    const page16PostMessage = vi.spyOn(
      page16Frame.contentWindow!,
      "postMessage",
    );
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "pdf-viewer-ready" },
        origin: window.location.origin,
        source: page16Frame.contentWindow,
      }),
    );
    expect(page16PostMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "REQPILOTX_PDF_NAVIGATE",
        page: 16,
        overlays: expect.arrayContaining([
          expect.objectContaining({
            id: "matrix-table-segment-physical-5-1-b",
            type: "TABLE",
            bbox: [71.4, 76.7, 546.0, 346.0],
            precision: "TABLE_SEGMENT_EXACT",
          }),
        ]),
      }),
      window.location.origin,
    );
  });

  it("keeps a loading record private, then opens its exact continuation page and row", async () => {
    const headerTable = {
      id: "physical-5-2-a",
      table_id: "physical-5-2-a",
      kind: "table",
      page_start: 15,
      page_end: 15,
      location: { page_number: 15, bbox: [71, 380, 546, 760] },
      row_count: 1,
      column_count: 2,
      cells: [
        {
          id: "header-item",
          row: 0,
          column: 0,
          text: "项目",
          header: true,
          location: { page_number: 15, bbox: [71, 380, 200, 410] },
        },
        {
          id: "header-value",
          row: 0,
          column: 1,
          text: "要求",
          header: true,
          location: { page_number: 15, bbox: [200, 380, 546, 410] },
        },
      ],
      continuation: {
        logical_table_id: "logical-5-2",
        segment_index: 0,
        segment_count: 2,
      },
    };
    const recordTable = {
      id: "physical-5-2-b",
      table_id: "physical-5-2-b",
      kind: "table",
      page_start: 16,
      page_end: 16,
      location: { page_number: 16, bbox: [71, 76, 546, 346] },
      row_count: 3,
      column_count: 2,
      cells: [
        { id: "prior-item", row: 0, column: 0, text: "续表上一条" },
        { id: "prior-value", row: 0, column: 1, text: "60°C" },
        {
          id: "current-item",
          row: 1,
          column: 0,
          text: "当前记录",
          location: { page_number: 16, bbox: [71, 120, 200, 168] },
        },
        {
          id: "current-value",
          row: 1,
          column: 1,
          text: "85°C",
          location: { page_number: 16, bbox: [200, 120, 546, 168] },
        },
        { id: "other-item", row: 2, column: 0, text: "不应闪现的其他记录" },
        { id: "other-value", row: 2, column: 1, text: "40°C" },
      ],
      continuation: {
        logical_table_id: "logical-5-2",
        segment_index: 1,
        segment_count: 2,
      },
    };
    const root = node("record-root", {
      child_ids: ["record-node"],
      label: "5 参数表",
    });
    const recordNode = node("record-node", {
      parent_id: root.node_id,
      depth: 1,
      source_order: 1,
      display_role: "TABLE_RECORD",
      label: "当前记录",
      source_kind: "TABLE_RECORD",
      source_number: "10",
      display_title: "当前记录",
      table_record_id: "record-current",
      logical_table_ids: ["logical-5-2"],
      table_refs: [headerTable, recordTable],
      page_start: 15,
      page_end: 16,
    });
    const logicalTable = {
      logical_table_id: "logical-5-2",
      projection_id: "projection-5-2",
      projection_mode: "RECORDS",
      record_count: 2,
      physical_table_ids: ["physical-5-2-a", "physical-5-2-b"],
      node_ids: [recordNode.node_id],
      evidence_binding_ids: [],
    };
    const matrix: RequirementMatrixSnapshot = {
      schema_version: "requirement-matrix-snapshot/v1",
      state: "READY",
      project_id: "project",
      document_id: "document",
      requirement_set_id: "record-set",
      requirement_set_status: "LOCKED",
      tree_sha256: "d".repeat(64),
      root_node_ids: [root.node_id],
      nodes: [root, recordNode],
      logical_tables: [logicalTable],
      counts: {
        total_nodes: 2,
        visible_nodes: 2,
        system_non_business_nodes: 0,
        human_excluded_nodes: 0,
        analyze_nodes: 0,
        retained_nodes: 2,
        excluded_nodes: 0,
        analysis_eligible_nodes: 0,
        assessed_nodes: 0,
        decided_nodes: 0,
        logical_tables: 1,
      },
    };
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({
      regions: [
        { id: "broad-a", page_number: 15, bbox: [71, 380, 546, 760] },
        { id: "broad-b", page_number: 16, bbox: [71, 76, 546, 346] },
      ],
    });
    vi.spyOn(api, "requirementMatrixNode").mockImplementation(
      async (_projectId, _documentId, _setId, nodeId) => ({
        schema_version: "requirement-matrix-node-detail/v1",
        project_id: "project",
        document_id: "document",
        requirement_set_id: "record-set",
        node: nodeId === root.node_id ? root : recordNode,
        path: nodeId === root.node_id ? [root] : [root, recordNode],
        tree_node: {},
        requirement: null,
        assessment: null,
        logical_tables: [],
      }),
    );
    let resolveTableDetail!: (
      value: Awaited<ReturnType<typeof api.requirementMatrixLogicalTable>>,
    ) => void;
    vi.spyOn(api, "requirementMatrixLogicalTable").mockReturnValue(
      new Promise((resolve) => {
        resolveTableDetail = resolve;
      }),
    );
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspaceValue("record-set")}>
        <RequirementMatrixWorkbench lens="review" />
      </WorkspaceProvider>,
    );

    await user.click(await screen.findByRole("treeitem", { name: /当前记录/ }));
    expect(
      await screen.findByText("正在还原当前表格条目…"),
    ).toBeInTheDocument();
    expect(screen.queryByText("不应闪现的其他记录")).not.toBeInTheDocument();

    await act(async () => {
      resolveTableDetail({
        schema_version: "requirement-matrix-logical-table-detail/v1",
        project_id: "project",
        document_id: "document",
        requirement_set_id: "record-set",
        logical_table: {
          ...logicalTable,
          projection: {
            tables: [headerTable, recordTable],
            records: [
              {
                record_id: "record-current",
                row_bindings: [
                  {
                    table_id: "physical-5-2-b",
                    row_index: 1,
                    cell_ids: ["current-item", "current-value"],
                  },
                ],
                column_bindings: [
                  {
                    table_id: "physical-5-2-b",
                    column_index: 0,
                    cell_ids: ["current-item"],
                  },
                  {
                    table_id: "physical-5-2-b",
                    column_index: 1,
                    cell_ids: ["current-value"],
                  },
                ],
                header_bindings: [
                  {
                    table_id: "physical-5-2-a",
                    cell_ids: ["header-item", "header-value"],
                    axis: "ROW",
                    text: "项目 / 要求",
                  },
                ],
              },
            ],
          },
          evidence_bindings: [],
          structured_segments: [headerTable, recordTable],
        },
        nodes: [recordNode],
      });
    });

    expect(
      await screen.findByRole("button", { name: "查看完整原表" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "85°C" })).toBeInTheDocument();
    expect(screen.queryByText("不应闪现的其他记录")).not.toBeInTheDocument();
    expect(await screen.findByText("原 PDF · 第 16 页")).toBeInTheDocument();
    const recordFrame = screen.getByTitle(
      "带当前内容框选的原 PDF",
    ) as HTMLIFrameElement;
    const recordPostMessage = vi.spyOn(
      recordFrame.contentWindow!,
      "postMessage",
    );
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "pdf-viewer-ready" },
        origin: window.location.origin,
        source: recordFrame.contentWindow,
      }),
    );
    expect(recordPostMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "REQPILOTX_PDF_NAVIGATE",
        page: 16,
        overlays: [
          expect.objectContaining({
            id: "matrix-table-record-record-current",
            type: "TABLE",
            bbox: [71, 120, 546, 168],
            precision: "TABLE_RECORD_ROW_EXACT",
          }),
        ],
      }),
      window.location.origin,
    );
  });

  it("keeps parent context, retries detail, protects failed edits and saves from the inspector shortcut", async () => {
    const { matrix, root, clause, atomic1, atomic2 } = fixture();
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({
      source_identity: { source_sha256: "b".repeat(64) },
      page_comparison: {
        pages: [
          {
            page_number: 1,
            bounds: [0, 0, 595, 842],
            blocks: [],
            tables: [],
            issues: [],
          },
        ],
      },
    });
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    let atomicDetailAttempts = 0;
    vi.spyOn(api, "requirementMatrixNode").mockImplementation(
      async (_projectId, _documentId, _setId, nodeId) => {
        if (nodeId === atomic1.node_id && atomicDetailAttempts++ === 0) {
          throw new Error("版本详情暂不可用");
        }
        const current = matrix.nodes.find(
          (candidate) => candidate.node_id === nodeId,
        )!;
        const path =
          nodeId === root.node_id
            ? [root]
            : nodeId === clause.node_id
              ? [root, clause]
              : [root, clause, current];
        return {
          schema_version: "requirement-matrix-node-detail/v1",
          project_id: "project",
          document_id: "document",
          requirement_set_id: "set",
          node: current,
          path,
          tree_node: {},
          requirement: null,
          assessment: null,
          logical_tables: [],
        };
      },
    );
    const decisionRequest = vi
      .spyOn(api, "updateRequirementMatrixDecision")
      .mockRejectedValueOnce(new Error("保存失败"))
      .mockResolvedValue({
        node: {
          ...atomic1,
          analysis_status: "DECIDED",
          save_revision: 1,
          manual_decision: {
            decision: "ACCEPT",
            reason: "已核对",
            decided_at: "2026-07-31T00:00:00Z",
          },
        },
      });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspaceValue()}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    const firstRow = await screen.findByRole("row", {
      name: /4\.1\.1，高温验证/,
    });
    expect(
      screen.getByText(/智能分析未配置，可继续人工处理与导出/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "分析全部 2 项" }),
    ).toBeDisabled();
    await user.click(screen.getByText("更多筛选"));
    expect(screen.getByRole("button", { name: "已排除" })).toBeInTheDocument();
    await user.click(firstRow);
    expect(
      (await screen.findAllByText("版本详情暂不可用")).length,
    ).toBeGreaterThan(0);
    await user.click(screen.getAllByRole("button", { name: "重试" })[0]);
    expect(
      await screen.findByText("父条款完整正文：控制器应满足全部热性能要求。", {
        selector: ".matrix-primary-copy p",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "当前选中内容边界" }),
    ).toHaveTextContent("控制器应通过 85°C 高温验证。");

    const decision = screen.getByLabelText("结论");
    await user.selectOptions(decision, "ACCEPT");
    const reason = screen.getByLabelText("工程师说明");
    await user.type(reason, "已核对");
    fireEvent.keyDown(reason, { key: "Enter", metaKey: true });
    expect(await screen.findByText("保存失败，请重试")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /重试保存/ }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("row", { name: /4\.1\.2，低温验证/ }));
    expect(confirm).toHaveBeenCalled();
    expect(firstRow).toHaveAttribute("aria-selected", "true");
    expect(reason).toHaveValue("已核对");

    fireEvent.keyDown(reason, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(decisionRequest).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(
        screen.getByRole("row", { name: /4\.1\.2，低温验证/ }),
      ).toHaveAttribute("aria-selected", "true"),
    );

    const selectedSecondRow = screen.getByRole("row", {
      name: /4\.1\.2，低温验证/,
    });
    fireEvent.keyDown(selectedSecondRow, { key: "/" });
    expect(screen.getByRole("searchbox")).toHaveFocus();
  });

  it("saves and advances past a terminal AI-default row to the next pending item", async () => {
    const { matrix: base, root, clause, atomic1, atomic2 } = fixture();
    const aiDefault = node("atomic-ai-default", {
      parent_id: clause.node_id,
      depth: 2,
      source_order: 3,
      display_role: "ATOMIC_REQUIREMENT",
      label: "AI 已结束条目",
      source_kind: "REQUIREMENT",
      source_number: "4.1.2",
      display_title: "AI 已结束条目",
      requirement_code: "REQ-AI",
      requirement_id: "requirement-ai",
      requirement_text: "AI 已结束条目",
      disposition: "ANALYZE",
      analysis_eligibility: "ELIGIBLE",
      analysis_status: "ASSESSED",
      analysis_result_state: "CURRENT",
      ai_assessment: {
        decision: "ACCEPT",
        compliance_status: "COMPLIANT",
        reason: "终态结论",
        confidence: 0.9,
        assessment_mode: "EVIDENCE",
      },
    });
    const pending = {
      ...atomic2,
      source_order: 4,
      source_number: "4.1.3",
      display_title: "下一个待分析条目",
      label: "下一个待分析条目",
    };
    const nextClause = {
      ...clause,
      child_ids: [atomic1.node_id, aiDefault.node_id, pending.node_id],
    };
    const matrix: RequirementMatrixSnapshot = {
      ...base,
      nodes: [root, nextClause, atomic1, aiDefault, pending],
      counts: {
        ...base.counts,
        total_nodes: 5,
        visible_nodes: 5,
        analyze_nodes: 3,
        analysis_eligible_nodes: 3,
        assessed_nodes: 2,
      },
    };
    vi.spyOn(api, "requirementMatrix").mockResolvedValue(matrix);
    vi.spyOn(api, "sourcePage").mockResolvedValue({});
    vi.spyOn(api, "geometryFocus").mockResolvedValue({ regions: [] });
    vi.spyOn(api, "requirementMatrixNode").mockImplementation(
      async (_projectId, _documentId, _setId, nodeId) => {
        const current = matrix.nodes.find(
          (candidate) => candidate.node_id === nodeId,
        )!;
        return {
          schema_version: "requirement-matrix-node-detail/v1",
          project_id: "project",
          document_id: "document",
          requirement_set_id: "set",
          node: current,
          path: [root, nextClause, current],
          tree_node: {},
          requirement: null,
          assessment: null,
          logical_tables: [],
        };
      },
    );
    vi.spyOn(api, "updateRequirementMatrixDecision").mockResolvedValue({
      node: {
        ...atomic1,
        analysis_status: "DECIDED",
        analysis_result_state: "CURRENT",
        manual_decision: {
          decision: "ACCEPT",
          reason: "已核对",
          decided_at: "2026-08-08T00:00:00Z",
        },
      },
    });
    const user = userEvent.setup();

    render(
      <WorkspaceProvider value={workspaceValue()}>
        <RequirementMatrixWorkbench lens="analysis" />
      </WorkspaceProvider>,
    );

    await user.click(
      await screen.findByRole("row", { name: /4\.1\.1，高温验证/ }),
    );
    await user.selectOptions(await screen.findByLabelText("结论"), "ACCEPT");
    await user.type(screen.getByLabelText("工程师说明"), "已核对");
    fireEvent.keyDown(screen.getByLabelText("工程师说明"), {
      key: "Enter",
      ctrlKey: true,
    });

    await waitFor(() =>
      expect(
        screen.getByRole("row", { name: /4\.1\.3，下一个待分析条目/ }),
      ).toHaveAttribute("aria-selected", "true"),
    );
    expect(screen.getByRole("row", { name: /AI 已结束条目/ })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });
});
