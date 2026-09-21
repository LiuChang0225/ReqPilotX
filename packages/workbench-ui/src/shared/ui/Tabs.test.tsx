import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { Tabs } from "./Tabs";

function Harness() {
  const [value, setValue] = useState("source");
  return (
    <Tabs
      label="详情"
      value={value}
      onChange={setValue}
      items={[
        { value: "source", label: "来源" },
        { value: "decision", label: "判定" },
      ]}
    />
  );
}

describe("Tabs", () => {
  it("moves selection and roving focus with the keyboard", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const source = screen.getByRole("tab", { name: "来源" });
    const decision = screen.getByRole("tab", { name: "判定" });

    source.focus();
    await user.keyboard("{ArrowRight}");

    expect(source).toHaveAttribute("aria-selected", "false");
    expect(decision).toHaveAttribute("aria-selected", "true");
    expect(decision).toHaveFocus();
  });
});
