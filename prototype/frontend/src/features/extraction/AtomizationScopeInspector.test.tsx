import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
  AtomizationScopeInspector,
  type AtomizationScopeInspectorProps,
} from "./AtomizationScopeInspector";
import type {
  RequirementAtomizationScopeAttempt,
  RequirementAtomizationScopeReceipt,
} from "../../shared/api/contracts";
import type { AtomizationScopeViewModel } from "./atomizationScopeModel";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close() {
    this.open = false;
  };
});

afterEach(cleanup);

function viewModel(
  overrides: Partial<AtomizationScopeViewModel> = {},
): AtomizationScopeViewModel {
  const plan = {
    identity: {
      projectId: "project",
      documentId: "document",
      sourceSha256: "a".repeat(64),
      effectiveStructureId: "effective",
      effectiveTreeSha256: "b".repeat(64),
      workingTreeId: "working",
      workingTreeSha256: "c".repeat(64),
    },
    scope: "BRANCH" as const,
    anchorTreeNodeId: "section",
    selectedWorkflowIds: [],
    items: [],
    requestedTreeNodeIds: ["tech", "info"],
    requestedWorkflowIds: ["workflow-tech", "workflow-info"],
    executableWorkflowIds: ["workflow-tech"],
    estimatedRemoteCallCount: 1,
    targets: [
      {
        treeNodeId: "tech",
        workflowNodeId: "workflow-tech",
        label: "3.1 制动性能",
        pathLabel: "制动系统 › 3.1 制动性能",
        sourceOrder: 1,
        sourceOffset: 0,
        classificationType: "TECHNICAL" as const,
        disposition: "TARGET" as const,
        reason: null,
      },
    ],
    skipped: [
      {
        treeNodeId: "info",
        workflowNodeId: "workflow-info",
        label: "背景说明",
        pathLabel: "制动系统 › 背景说明",
        sourceOrder: 2,
        sourceOffset: 0,
        classificationType: "INFORMATION" as const,
        disposition: "SKIPPED" as const,
        reason: "INFORMATION" as const,
      },
    ],
  };
  return {
    plan: { ...plan, items: [...plan.targets, ...plan.skipped] },
    scope: "BRANCH",
    scopeLabel: "当前分支",
    defaultBehavior: "默认保留原条目，不拆分",
    summary: "1 项技术需求将原子化，1 项保持原条目",
    actionLabel: "原子化 1 项技术需求",
    targetCount: 1,
    skippedCount: 1,
    estimatedRemoteCallCount: 1,
    canApply: true,
    emptyReason: "",
    targets: plan.targets.map((item) => ({
      ...item,
      reasonLabel: "",
    })),
    skipped: plan.skipped.map((item) => ({
      ...item,
      reasonLabel: "信息与参考不参与原子化",
    })),
    ...overrides,
  };
}

function props(
  overrides: Partial<AtomizationScopeInspectorProps> = {},
): AtomizationScopeInspectorProps {
  return {
    viewModel: viewModel(),
    dialogOpen: false,
    busy: false,
    rangeLoading: false,
    canOpenDialog: true,
    actionGuidance: "",
    serverStateLabel: "尚未执行 · 当前保留原条目",
    canUndo: false,
    error: "",
    currentReceipt: null,
    lastAttempt: null,
    onOpenDialog: vi.fn(),
    onCloseDialog: vi.fn(),
    onScopeChange: vi.fn(),
    onApply: vi.fn(),
    onRetry: vi.fn(),
    onUndo: vi.fn(),
    ...overrides,
  };
}

function successfulReceipt(): RequirementAtomizationScopeReceipt {
  return {
    schema_version: "atomization-scope-receipt/v1",
    receipt_id: "atomization-scope-receipt-" + "1".repeat(32),
    payload_sha256: "2".repeat(64),
    scope_revision: 3,
    operation: "APPLY",
    scope: "SELECTED",
    requested_review_node_ids: ["a", "b", "c"],
    executed_review_node_ids: ["a", "b"],
    new_review_node_ids: ["a"],
    reused_review_node_ids: ["b"],
    skipped_targets: [{ review_node_id: "c", reason: "INFORMATION" }],
    succeeded_review_node_ids: ["a", "b"],
    failed_review_node_ids: [],
    pass_through_review_node_ids: ["c"],
    cumulative_review_node_ids: ["a", "b"],
    provider_call_count: 1,
    checkpoint_created_count: 1,
    checkpoint_reuse_count: 1,
    resumable: false,
  };
}

