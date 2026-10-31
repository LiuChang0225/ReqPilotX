import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { FixedWorkflowStatus } from "./FixedWorkflowStatus";
import type { ApplicationWorkflowState } from "./ApplicationHost";

it("shows localized stage reasons and evidence instead of raw machine codes", () => {
  const state: ApplicationWorkflowState = {
    schema: "reqpilot-fixed-workflow-state/v1",
    workflow: { id: "reqpilot-fixed-workflow", version: "1.0.0" },
    source_sha256: "a".repeat(64),
    stages: [
      { id: "PREPARATION", label: "资料准备", status: "SAVED" },
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
  expect(screen.queryByText("ITEMS_REQUIRED")).not.toBeInTheDocument();
  expect(screen.queryByText("WORKFLOW_CHECK")).not.toBeInTheDocument();
});
