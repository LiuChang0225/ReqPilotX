import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  EntryClassificationResponse,
  RequirementTreeNode,
} from "../../shared/api/contracts";
import { EntryClassificationInspector } from "./EntryClassificationInspector";

afterEach(cleanup);

const sha = "a".repeat(64);

function node(overrides: Partial<RequirementTreeNode> = {}): RequirementTreeNode {
  return {
    id: "entry-1",
    parent_id: null,
    kind: "CLAUSE",
    depth: 0,
    source_order: 0,
    label: "3.2 诊断要求",
    summary: "系统应记录诊断结果。",
    child_ids: [],
    structure_node_id: "clause-1",
    requirement_entry_id: null,
    workflow_node_id: "workflow-1",
    page_start: 1,
    page_end: 1,
    source_refs: [],
    requirement_count: 0,
    attention_count: 0,
    reviewed_count: 0,
    status: "READY",
    classification: "NOT_CLASSIFIED",
    projection_role: "STRUCTURE_PROXY",
    include_in_view: true,
    content_blocks: [],
    table_refs: [],
    figure_refs: [],
    ...overrides,
  };
}

function classification(
  status: "WORKING" | "ACCEPTED" = "WORKING",
): EntryClassificationResponse {
  return {
    schema_version: "entry-classification-response/v1",
    snapshot: {
      schema_version: "entry-classification-snapshot/v1",
      snapshot_id: `${status.toLowerCase()}-1`,
      payload_sha256: sha,
      project_id: "project",
      sor_document_id: "document",
      source_sha256: sha,
      working_tree_id: sha,
      working_tree_sha256: sha,
      effective_structure_id: "effective",
      semantic_snapshot_id: "",
      policy_version: "entry-classification-policy/v1",
      revision: 1,
      status,
      entries: [
        {
          tree_node_id: "entry-1",
          workflow_node_id: "workflow-1",
          projection_role: "STRUCTURE_PROXY",
          suggested_type: null,
          suggestion_source: "NONE",
          effective_type: status === "ACCEPTED" ? "TECHNICAL" : null,
          human_override: status === "ACCEPTED" ? "TECHNICAL" : null,
          reason_codes: ["NO_DEDICATED_CLASSIFICATION_SUGGESTION"],
          confidence: null,
        },
      ],
      supersedes_id: "",
      created_at: "",
      accepted_at: status === "ACCEPTED" ? "2026-08-07T00:00:00Z" : "",
      accepted_by: status === "ACCEPTED" ? "LOCAL_USER" : "",
    },
    counts: {
      total: 1,
      technical: status === "ACCEPTED" ? 1 : 0,
      non_technical: 0,
      information: 0,
      unclassified: status === "ACCEPTED" ? 0 : 1,
      human_override: status === "ACCEPTED" ? 1 : 0,
    },
    can_accept: false,
    can_publish: status === "ACCEPTED",
    actions: {
      can_override: status === "WORKING",
      can_accept: false,
      can_publish: status === "ACCEPTED",
    },
    changed: false,
    serving_impact: "NONE",
  };
}

describe("EntryClassificationInspector", () => {
  it("uses user-facing save state instead of exposing policy versions", () => {
    render(
      <EntryClassificationInspector
        node={node()}
        classification={classification()}
        busy={false}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText("修改会自动保存")).toBeInTheDocument();
    expect(screen.queryByText(/entry-classification-policy/)).not.toBeInTheDocument();
  });

  it("does not mislabel a classifiable row while the snapshot is unavailable", () => {
    render(
      <EntryClassificationInspector
        node={node()}
        classification={null}
        busy={false}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText("判断结果暂不可用")).toBeInTheDocument();
    expect(
      screen.queryByText("结构标题不作为需求判断"),
    ).not.toBeInTheDocument();
  });

  it("offers the three business classes for one classifiable content row", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <EntryClassificationInspector
        node={node()}
        classification={classification()}
        busy={false}
        onChange={onChange}
      />,
    );

    expect(
      screen.getByRole("region", { name: "需求基线判断" }),
    ).toHaveTextContent("尚未分类");
    await user.click(screen.getByRole("button", { name: "技术需求" }));
    expect(onChange).toHaveBeenCalledWith("workflow-1", "TECHNICAL");
    expect(
      screen.getByText("功能、性能、接口、安全、验证或其他技术约束"),
    ).toBeInTheDocument();
    expect(screen.getByText(/本步骤不改写、摘要或拆分原文/)).toBeInTheDocument();
  });

  it("explains that structural titles remain in the matrix but are not classified", () => {
    render(
      <EntryClassificationInspector
        node={node({ id: "heading", projection_role: "STRUCTURE" })}
        classification={classification()}
        busy={false}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText("结构标题不作为需求判断")).toBeInTheDocument();
    expect(screen.getByText(/仍会完整保留在基线和导出矩阵/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "技术需求" }),
    ).not.toBeInTheDocument();
  });

  it("keeps an accepted classification read-only", () => {
    render(
      <EntryClassificationInspector
        node={node()}
        classification={classification("ACCEPTED")}
        busy={false}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText("本轮分类已接受")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "技术需求" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "技术需求" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
