import { describe, expect, it } from "vitest";
import {
  codePointOffset,
  excludeReviewNode,
  mergeReviewNode,
  reincludeReviewNode,
  reviewNodeText,
  splitReviewNode,
  type ReviewModelNode,
  type ReviewNode,
} from "./reviewEdits";

function directNode(): ReviewNode {
  return {
    review_node_id: "review-1",
    source_semantic_unit_ids: ["unit-1"],
    source_owner_structure_node_ids: ["structure-1"],
    source_spans: [
      {
        semantic_unit_id: "unit-1",
        owner_structure_node_id: "structure-1",
        start_offset: 0,
        end_offset: 3,
      },
    ],
    source_order: 1,
    semantic_class: "REQUIREMENT",
    include_in_view: true,
    role: "REQUIREMENT",
    parent_review_node_id: null,
    decision_mode: "DIRECT",
  };
}

const model = new Map<string, ReviewModelNode>([
  [
    "unit-1",
    {
      semantic_unit_id: "unit-1",
      review_text: "A😀B",
      role: "REQUIREMENT",
      decision_mode: "DIRECT",
    },
  ],
]);

describe("review edits", () => {
  it("converts browser UTF-16 selections to Unicode code point offsets", () => {
    expect(codePointOffset("A😀B", 2)).toBe(1);
    expect(codePointOffset("A😀B", 3)).toBe(2);
  });

  it("splits and merges without changing source lineage", () => {
    const split = splitReviewNode([directNode()], "review-1", 1, 2, 4);
    expect(split.nodes).toHaveLength(3);
    expect(reviewNodeText(split.nodes[1], model)).toBe("😀");
    expect(split.nodes.flatMap((node) => node.source_spans)).toEqual([
      {
        semantic_unit_id: "unit-1",
        owner_structure_node_id: "structure-1",
        start_offset: 0,
        end_offset: 1,
      },
      {
        semantic_unit_id: "unit-1",
        owner_structure_node_id: "structure-1",
        start_offset: 1,
        end_offset: 2,
      },
      {
        semantic_unit_id: "unit-1",
        owner_structure_node_id: "structure-1",
        start_offset: 2,
        end_offset: 3,
      },
    ]);

    const firstMerge = mergeReviewNode(split.nodes, split.nodes[0].review_node_id);
    const restored = mergeReviewNode(firstMerge, firstMerge[0].review_node_id);
    expect(restored).toHaveLength(1);
    expect(restored[0].source_spans[0]).toMatchObject({
      start_offset: 0,
      end_offset: 3,
    });
    expect(reviewNodeText(restored[0], model)).toBe("A😀B");
  });

  it("requires the frozen model projection before restoring an excluded node", () => {
    const excluded = excludeReviewNode([directNode()], "review-1");
    expect(excluded[0].include_in_view).toBe(false);
    expect(() =>
      reincludeReviewNode(excluded, "review-1", new Map()),
    ).toThrow("缺少原模型投影");
    const restored = reincludeReviewNode(excluded, "review-1", model);
    expect(restored[0]).toMatchObject({
      include_in_view: true,
      decision_mode: "DIRECT",
      role: "REQUIREMENT",
    });
  });
});
