import { describe, expect, it } from "vitest";
import {
  boundarySelection,
  buildCanonicalBoundaryOperation,
  initialBoundaryOffset,
  nearestBoundaryAnchor,
  normalizeBoundaryCalibrationPlan,
  stepBoundaryAnchor,
} from "./boundaryCalibrationModel";

function rawPlan() {
  return {
    schema_version: "boundary-calibration-page/v1",
    page: { number: 4, width: 595, height: 842 },
    binding: {
      revision_number: 3,
      effective_snapshot_id: "effective-3",
      geometry_index_sha256: "g".repeat(64),
    },
    segments: [
      {
        node_id: "preceding",
        parent_node_id: "clause",
        kind: "CONTENT",
        label: "3.1.1 前半条目",
        path: ["3 产品要求", "3.1 强度", "3.1.1 前半条目"],
        source_id: "source-block-1",
        source_kind: "source_node",
        start_offset: 0,
        end_offset: 50,
        bbox: [70, 90, 530, 190],
        line_regions: [
          {
            line_id: "line-before",
            bbox: [70, 170, 530, 188],
            start_offset: 30,
            end_offset: 50,
          },
        ],
        editable: true,
        locked_reason: "",
      },
      {
        node_id: "following",
        parent_node_id: "clause",
        kind: "CONTENT",
        label: "3.1.2 后半条目",
        path: ["3 产品要求", "3.1 强度", "3.1.2 后半条目"],
        source_id: "source-block-1",
        start_offset: 50,
        end_offset: 100,
        bbox: [70, 195, 530, 300],
        line_regions: [],
        editable: true,
        locked_reason: "",
      },
      {
        node_id: "split-target",
        parent_node_id: "clause",
        kind: "CLAUSE",
        label: "3.2 安装",
        path: "3 产品要求 › 3.2 安装",
        source_id: "source-block-2",
        start_offset: 100,
        end_offset: 180,
        bbox: [70, 340, 530, 450],
        line_regions: [],
        editable: true,
        locked_reason: "",
      },
      {
        node_id: "",
        source_id: "invalid",
        start_offset: 0,
        end_offset: 10,
        bbox: [0, 0, 10, 10],
      },
    ],
    boundaries: [
      {
        boundary_id: "resegment-1",
        mode: "RESEGMENT",
        before_node_id: "preceding",
        after_node_id: "following",
        target_node_id: "",
        source_id: "source-block-1",
        current_offset: 50,
        editable: true,
        locked_reason: "",
        anchors: [
          { offset: 60, bbox: [70, 220, 530, 238], line_id: "line-60" },
          { offset: 40, bbox: [70, 150, 530, 168], line_id: "line-40" },
          { offset: 50, bbox: [70, 185, 530, 203], line_id: "line-50" },
          { offset: 50, bbox: [70, 186, 530, 204], line_id: "duplicate" },
          { offset: 70, bbox: [1, 2], line_id: "invalid" },
        ],
      },
      {
        boundary_id: "split-1",
        mode: "SPLIT",
        before_node_id: "",
        after_node_id: "",
        target_node_id: "split-target",
        source_id: "source-block-2",
        current_offset: 140,
        editable: true,
        locked_reason: "",
        anchors: [
          { offset: 130, bbox: [70, 370, 530, 388], line_id: "line-130" },
          { offset: 140, bbox: [70, 400, 530, 418], line_id: "line-140" },
        ],
      },
      {
        boundary_id: "locked-1",
        mode: "SPLIT",
        target_node_id: "split-target",
        source_id: "source-block-2",
        current_offset: 140,
        editable: true,
        anchors: [],
      },
    ],
  };
}

