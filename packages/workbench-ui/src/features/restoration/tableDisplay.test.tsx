import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { tableDisplayAuthority, tableDisplayTitle } from "./tableDisplay";
import { StructuredTable } from "./StructuredTable";
import { normalizeStructuredTable } from "./restorationModel";

afterEach(cleanup);

describe("source table display metadata", () => {
  it("keeps unannotated Map titles unchanged and uses the shared explicit label", () => {
    expect(tableDisplayTitle({ title: "Existing Map label" })).toBe("Existing Map label");
    expect(tableDisplayAuthority({ title: "Existing Map label" })).toBeUndefined();
    const raw = { title: "Existing Map label", agent_display_label: "合成模块测试清单" };
    const preview = { title: raw.title, presentation: { display_title: raw.agent_display_label, authority: "AGENT_DISPLAY_LABEL", map_title: raw.title } };
    expect(tableDisplayTitle(raw)).toBe("合成模块测试清单");
    expect(tableDisplayTitle(preview)).toBe(tableDisplayTitle(raw));
    expect(tableDisplayAuthority(preview)).toBe("AGENT_DISPLAY_LABEL");
  });
  it("shows a concise AI source cue while the original table cells stay visible", () => {
    const table = normalizeStructuredTable({ id: "t", type: "TABLE", page: 1, row_count: 2, column_count: 2,
      cells: [{ id: "h1", row: 0, column: 0, text: "客户原文项目", header: true }, { id: "h2", row: 0, column: 1, text: "判据", header: true },
        { id: "v1", row: 1, column: 0, text: "开启试验" }, { id: "v2", row: 1, column: 1, text: "≤30 ms" }] });
    render(<StructuredTable table={table} tables={[table]} displayLabel="合成模块测试清单" displayLabelAuthority="AGENT_DISPLAY_LABEL"
      projectId="synthetic" documentId="source" sourceSha256={"a".repeat(64)} onPageChange={vi.fn()} />);
    expect(screen.getByText("合成模块测试清单")).toBeVisible();
    expect(screen.getByText(/显示名（AI）/)).toBeVisible();
    expect(screen.getByText("客户原文项目")).toBeVisible();
    expect(screen.getByText("≤30 ms")).toBeVisible();
  });
});
