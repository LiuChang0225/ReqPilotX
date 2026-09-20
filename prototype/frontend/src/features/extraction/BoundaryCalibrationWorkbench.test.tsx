import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PageSnapshot } from "../restoration/restorationModel";
import { BoundaryCalibrationWorkbench } from "./BoundaryCalibrationWorkbench";

const plan = {
  schema_version: "boundary-calibration-page/v1",
  page: { number: 2, width: 595, height: 842 },
  binding: {
    revision_number: 4,
    effective_snapshot_id: "effective-4",
    geometry_index_sha256: "c".repeat(64),
  },
  segments: [
    {
      node_id: "split-target",
      parent_node_id: "clause-2.1",
      kind: "CONTENT",
      label: "2.1.1 安装间隙",
      path: ["2 总成要求", "2.1 安装", "2.1.1 安装间隙"],
      source_id: "TXT-001",
      start_offset: 10,
      end_offset: 50,
      bbox: [70, 120, 525, 210],
      line_regions: [],
      editable: true,
      locked_reason: "",
    },
  ],
  boundaries: [
    {
      boundary_id: "split-1",
      mode: "SPLIT",
      before_node_id: "",
      after_node_id: "",
      target_node_id: "split-target",
      source_id: "TXT-001",
      current_offset: 30,
      editable: true,
      locked_reason: "",
      anchors: [
        { offset: 20, bbox: [70, 150, 525, 168], line_id: "line-1" },
        { offset: 30, bbox: [70, 180, 525, 198], line_id: "line-2" },
      ],
    },
  ],
};

const pageSnapshot: PageSnapshot = {
  blocks: [
    {
      id: "TXT-001",
      type: "TEXT",
      role: "",
      label: "正文",
      text: "安装间隙应满足图纸要求。",
      bbox: [70, 120, 525, 210],
      locatable: true,
      locationStatus: "EXACT",
      sourceOrder: 0,
      pageNumber: 2,
      tableId: "",
      provenance: {},
      diagnostics: [],
    },
  ],
  issues: [],
  tables: [],
  bounds: [0, 0, 595, 842],
  counts: {},
  coverage: {},
  sourceSha256: "a".repeat(64),
};

afterEach(cleanup);

describe("BoundaryCalibrationWorkbench", () => {
  it("moves by verified line anchors, previews, and applies the same operation", async () => {
    const user = userEvent.setup();
    const onPreview = vi.fn();
    const onApply = vi.fn();
    const onDraftChange = vi.fn();
    render(
      <BoundaryCalibrationWorkbench
        plan={plan}
        pageSnapshot={pageSnapshot}
        busy={false}
        onPreview={onPreview}
        onApply={onApply}
        onCancel={() => undefined}
        onDraftChange={onDraftChange}
      />,
    );

    expect(screen.getByText("安装间隙应满足图纸要求。")).toBeInTheDocument();
    expect(screen.getByText("结构版本 4")).toBeInTheDocument();
    expect(
      screen.getAllByText("2 总成要求 › 2.1 安装 › 2.1.1 安装间隙"),
    ).not.toHaveLength(0);

    const rail = screen.getByRole("button", {
      name: /字符偏移 30.*上下方向键/,
    });
    rail.focus();
    await user.keyboard("{ArrowUp}");
    await waitFor(() =>
      expect(onDraftChange).toHaveBeenLastCalledWith(
        expect.objectContaining({
          boundaryId: "split-1",
          offset: 20,
          bbox: [70, 150, 525, 168],
        }),
      ),
    );
    expect(
      screen.getByRole("button", {
        name: /字符偏移 20.*上下方向键/,
      }),
    ).toHaveFocus();
    await user.keyboard("{Enter}");

    await waitFor(() => expect(onPreview).toHaveBeenCalledTimes(1));
    expect(onPreview.mock.calls[0][0]).toEqual({
      operation: "boundary_split",
      structure_node_id: "split-target",
      parts: [
        {
          source_spans: [
            {
              source_ref: {
                kind: "source_node",
                source_id: "TXT-001",
              },
              start_offset: 10,
              end_offset: 20,
            },
          ],
        },
        {
          source_spans: [
            {
              source_ref: {
                kind: "source_node",
                source_id: "TXT-001",
              },
              start_offset: 20,
              end_offset: 50,
            },
          ],
        },
      ],
    });
    expect(onPreview.mock.calls[0][1]).toMatchObject({
      boundary_id: "split-1",
      selected_offset: 20,
      binding: {
        revision_number: 4,
        effective_snapshot_id: "effective-4",
      },
    });

    await user.click(screen.getByRole("button", { name: "应用修订" }));
    expect(onApply).toHaveBeenCalledWith(
      onPreview.mock.calls[0][0],
      onPreview.mock.calls[0][1],
    );
  });

  it("cancels the draft with Escape", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(
      <BoundaryCalibrationWorkbench
        plan={plan}
        pageSnapshot={pageSnapshot}
        busy={false}
        onPreview={() => undefined}
        onApply={() => undefined}
        onCancel={onCancel}
      />,
    );

    const rail = screen.getByRole("button", {
      name: /字符偏移 30.*上下方向键/,
    });
    rail.focus();
    await user.keyboard("{ArrowUp}{Escape}");

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", {
        name: /字符偏移 30.*上下方向键/,
      }),
    ).toBeInTheDocument();
  });

  it("requests a split plan for the source-bound segment", async () => {
    const user = userEvent.setup();
    const onSegmentSelect = vi.fn();
    render(
      <BoundaryCalibrationWorkbench
        plan={plan}
        pageSnapshot={pageSnapshot}
        busy={false}
        onPreview={() => undefined}
        onApply={() => undefined}
        onCancel={() => undefined}
        onSegmentSelect={onSegmentSelect}
      />,
    );

    await user.click(
      screen.getByRole("button", {
        name: "Item：2 总成要求 › 2.1 安装 › 2.1.1 安装间隙",
      }),
    );

    expect(onSegmentSelect).toHaveBeenCalledWith("split-target");
  });
});