describe("normalizeBoundaryCalibrationPlan", () => {
  it("normalizes page geometry and fails closed on malformed facts", () => {
    const plan = normalizeBoundaryCalibrationPlan(rawPlan());

    expect(plan).not.toBeNull();
    expect(plan?.page).toEqual({ number: 4, width: 595, height: 842 });
    expect(plan?.segments).toHaveLength(3);
    expect(plan?.segments[1].sourceKind).toBe("source_node");
    expect(plan?.segments[2].path).toEqual([
      "3 产品要求 › 3.2 安装",
    ]);
    expect(plan?.boundaries[0].anchors.map((item) => item.offset)).toEqual([
      40, 50, 60,
    ]);
    expect(plan?.boundaries[2]).toMatchObject({
      editable: false,
      lockedReason: "当前边界没有可吸附的原文行",
    });
  });

  it("rejects payloads without the bound page contract", () => {
    expect(
      normalizeBoundaryCalibrationPlan({
        schema_version: "boundary-calibration-page/v2",
        page: { number: 1, width: 595, height: 842 },
      }),
    ).toBeNull();
    expect(
      normalizeBoundaryCalibrationPlan({
        schema_version: "boundary-calibration-page/v1",
        page: { number: 0, width: 0, height: 842 },
      }),
    ).toBeNull();
  });

  it("accepts the canonical preceding/following node field names", () => {
    const payload = rawPlan();
    const boundary = payload.boundaries[0] as Record<string, unknown>;
    boundary.preceding_node_id = boundary.before_node_id;
    boundary.following_node_id = boundary.after_node_id;
    delete boundary.before_node_id;
    delete boundary.after_node_id;

    const plan = normalizeBoundaryCalibrationPlan(payload);

    expect(plan?.boundaries[0]).toMatchObject({
      beforeNodeId: "preceding",
      afterNodeId: "following",
      editable: true,
    });
  });
});

describe("boundary anchors", () => {
  it("snaps pointer movement and keyboard movement only to supplied anchors", () => {
    const plan = normalizeBoundaryCalibrationPlan(rawPlan());
    const boundary = plan?.boundaries[0];
    expect(boundary).toBeDefined();
    if (!boundary) return;

    expect(initialBoundaryOffset(boundary)).toBe(50);
    expect(nearestBoundaryAnchor(boundary, 216)?.offset).toBe(60);
    expect(stepBoundaryAnchor(boundary, 50, -1)?.offset).toBe(40);
    expect(stepBoundaryAnchor(boundary, 50, 1)?.offset).toBe(60);
    expect(stepBoundaryAnchor(boundary, 60, 1)?.offset).toBe(60);
  });
});

describe("buildCanonicalBoundaryOperation", () => {
  it("builds an exact-cover boundary_split from a verified anchor", () => {
    const plan = normalizeBoundaryCalibrationPlan(rawPlan());
    expect(plan).not.toBeNull();
    if (!plan) return;

    expect(
      buildCanonicalBoundaryOperation(plan, "split-1", 130),
    ).toEqual({
      operation: "boundary_split",
      structure_node_id: "split-target",
      parts: [
        {
          source_spans: [
            {
              source_ref: {
                kind: "source_node",
                source_id: "source-block-2",
              },
              start_offset: 100,
              end_offset: 130,
            },
          ],
        },
        {
          source_spans: [
            {
              source_ref: {
                kind: "source_node",
                source_id: "source-block-2",
              },
              start_offset: 130,
              end_offset: 180,
            },
          ],
        },
      ],
    });
  });

  it("builds boundary_resegment only for adjacent, same-parent spans", () => {
    const plan = normalizeBoundaryCalibrationPlan(rawPlan());
    expect(plan).not.toBeNull();
    if (!plan) return;

    expect(
      buildCanonicalBoundaryOperation(plan, "resegment-1", 60),
    ).toEqual({
      operation: "boundary_resegment",
      preceding_structure_node_id: "preceding",
      following_structure_node_id: "following",
      new_boundary_offset: 60,
    });
    expect(
      buildCanonicalBoundaryOperation(plan, "resegment-1", 50),
    ).toBeNull();
    expect(
      buildCanonicalBoundaryOperation(plan, "resegment-1", 55),
    ).toBeNull();

    plan.segments[1].parentNodeId = "another-clause";
    expect(
      buildCanonicalBoundaryOperation(plan, "resegment-1", 60),
    ).toBeNull();
  });

  it("keeps the optimistic structure and geometry binding with the selection", () => {
    const plan = normalizeBoundaryCalibrationPlan(rawPlan());
    expect(plan).not.toBeNull();
    if (!plan) return;

    expect(boundarySelection(plan, "split-1", 130)).toEqual({
      boundary_id: "split-1",
      selected_offset: 130,
      binding: {
        revision_number: 3,
        effective_snapshot_id: "effective-3",
        geometry_index_sha256: "g".repeat(64),
      },
    });
  });
});
