import { describe, expect, it } from "vitest";
import type {
  RequirementTreeNode,
} from "../../shared/api/contracts";
import type { PageSnapshot } from "../restoration/restorationModel";
import {
  geometryFocusRegionsForNode,
  requirementTreeFocusOverlays,
} from "./requirementTreeFocus";

function node(overrides: Partial<RequirementTreeNode>): RequirementTreeNode {
  return {
    id: "clause-4.1.4",
    parent_id: "section-4.1",
    kind: "CLAUSE",
    depth: 2,
    source_order: 10,
    label: "4.1.4 试验电压",
    summary: "试验电压按表1。",
    child_ids: [],
    structure_node_id: "clause-4.1.4",
    requirement_entry_id: null,
    workflow_node_id: null,
    page_start: 10,
    page_end: 10,
    source_refs: [],
    requirement_count: 3,
    attention_count: 0,
    reviewed_count: 0,
    status: "READY",
    classification: "STRUCTURE",
    include_in_view: true,
    content_blocks: [],
    table_refs: [],
    figure_refs: [],
    ...overrides,
  };
}

const pageSnapshot: PageSnapshot = {
  blocks: Array.from({ length: 32 }, (_, index) => ({
    id: `TXT-${String(index + 1).padStart(3, "0")}`,
    type: "TEXT",
    role: "",
    label: `TXT-${String(index + 1).padStart(3, "0")}`,
    text: `page fact ${index + 1}`,
    bbox: [10, 10 + index, 100, 20 + index],
    locatable: true,
    locationStatus: "",
    sourceOrder: index,
    pageNumber: 10,
    tableId: "",
    provenance: {},
    diagnostics: [],
  })),
  issues: [],
  tables: [],
  bounds: [0, 0, 595, 842],
  counts: {},
  coverage: {},
  sourceSha256: "a".repeat(64),
};

describe("requirementTreeFocusOverlays", () => {
  it("rejects geometry left over from the previously selected node", () => {
    expect(
      geometryFocusRegionsForNode(
        {
          node_id: "clause-3.1",
          regions: [{ id: "previous-clause-line" }],
        },
        "clause-3.2",
      ),
    ).toBeNull();
    expect(
      geometryFocusRegionsForNode(
        {
          node_id: "clause-3.2",
          regions: [{ id: "current-clause-line" }],
        },
        "clause-3.2",
      ),
    ).toEqual([{ id: "current-clause-line" }]);
  });

  it("rejects geometry from a different immutable geometry index", () => {
    expect(
      geometryFocusRegionsForNode(
        {
          node_id: "clause-3.2",
          geometry_identity: {
            geometry_index_id: "index-old",
            geometry_index_sha256: "sha-old",
          },
          regions: [{ id: "old-index-line" }],
        },
        "clause-3.2",
        {
          geometryIndexId: "index-current",
          geometryIndexSha256: "sha-current",
        },
      ),
    ).toBeNull();
  });

  it("sends only the selected clause regions instead of every page fact", () => {
    const selected = node({});
    const focusRegions = [
        {
          id: "clause-line-1",
          page_number: 10,
          bbox: [70, 210, 540, 224],
          role: "CLAUSE_TEXT",
          precision: "EXACT_LINE",
        },
        {
          id: "clause-line-2",
          page_number: 10,
          bbox: [70, 228, 540, 242],
          role: "CLAUSE_TEXT",
          precision: "EXACT_LINE",
        },
        {
          id: "table-caption",
          page_number: 10,
          bbox: [250, 263, 357, 289],
          role: "TABLE_CAPTION",
          precision: "EXACT_BLOCK",
        },
        {
          id: "table-1",
          page_number: 10,
          bbox: [63, 300, 546, 391],
          role: "TABLE",
          precision: "EXACT_BLOCK",
        },
        {
          id: "figure-1",
          page_number: 10,
          bbox: [90, 410, 505, 650],
          role: "FIGURE",
          precision: "EXACT_BLOCK",
        },
      ];

    const overlays = requirementTreeFocusOverlays({
      selectedNode: selected,
      contextNode: selected,
      pageSnapshot,
      pdfPage: 10,
      focusRegions,
    });

    expect(overlays).toHaveLength(5);
    expect(overlays.every((overlay) => overlay.focus)).toBe(true);
    expect(overlays.every((overlay) => overlay.selected)).toBe(true);
    expect(overlays.map((overlay) => overlay.id)).not.toContain("TXT-001");
    expect(overlays.map((overlay) => overlay.label)).toEqual([
      "条款正文",
      "",
      "表题",
      "条款表格",
      "条款图片",
    ]);
  });

  it("filters multi-page focus regions to the current PDF page", () => {
    const selected = node({
      page_end: 11,
    });
    const focusRegions = [
        {
          id: "page-10",
          page_number: 10,
          bbox: [70, 210, 540, 224],
          role: "CLAUSE_TEXT",
          precision: "EXACT_LINE",
        },
        {
          id: "page-11",
          page_number: 11,
          bbox: [70, 80, 540, 94],
          role: "CLAUSE_TEXT",
          precision: "EXACT_LINE",
        },
      ];

    expect(
      requirementTreeFocusOverlays({
        selectedNode: selected,
        contextNode: selected,
        pageSnapshot,
        pdfPage: 11,
        focusRegions,
      }).map((overlay) => overlay.id),
    ).toEqual(["page-11"]);
  });

  it("renders only the on-demand geometry response", () => {
    const selected = node({});

    const overlays = requirementTreeFocusOverlays({
      selectedNode: selected,
      contextNode: selected,
      pageSnapshot,
      pdfPage: 10,
      focusRegions: [
        {
          id: "on-demand-line",
          page_number: 10,
          bbox: [70, 210, 540, 224],
          role: "CLAUSE_TEXT",
          precision: "EXACT_LINE",
        },
      ],
    });

    expect(overlays.map((overlay) => overlay.id)).toEqual(["on-demand-line"]);
    expect(overlays[0].precision).toBe("EXACT_LINE");
  });

  it("keeps an authoritative empty result empty instead of guessing a fallback", () => {
    const selected = node({
      source_refs: [
        {
          source_id: "legacy-block",
          page_number: 10,
          bbox: [70, 80, 540, 250],
        },
      ],
    });

    expect(
      requirementTreeFocusOverlays({
        selectedNode: selected,
        contextNode: selected,
        pageSnapshot,
        pdfPage: 10,
        focusRegions: [],
      }),
    ).toEqual([]);
  });

  it("suppresses overlays until the matching page bounds arrive", () => {
    expect(
      requirementTreeFocusOverlays({
        selectedNode: node({}),
        contextNode: null,
        pageSnapshot: { ...pageSnapshot, bounds: [] },
        pdfPage: 10,
        focusRegions: [
          {
            id: "line-before-page-facts",
            page_number: 10,
            bbox: [70, 210, 540, 224],
          },
        ],
      }),
    ).toEqual([]);
  });

  it("drops stale regions while no tree node is selected", () => {
    expect(
      requirementTreeFocusOverlays({
        selectedNode: null,
        contextNode: null,
        pageSnapshot,
        pdfPage: 10,
        focusRegions: [
          {
            id: "stale-line",
            page_number: 10,
            bbox: [70, 210, 540, 224],
            role: "CLAUSE_TEXT",
            precision: "EXACT_LINE",
          },
        ],
      }),
    ).toEqual([]);
  });
});
