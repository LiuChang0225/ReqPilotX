import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ReadingHandoffAction } from "./ReadingHandoffAction";
import type { ReadingFinding, ReadingHandoff } from "../workspace/readingHandoff";
const effects = vi.hoisted(() => ({ intent: vi.fn(), navigate: vi.fn() }));
vi.mock("../workspace/readingHandoff", () => ({ storeReadingHandoffIntent: effects.intent }));
vi.mock("../../shared/navigation", () => ({ useNavigate: () => effects.navigate }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const finding = { kind: "message", value: { status: "ACTIVE" } } as ReadingFinding;
const target = { url: "/workspace?resultRevision=v2" };
function handoff(prepare = vi.fn(() => "完整来源和发现")) { return { prepare, target, reason: "", number: 2, historical: true, durable: true } as unknown as ReadingHandoff; }
it("prepares a reviewable draft first, then stores an open intent only on the explicit open action", async () => {
  const user = userEvent.setup(), current = handoff(); render(<ReadingHandoffAction finding={finding} handoff={current} />);
  await user.click(screen.getByRole("button", { name: "带到工作台草稿" }));
  expect(current.prepare).toHaveBeenCalledWith(finding);
  expect(screen.getByText(/历史查看版，保持只读/)).toBeVisible();
  expect(effects.intent).not.toHaveBeenCalled(); expect(effects.navigate).not.toHaveBeenCalled();
  await user.click(screen.getByText("查看已准备内容")); expect(screen.getByText("完整来源和发现")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "打开已准备草稿" }));
  expect(effects.intent).toHaveBeenCalledWith(target); expect(effects.navigate).toHaveBeenCalledWith(target.url);
});
it("keeps a conflict visible without navigating or writing an open intent", async () => {
  render(<ReadingHandoffAction finding={finding} handoff={handoff(vi.fn(() => { throw new Error("已有草稿，未覆盖"); }))} />);
  await userEvent.setup().click(screen.getByRole("button", { name: "带到工作台草稿" }));
  expect(screen.getByRole("alert")).toHaveTextContent("未覆盖");
  expect(screen.queryByRole("button", { name: "打开已准备草稿" })).not.toBeInTheDocument(); expect(effects.intent).not.toHaveBeenCalled();
});
