import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RequirementTreeStageRail } from "./RequirementTreeStageRail";

afterEach(cleanup);

describe("RequirementTreeStageRail", () => {
  it("shows three compact extraction lenses without a second numbered analysis workflow", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <RequirementTreeStageRail
        value="structure"
        reviewAvailable={false}
        onChange={onChange}
      />,
    );

    expect(
      screen.getByRole("tab", {
        name: "原文结构",
      }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByRole("tab", { name: "原文条目" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("tab", { name: "需求清单" }),
    ).toBeEnabled();
    expect(screen.queryByRole("tab", { name: /AI 分析/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(screen.queryByRole("tab", { name: /来源核对/ })).not.toBeInTheDocument();
    const review = screen.getByRole("button", {
      name: "版本确认（可选）",
    });
    expect(review).toBeDisabled();

    await user.click(review);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("navigates between document views without changing data itself", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <RequirementTreeStageRail
        value="structure"
        reviewAvailable={false}
        onChange={onChange}
      />,
    );

    await user.click(
      screen.getByRole("tab", { name: "原文条目" }),
    );
    expect(onChange).toHaveBeenCalledWith("list");
    await user.click(
      screen.getByRole("tab", { name: "需求清单" }),
    );
    expect(onChange).toHaveBeenCalledWith("requirements");
    expect(screen.queryByRole("tab", { name: /AI 分析/ })).not.toBeInTheDocument();
  });

  it("enters optional version confirmation when a review version exists", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <RequirementTreeStageRail
        value="list"
        reviewAvailable
        onChange={onChange}
      />,
    );

    await user.click(
      screen.getByRole("button", {
        name: "版本确认（可选）",
      }),
    );
    expect(onChange).toHaveBeenCalledWith("review");
  });

  it("disables item-based views until saved source items exist", () => {
    render(
      <RequirementTreeStageRail
        value="structure"
        reviewAvailable={false}
        itemsAvailable={false}
        onChange={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("tab", { name: "原文条目" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("tab", { name: "需求清单" }),
    ).toBeDisabled();
    expect(screen.queryByRole("tab", { name: /AI 分析/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(3);
  });

  it("projects legacy diagnosis routes onto original structure", () => {
    render(
      <RequirementTreeStageRail
        value="diagnose"
        reviewAvailable={false}
        onChange={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("tab", { name: "原文结构" }),
    ).toHaveAttribute("aria-selected", "true");
  });
});
