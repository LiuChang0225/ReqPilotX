import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { NativeStructurePageUnitV1 } from "../../shared/api/contracts";
import { NativeStructurePageAtlas } from "./NativeStructurePageAtlas";

afterEach(cleanup);

describe("NativeStructurePageAtlas", () => {
  it("keeps long documents bounded by expanding only the active unit", async () => {
    const user = userEvent.setup();
    const units: NativeStructurePageUnitV1[] = [
      {
        unit_id: "unit-a",
        title: "主规范",
        pages: Array.from({ length: 30 }, (_, index) => index + 1),
        title_authority: "VISIBLE_TITLE",
        confidence: "HIGH",
      },
      {
        unit_id: "unit-b",
        title: "试验附件",
        pages: Array.from({ length: 31 }, (_, index) => index + 31),
        title_authority: "VISIBLE_TITLE",
        confidence: "HIGH",
      },
    ];

    render(
      <NativeStructurePageAtlas
        units={units}
        pageCount={61}
        receipts={[]}
        logicalTables={[]}
        unresolved={[]}
        proposals={[]}
        activePage={1}
        agentFocusPages={[]}
        onSelectPage={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByRole("button", { name: /^P1 ·/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /^P31 ·/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "展开" }));
    expect(screen.getByRole("button", { name: /^P31 ·/ })).toBeVisible();
  });
});