function interruptedAttempt(
  overrides: Partial<RequirementAtomizationScopeAttempt> = {},
): RequirementAtomizationScopeAttempt {
  return {
    schema_version: "atomization-scope-attempt/v1",
    operation: "APPLY",
    scope: "SELECTED",
    status: "INTERRUPTED",
    requested_review_node_ids: ["a", "b", "c"],
    executed_review_node_ids: ["a", "b"],
    skipped_targets: [{ review_node_id: "c", reason: "INFORMATION" }],
    cumulative_review_node_ids: ["a"],
    completed_review_node_ids: ["a"],
    failed_review_node_ids: ["b"],
    checkpoint_created_review_node_ids: ["a"],
    checkpoint_reused_review_node_ids: [],
    checkpoint_invalidated_review_node_ids: ["c"],
    provider_call_count: 2,
    retryable: true,
    checkpoint_resumable: true,
    error_code: "PROVIDER_INTERRUPTED",
    ...overrides,
  };
}

describe("AtomizationScopeInspector", () => {
  it("keeps the inspector concise and states that no split is the default", async () => {
    const user = userEvent.setup();
    const input = props();
    render(<AtomizationScopeInspector {...input} />);

    const inspector = screen.getByRole("region", { name: "需求原子化" });
    expect(inspector).toHaveTextContent("默认保留原条目，不拆分");
    expect(inspector).toHaveTextContent("需求分析前的独立、人工触发步骤");
    expect(inspector).toHaveTextContent("完整父条目和来源证据始终保留");
    expect(inspector).toHaveTextContent(input.viewModel.summary);

    await user.click(
      screen.getByRole("button", { name: "选择原子化范围" }),
    );
    expect(input.onOpenDialog).toHaveBeenCalledOnce();
    expect(input.onApply).not.toHaveBeenCalled();
  });

  it("exposes server state and an accessible explicit undo action", async () => {
    const user = userEvent.setup();
    const input = props({
      serverStateLabel: "已应用 3 项 · 可撤销最近一次操作",
      canUndo: true,
    });
    render(<AtomizationScopeInspector {...input} />);

    const inspector = screen.getByRole("region", { name: "需求原子化" });
    expect(
      within(inspector).getByText("已应用 3 项 · 可撤销最近一次操作"),
    ).toBeInTheDocument();
    await user.click(
      within(inspector).getByRole("button", {
        name: "撤销最近一次原子化",
      }),
    );
    expect(input.onUndo).toHaveBeenCalledOnce();
    expect(input.onApply).not.toHaveBeenCalled();
  });

  it("shows the latest interrupted attempt instead of a stale success receipt", () => {
    const { rerender } = render(
      <AtomizationScopeInspector
        {...props({
          currentReceipt: successfulReceipt(),
          lastAttempt: interruptedAttempt(),
        })}
      />,
    );

    const receipt = screen.getByRole("region", {
      name: "原子化执行记录",
    });
    expect(receipt).toHaveTextContent("最近一次中断");
    for (const label of [
      "范围 3 项",
      "实际处理 2 项",
      "保留 1 项",
      "已完成 1 项",
      "失败 1 项",
      "AI 处理 2 次",
      "保存进度 1 项",
      "沿用进度 0 项",
      "失效进度 1 项",
    ]) {
      expect(within(receipt).getByLabelText(label)).toBeInTheDocument();
    }
    expect(receipt).toHaveTextContent("可重试");
    expect(receipt).toHaveTextContent("将从上次进度继续");
    expect(receipt).toHaveTextContent("AI 拆分服务暂时中断");
    expect(receipt).not.toHaveTextContent("PROVIDER_INTERRUPTED");

    rerender(
      <AtomizationScopeInspector
        {...props({
          currentReceipt: successfulReceipt(),
          lastAttempt: null,
        })}
      />,
    );
    const refreshed = screen.getByRole("region", {
      name: "原子化执行记录",
    });
    expect(refreshed).toHaveTextContent("最近原子化已完成");
    expect(refreshed).not.toHaveTextContent("PROVIDER_INTERRUPTED");
    expect(
      within(refreshed).getByLabelText("已完成 2 项"),
    ).toBeInTheDocument();
    expect(
      within(refreshed).getByLabelText("失败 0 项"),
    ).toBeInTheDocument();
  });

  it("keeps implementation terminology and raw error codes out of the interface", () => {
    render(
      <AtomizationScopeInspector
        {...props({
          dialogOpen: true,
          currentReceipt: successfulReceipt(),
          lastAttempt: interruptedAttempt({
            error_code: "RAW_BINDING_FAILURE",
          }),
        })}
      />,
    );

    expect(screen.getByText("执行过程中出现异常")).toBeInTheDocument();
    expect(document.body.textContent || "").not.toMatch(
      /工作树|canonical|锚点|binding|READY|Requirement Set|APPLIED|NOT_RUN|服务端|远端|检查点|PROVIDER_INTERRUPTED/iu,
    );
  });

  it("does not describe a running attempt as non-retryable before it reaches a terminal state", () => {
    const running: RequirementAtomizationScopeAttempt = {
      ...interruptedAttempt(),
      status: "RUNNING",
      completed_review_node_ids: [],
      failed_review_node_ids: [],
      checkpoint_created_review_node_ids: [],
      provider_call_count: 0,
      retryable: false,
      checkpoint_resumable: false,
      error_code: "",
    };
    render(
      <AtomizationScopeInspector
        {...props({ lastAttempt: running })}
      />,
    );

    const receipt = screen.getByRole("region", {
      name: "原子化执行记录",
    });
    expect(receipt).toHaveTextContent("正在执行");
    expect(receipt).not.toHaveTextContent("不可重试");
    expect(receipt).not.toHaveTextContent("需重新处理当前范围");
  });

  it("blocks server writes during RUNNING without presenting the UI as a local mutation", () => {
    const running: RequirementAtomizationScopeAttempt = {
      ...interruptedAttempt(),
      status: "RUNNING",
      completed_review_node_ids: [],
      failed_review_node_ids: [],
      checkpoint_created_review_node_ids: [],
      provider_call_count: 0,
      retryable: false,
      checkpoint_resumable: false,
      error_code: "",
    };
    render(
      <AtomizationScopeInspector
        {...props({
          dialogOpen: true,
          busy: false,
          canOpenDialog: false,
          actionGuidance:
            "系统正在执行原子化；每 2.5 秒自动更新进度，完成前不可发起新操作。",
          lastAttempt: running,
          viewModel: viewModel({ canApply: false }),
        })}
      />,
    );

    expect(screen.getByText(/每 2.5 秒自动更新进度/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "选择原子化范围" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "撤销最近一次原子化" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "当前项" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "原子化 1 项技术需求" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "关闭原子化范围" }),
    ).toBeEnabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeEnabled();
  });

  it("keeps scope adjustment available after a matching non-retryable APPLY", async () => {
    const user = userEvent.setup();
    const input = props({
      dialogOpen: true,
      canOpenDialog: true,
      actionGuidance: "最近一次执行不可重试；请调整范围后再执行。",
      viewModel: viewModel({ canApply: false }),
      lastAttempt: interruptedAttempt({ retryable: false }),
    });
    render(<AtomizationScopeInspector {...input} />);

    expect(screen.getByText(/调整范围后再执行/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "选择原子化范围" }),
    ).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "全部条目" }));
    expect(input.onScopeChange).toHaveBeenCalledWith("ALL");
    expect(
      screen.getByRole("button", { name: "原子化 1 项技术需求" }),
    ).toBeDisabled();
    expect(input.onApply).not.toHaveBeenCalled();
  });

  it("labels a retryable matching APPLY as an explicit retry", async () => {
    const user = userEvent.setup();
    const input = props({
      dialogOpen: true,
      actionGuidance: "上次执行已中断，可重试当前范围。",
      viewModel: viewModel({
        actionLabel: "重试原子化 1 项技术需求",
      }),
      lastAttempt: interruptedAttempt(),
    });
    render(<AtomizationScopeInspector {...input} />);

    expect(screen.getByText(/可重试当前范围/)).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "重试原子化 1 项技术需求" }),
    );
    expect(input.onApply).toHaveBeenCalledOnce();
  });

  it("keeps a recoverable error local, focuses it and retries without executing", async () => {
    const user = userEvent.setup();
    const input = props({
      dialogOpen: true,
      error: "范围版本已变化。所选项已保留；范围成功重读后请核对并重试。",
    });
    render(<AtomizationScopeInspector {...input} />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("所选项已保留");
    await waitFor(() => expect(alert).toHaveFocus());
    await user.click(screen.getByRole("button", { name: "重新读取范围" }));
    expect(input.onRetry).toHaveBeenCalledOnce();
    expect(input.onApply).not.toHaveBeenCalled();
    expect(input.onCloseDialog).not.toHaveBeenCalled();
  });

  it("shows all four explicit scopes and explains targets and skips", async () => {
    const user = userEvent.setup();
    const input = props({ dialogOpen: true });
    render(<AtomizationScopeInspector {...input} />);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "原子化可能把一条完整原文转换为便于分析的派生条目",
    );
    for (const label of ["当前项", "所选项", "当前分支", "全部条目"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByText("3.1 制动性能")).toBeInTheDocument();
    expect(
      screen.getByText("制动系统 › 3.1 制动性能"),
    ).toBeInTheDocument();
    expect(screen.getByText("背景说明")).toBeInTheDocument();
    expect(screen.getByText("制动系统 › 背景说明")).toBeInTheDocument();
    expect(
      screen.getByText("信息与参考不参与原子化"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "所选项" }));
    expect(input.onScopeChange).toHaveBeenCalledWith("SELECTED");
    expect(input.onApply).not.toHaveBeenCalled();
  });

  it("exposes every scope description to assistive technology with stable IDs", () => {
    const input = props({ dialogOpen: true });
    const descriptions = {
      CURRENT: "只处理当前高亮条目，不自动包含子项。",
      SELECTED: "只处理原子化复选框中的独立选择。",
      BRANCH: "包含当前条目及其在清单中的全部下级条目。",
      ALL: "检查当前清单中的全部条目。",
    } as const;
    const labels = {
      CURRENT: "当前项",
      SELECTED: "所选项",
      BRANCH: "当前分支",
      ALL: "全部条目",
    } as const;
    const { rerender } = render(<AtomizationScopeInspector {...input} />);
    const initialDescriptionIds = new Map<string, string>();

    for (const scope of Object.keys(descriptions) as Array<
      keyof typeof descriptions
    >) {
      const button = screen.getByRole("button", { name: labels[scope] });
      const descriptionId = button.getAttribute("aria-describedby");
      expect(descriptionId).toBeTruthy();
      expect(button).toHaveAttribute("data-scope", scope);
      expect(button).toHaveAccessibleDescription(descriptions[scope]);
      initialDescriptionIds.set(scope, descriptionId || "");
    }

    rerender(<AtomizationScopeInspector {...input} />);
    for (const scope of Object.keys(descriptions) as Array<
      keyof typeof descriptions
    >) {
      expect(
        screen
          .getByRole("button", { name: labels[scope] })
          .getAttribute("aria-describedby"),
      ).toBe(initialDescriptionIds.get(scope));
    }
  });

  it("keeps duplicate titles distinguishable with their complete paths", () => {
    const base = viewModel();
    const duplicateTargets = [
      {
        ...base.targets[0],
        treeNodeId: "left-duplicate",
        workflowNodeId: "workflow-left-duplicate",
        label: "重复标题",
        pathLabel: "左侧系统 › 重复标题",
      },
      {
        ...base.targets[0],
        treeNodeId: "right-duplicate",
        workflowNodeId: "workflow-right-duplicate",
        label: "重复标题",
        pathLabel: "右侧系统 › 重复标题",
      },
    ];
    render(
      <AtomizationScopeInspector
        {...props({
          dialogOpen: true,
          viewModel: {
            ...base,
            targetCount: 2,
            targets: duplicateTargets,
            plan: {
              ...base.plan,
              targets: duplicateTargets,
              items: [...duplicateTargets, ...base.plan.skipped],
            },
          },
        })}
      />,
    );

    expect(screen.getAllByText("重复标题")).toHaveLength(2);
    expect(screen.getByText("左侧系统 › 重复标题")).toBeInTheDocument();
    expect(screen.getByText("右侧系统 › 重复标题")).toBeInTheDocument();
  });

  it("caps long previews at 20 rows and reports the remaining count", () => {
    const base = viewModel();
    const targets = Array.from({ length: 21 }, (_, index) => ({
      ...base.targets[0],
      treeNodeId: `technical-${index + 1}`,
      workflowNodeId: `workflow-technical-${index + 1}`,
      label: `技术需求 ${index + 1}`,
      pathLabel: `系统 › 技术需求 ${index + 1}`,
    }));
    render(
      <AtomizationScopeInspector
        {...props({
          dialogOpen: true,
          viewModel: {
            ...base,
            targetCount: targets.length,
            targets,
            plan: {
              ...base.plan,
              targets,
              items: [...targets, ...base.plan.skipped],
            },
          },
        })}
      />,
    );

    expect(screen.getByText("技术需求 20")).toBeInTheDocument();
    expect(screen.queryByText("技术需求 21")).not.toBeInTheDocument();
    expect(screen.getByText("另有 1 项未展开显示")).toBeInTheDocument();
  });

  it("disables opening, changing, closing and applying while busy", () => {
    const input = props({ dialogOpen: true, busy: true });
    render(<AtomizationScopeInspector {...input} />);

    expect(
      screen.getByRole("button", { name: "选择原子化范围" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "撤销最近一次原子化" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "当前项" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "关闭原子化范围" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "原子化 1 项技术需求" }),
    ).toBeDisabled();
  });

  it("closes only through an enabled close action and uses the dedicated footer class", async () => {
    const user = userEvent.setup();
    const input = props({ dialogOpen: true });
    render(<AtomizationScopeInspector {...input} />);

    const dialog = screen.getByRole("dialog");
    expect(
      dialog.querySelector("footer.confirm-action-footer"),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "关闭原子化范围" }),
    );
    expect(input.onCloseDialog).toHaveBeenCalledOnce();
    expect(input.onApply).not.toHaveBeenCalled();
  });

  it("only invokes execution from the explicit confirmation button", async () => {
    const user = userEvent.setup();
    const input = props({ dialogOpen: true });
    const { rerender } = render(<AtomizationScopeInspector {...input} />);

    rerender(
      <AtomizationScopeInspector
        {...input}
        viewModel={{ ...input.viewModel, scope: "ALL", scopeLabel: "全部条目" }}
      />,
    );
    expect(input.onApply).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", { name: "原子化 1 项技术需求" }),
    );
    expect(input.onApply).toHaveBeenCalledOnce();
    expect(input.onApply).toHaveBeenCalledWith(
      expect.objectContaining({ executableWorkflowIds: ["workflow-tech"] }),
    );
  });

  it("does not offer execution when the exact range has no technical target", () => {
    const input = props({
      dialogOpen: true,
      viewModel: viewModel({
        targetCount: 0,
        estimatedRemoteCallCount: 0,
        canApply: false,
        summary: "0 项技术需求将原子化，1 项保持原条目",
        actionLabel: "执行原子化",
        emptyReason: "当前范围没有可原子化的技术需求",
        targets: [],
        plan: {
          ...viewModel().plan,
          executableWorkflowIds: [],
          estimatedRemoteCallCount: 0,
          targets: [],
        },
      }),
    });
    render(<AtomizationScopeInspector {...input} />);

    expect(
      screen.getByText("当前范围没有可原子化的技术需求"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "执行原子化" }),
    ).toBeDisabled();
    expect(input.onApply).not.toHaveBeenCalled();
  });
});
