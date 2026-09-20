import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RequirementTreeStageRail } from "./RequirementTreeStageRail";

afterEach(cleanup);

describe("RequirementTreeStageRail", () => {
  it("shows the three product checkpoints and keeps confirmation unavailable until a review version exists", async () => {
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
        name: "第 1 步：原文结构",
      }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByRole("tab", { name: "第 2 步：原文需求清单" }),
    ).toBeEnabled();
    expect(screen.queryByRole("tab", { name: /来源核对/ })).not.toBeInTheDocument();
    const review = screen.getByRole("tab", {
      name: "第 3 步：版本确认（可选）；锁定版本后可用",
    });
    expect(review).toBeDisabled();

    await user.click(review);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("navigates between working checkpoints without changing data itself", async () => {
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
      screen.getByRole("tab", { name: "第 2 步：原文需求清单" }),
    );
    expect(onChange).toHaveBeenCalledWith("list");
  });

  it("enters confirmation when a review version exists", async () => {
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
      screen.getByRole("tab", {
        name: "第 3 步：版本确认（可选）",
      }),
    );
    expect(onChange).toHaveBeenCalledWith("review");
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
      screen.getByRole("tab", { name: "第 1 步：原文结构" }),
    ).toHaveAttribute("aria-selected", "true");
  });
});
