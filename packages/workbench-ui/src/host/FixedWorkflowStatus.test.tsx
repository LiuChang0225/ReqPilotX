import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { FixedWorkflowStatus, FixedWorkflowSummary } from "./FixedWorkflowStatus";
import { HostLocaleProvider } from "./HostLocaleContext";
import type { ApplicationWorkflowState } from "./ApplicationHost";

it("shows localized stage reasons and evidence instead of raw machine codes", () => {
  const state: ApplicationWorkflowState = {
    schema: "reqpilot-fixed-workflow-state/v1",
    workflow: { id: "reqpilot-fixed-workflow", version: "1.0.0" },
    source_sha256: "a".repeat(64),
    stages: [
      { id: "PREPARATION", label: "资料准备", status: "SAVED", evidence: [{ kind: "TASK_BINDING", source_sha256: "a".repeat(64) }] },
      { id: "SOURCE_MAP", label: "来源与结构", status: "SAVED" },
      { id: "ITEMS_CLASSIFICATION", label: "条目、分类与明确派生", status: "BLOCKED", reason: "ITEMS_REQUIRED" },
      { id: "EVIDENCE_ANALYSIS", label: "依据分析", status: "BLOCKED", reason: "ITEMS_REQUIRED" },
      { id: "SELF_CHECK", label: "自查与修正", status: "SAVED", evidence: [{ kind: "WORKFLOW_CHECK", source_sha256: "a".repeat(64) }] },
      { id: "DELIVERABLE", label: "可核对成果", status: "SAVED" },
    ],
    next_stage: "ITEMS_CLASSIFICATION",
    ready_to_review: false,
    verification: "SAVED_ARTIFACTS_ONLY",
    open_items: ["ITEMS_REQUIRED"],
    human_acceptance: "NOT_CLAIMED",
  };
  render(<FixedWorkflowStatus state={state} />);
  expect(screen.getAllByText("需要先保存原文条目").length).toBeGreaterThan(0);
  expect(screen.getByText("程序自查")).toBeVisible();
  expect(screen.getByText("任务绑定")).toBeVisible();
  expect(screen.queryByText("ITEMS_REQUIRED")).not.toBeInTheDocument();
  expect(screen.queryByText("WORKFLOW_CHECK")).not.toBeInTheDocument();
});

it("rerenders workflow copy through the native host language binding and keeps standalone Chinese", () => {
  const state: ApplicationWorkflowState = {
    schema: "reqpilot-fixed-workflow-state/v1",
    workflow: { id: "reqpilot-fixed-workflow", version: "1.0.0" },
    source_sha256: "a".repeat(64),
    stages: [
      { id: "PREPARATION", label: "资料准备", status: "SAVED", evidence: [{ kind: "TASK_BINDING", source_sha256: "a".repeat(64) }] },
      { id: "SOURCE_MAP", label: "来源与结构", status: "SAVED" },
      { id: "ITEMS_CLASSIFICATION", label: "条目、分类与明确派生", status: "BLOCKED", reason: "ITEMS_REQUIRED" },
      { id: "EVIDENCE_ANALYSIS", label: "依据分析", status: "BLOCKED", reason: "ITEMS_REQUIRED" },
      { id: "SELF_CHECK", label: "自查与修正", status: "SAVED", evidence: [{ kind: "WORKFLOW_CHECK", source_sha256: "a".repeat(64) }] },
      { id: "DELIVERABLE", label: "可核对成果", status: "SAVED" },
    ],
    next_stage: "ITEMS_CLASSIFICATION", ready_to_review: false, verification: "SAVED_ARTIFACTS_ONLY",
    open_items: ["ITEMS_REQUIRED"], human_acceptance: "NOT_CLAIMED",
  };
  const english: Record<string, string> = {
    "workflow.statusHeading": "Business result stages", "workflow.workflowVersion": "Workflow v{version}",
    "workflow.stageLabel.PREPARATION": "Preparation", "workflow.stageLabel.SOURCE_MAP": "Source and structure",
    "workflow.stageLabel.ITEMS_CLASSIFICATION": "Items, classification and explicit derivation",
    "workflow.stageLabel.EVIDENCE_ANALYSIS": "Evidence analysis", "workflow.stageLabel.SELF_CHECK": "Self-check and correction",
    "workflow.stageLabel.DELIVERABLE": "Reviewable result", "workflow.stage.SAVED": "Saved",
    "workflow.stage.BLOCKED": "Waiting for an earlier stage", "workflow.reason.ITEMS_REQUIRED": "Save source items first",
    "workflow.evidence.WORKFLOW_CHECK": "Program self-check", "workflow.evidence.TASK_BINDING": "Task binding",
    "workflow.next": "Next: ", "workflow.openItems": "Open items: ",
  };
  const view = render(<HostLocaleProvider t={key => english[key] ?? key}><FixedWorkflowStatus state={state} /></HostLocaleProvider>);
  const englishView = within(view.container);
  expect(englishView.getByText("Business result stages")).toBeVisible();
  expect(englishView.getAllByText("Items, classification and explicit derivation").length).toBeGreaterThan(0);
  expect(englishView.getAllByText("Save source items first").length).toBeGreaterThan(0);
  expect(englishView.getByText("Program self-check")).toBeVisible();
  expect(englishView.queryByText("业务成果阶段")).not.toBeInTheDocument();
  view.rerender(<HostLocaleProvider><FixedWorkflowStatus state={state} /></HostLocaleProvider>);
  expect(within(view.container).getByText("业务成果阶段")).toBeVisible();
});


