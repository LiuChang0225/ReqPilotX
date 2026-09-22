import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../../shared/api/client";
import type { NativeParentProvisionBaselinePublicV1 } from "../../shared/api/contracts";
import { NativeParentProvisionBaselineDock } from "./NativeParentProvisionBaselineDock";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const result: NativeParentProvisionBaselinePublicV1 = {
  schema_version: "native-parent-provision-baseline-public/v1",
  source_sha256: "a".repeat(64),
  baseline_id: "native-structure-baseline-" + "1".repeat(32),
  baseline_sha256: "1".repeat(64),
  artifact_id: "agent-artifact-" + "2".repeat(32),
  artifact_sha256: "2".repeat(64),
  status: "COMPLETE",
  professional_policy_attested: true,
  summary: {
    parent_provision_count: 2,
    declared_scope_count: 2,
    requirementness_counts: { REQUIREMENT: 1, NON_REQUIREMENT: 1 },
  },
  entries: [
    {
      parent_provision_id: "parent-provision-" + "3".repeat(32),
      source_order: 0,
      logical_object_ids: ["d2-logical-object-" + "4".repeat(32)],
      explicit_label: "1.1 交付语言",
      ancestor_labels: ["1 概述"],
      requirementness: "REQUIREMENT",
      requirement_kind: "CONSTRAINT",
      technicality: "NON_TECHNICAL",
      content_role: "PROVISION",
      confidence: 0.96,
      reason_codes: ["SOURCE_BOUND_OBLIGATION"],
      source_ranges: [
        {
          segment_id: "segment-1",
          start_offset: 0,
          end_offset: 12,
          verbatim_text: "只接受简体中文文件",
          verbatim_text_sha256: "5".repeat(64),
          page: 1,
          bbox: [10, 20, 100, 40],
          source_bounds: [0, 0, 595, 842],
          precision: "EXACT_SOURCE_BLOCK",
        },
      ],
      verbatim_text: "只接受简体中文文件",
    },
    {
      parent_provision_id: "parent-provision-" + "6".repeat(32),
      source_order: 1,
      logical_object_ids: ["d2-logical-object-" + "7".repeat(32)],
      explicit_label: "修订记录",
      ancestor_labels: [],
      requirementness: "NON_REQUIREMENT",
      requirement_kind: null,
      technicality: null,
      content_role: "METADATA",
      confidence: 0.99,
      reason_codes: ["DOCUMENT_METADATA"],
      source_ranges: [
        {
          segment_id: "segment-2",
          start_offset: 0,
          end_offset: 4,
          verbatim_text: "首版",
          verbatim_text_sha256: "8".repeat(64),
          page: 1,
          bbox: [10, 50, 100, 70],
          source_bounds: [0, 0, 595, 842],
          precision: "EXACT_TABLE_CELL",
        },
      ],
      verbatim_text: "首版",
    },
  ],
};

describe("NativeParentProvisionBaselineDock", () => {
  it("shows source-verbatim judgments and switches parent evidence", async () => {
    vi.spyOn(api, "nativeParentProvisionResult").mockResolvedValue(result);
    const user = userEvent.setup();
    render(
      <NativeParentProvisionBaselineDock
        projectId="project-1"
        documentId="document-1"
        sourceFileUrl="/api/source.pdf"
      />,
    );

    expect((await screen.findAllByText("1.1 交付语言")).length).toBeGreaterThan(0);
    expect(screen.getByText("只接受简体中文文件")).toBeInTheDocument();
    expect(screen.getByText("约束")).toBeInTheDocument();
    expect(screen.getByText("专业证明已闭合")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /修订记录/ }));
    expect(screen.getByText("首版")).toBeInTheDocument();
    expect(screen.getAllByText("非需求").length).toBeGreaterThan(0);
  });
});
