import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourceReviewToolbar } from "./SourceReviewToolbar";

afterEach(cleanup);
const props = {
  query: "", onQuery: vi.fn(), view: "ALL" as const, onView: vi.fn(), category: "", onCategory: vi.fn(),
  topic: "", onTopic: vi.fn(), more: true, onMore: vi.fn(), total: 10, matched: 10,
  focus: 0, attention: 0, changed: 0, ready: true, historyKnown: false,
  error: "", onRetry: vi.fn(), onExpand: vi.fn(), onCollapse: vi.fn(), onLocate: vi.fn(),
  onChooseFirst: vi.fn(), inputRef: { current: null },
};

describe("review focus coverage", () => {
  it("does not present an unavailable annotation as zero technical priorities", () => {
    render(<SourceReviewToolbar {...props} focusStatus="UNAVAILABLE" />);
    expect(screen.getByRole("button", { name: "重点 —" })).toBeDisabled();
    expect(screen.getByText("当前版本尚无重点标记，不能据此判断没有技术重点。")).toBeVisible();
    expect(screen.getByRole("option", { name: "技术参数" })).toBeDisabled();
  });
  it("keeps partial priorities usable while disclosing the unreviewed remainder", () => {
    render(<SourceReviewToolbar {...props} focus={3} focusStatus="PARTIAL" />);
    expect(screen.getByRole("button", { name: "重点 3" })).toBeEnabled();
    expect(screen.getByText("重点提示仅覆盖部分原文；未标记内容仍需审阅。")).toBeVisible();
  });
});