it("translates fixed-workflow optional inputs and expected outputs without touching unknown text", () => {
  const workflow = {
    schema: "reqpilot-fixed-workflow/v1" as const, id: "reqpilot-fixed-workflow", version: "1.0.0", label: "客户需求整理固定流程",
    stages: [{ id: "PREPARATION", order: 1, label: "资料准备", required: true }],
    optional_inputs: [
      { id: "project_references", label: "项目参考资料", available: false, reason: "当前版本尚未接入项目参考资料。" },
      { id: "team_knowledge", label: "团队知识库", available: false, reason: "当前版本尚未接入团队知识库。" },
      { id: "custom_a", label: "客户原文自定义依据", available: false, reason: "客户提供的原文原因" },
    ],
    expected_outputs: [
      "可回查原文与顺序的文档结构",
      "保留完整父项的原文条目、分类及明确授权的必要派生",
      "有内部依据时的引用分析；无内部依据时明确标记未执行",
      "程序自查结果与当前可核对成果版本",
    ],
  };
  const english: Record<string, string> = {
    "workflow.defaultLabel": "Customer requirement fixed workflow",
    "workflow.version": "Version {version}", "workflow.required": "Required inputs", "workflow.customerSource": "Customer requirement document",
    "workflow.optional": "Optional internal evidence",
    "workflow.optional.project_references.label": "Project references",
    "workflow.optional.project_references.reason": "Project references are not connected in this version.",
    "workflow.optional.team_knowledge.label": "Team knowledge",
    "workflow.optional.team_knowledge.reason": "Team knowledge is not connected in this version.",
    "workflow.available": "Available", "workflow.unavailable": "Unavailable in this version",
    "workflow.expected": "Expected outputs",
    "workflow.expected.structure": "Document structure with traceable original and order",
    "workflow.expected.items": "Source items, classification and authorized derivation with complete parents",
    "workflow.expected.evidence": "Referenced analysis when internal evidence exists, otherwise explicitly marked as not run",
    "workflow.expected.check": "Program self-check result and the current reviewable result version",
    "workflow.stageLabel.PREPARATION": "Preparation",
  };
  const view = render(<HostLocaleProvider t={key => english[key] ?? key}><FixedWorkflowSummary workflow={workflow} /></HostLocaleProvider>);
  const scope = within(view.container);
  expect(scope.getByText(/Project references：Project references are not connected in this version\./)).toBeVisible();
  expect(scope.getByText(/Team knowledge：Team knowledge is not connected in this version\./)).toBeVisible();
  expect(scope.getByText(/Document structure with traceable original and order/)).toBeVisible();
  expect(scope.getByText(/客户原文自定义依据：客户提供的原文原因/)).toBeVisible();
  expect(scope.queryByText(/项目参考资料/)).not.toBeInTheDocument();
  expect(scope.queryByText(/可回查原文与顺序/)).not.toBeInTheDocument();
});

it("keeps the workflow as one compact status row with real blocker facts and expandable details", () => {
  const state: ApplicationWorkflowState = {
    schema: "reqpilot-fixed-workflow-state/v1",
    workflow: { id: "reqpilot-fixed-workflow", version: "1.0.0" },
    source_sha256: "a".repeat(64),
    stages: [
      { id: "PREPARATION", label: "Preparation", status: "SAVED" },
      { id: "SOURCE_MAP", label: "Source and structure", status: "SAVED" },
      { id: "ITEMS_CLASSIFICATION", label: "Items and classification", status: "BLOCKED", reason: "ITEMS_REQUIRED" },
      { id: "EVIDENCE_ANALYSIS", label: "Evidence analysis", status: "PENDING" },
      { id: "SELF_CHECK", label: "Self-check", status: "PENDING" },
      { id: "DELIVERABLE", label: "Reviewable result", status: "PENDING" },
    ],
    next_stage: "ITEMS_CLASSIFICATION", ready_to_review: false, verification: "SAVED_ARTIFACTS_ONLY",
    open_items: ["ITEMS_REQUIRED"], human_acceptance: "NOT_CLAIMED",
  };
  const english: Record<string, string> = {
    "workflow.statusHeading": "Business result stages", "workflow.currentStage": "Current: {stage}",
    "workflow.stageLabel.ITEMS_CLASSIFICATION": "Items and classification", "workflow.stage.BLOCKED": "Waiting for an earlier stage",
    "workflow.reason.ITEMS_REQUIRED": "Save source items first", "workflow.viewDetails": "View details",
    "workflow.hideDetails": "Hide details", "workflow.next": "Next: ", "workflow.openItems": "Open items: ",
    "workflow.stageLabel.SOURCE_MAP": "Source and structure", "workflow.stage.SAVED": "Saved",
    "workflow.stageLabel.PREPARATION": "Preparation", "workflow.stageLabel.EVIDENCE_ANALYSIS": "Evidence analysis",
    "workflow.stageLabel.SELF_CHECK": "Self-check", "workflow.stageLabel.DELIVERABLE": "Reviewable result",
    "workflow.stage.PENDING": "Not saved",
  };
  render(<HostLocaleProvider t={key => english[key] ?? key}><FixedWorkflowStatus state={state} compact /></HostLocaleProvider>);
  expect(screen.getByText("Current: Items and classification")).toBeVisible();
  expect(screen.getAllByText("Waiting for an earlier stage").length).toBeGreaterThan(0);
  expect(screen.getAllByText("Save source items first").length).toBeGreaterThan(0);
  const toggle = screen.getByRole("button", { name: "View details" });
  toggle.focus();
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(document.querySelector(".fixed-workflow-compact-details")).toHaveAttribute("hidden");
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("button", { name: "Hide details" })).toHaveFocus();
  expect(document.querySelector(".fixed-workflow-compact-details")).not.toHaveAttribute("hidden");
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(toggle).toHaveFocus();
});

it("shows the next actionable stage and keeps unresolved operations visible when details are closed", () => {
  const state: ApplicationWorkflowState = {
    schema: "reqpilot-fixed-workflow-state/v1", workflow: { id: "reqpilot-fixed-workflow", version: "1.0.0" },
    source_sha256: "a".repeat(64), stages: [
      { id: "PREPARATION", label: "Preparation", status: "SAVED" },
      { id: "SOURCE_MAP", label: "Source and structure", status: "PENDING" },
      { id: "ITEMS_CLASSIFICATION", label: "Items", status: "BLOCKED", reason: "SOURCE_MAP_REQUIRED" },
    ], next_stage: "SOURCE_MAP", ready_to_review: false, verification: "SAVED_ARTIFACTS_ONLY",
    open_items: [], human_acceptance: "NOT_CLAIMED",
  };
  const { rerender } = render(<FixedWorkflowStatus state={state} compact />);
  expect(screen.getByText("当前：Source and structure")).toBeVisible();
  rerender(<FixedWorkflowStatus state={{ ...state, next_stage: null,
    stages: state.stages.map(stage => ({ ...stage, status: "SAVED", reason: undefined })),
    open_items: ["INCOMPLETE_OPERATION"],
  }} compact />);
  expect(screen.getAllByText("存在结果未知或待恢复的程序操作").some(node => !node.closest("[hidden]"))).toBe(true);
  expect(screen.getByRole("button", { name: "查看详情" })).toHaveAttribute("aria-expanded", "false");
});
