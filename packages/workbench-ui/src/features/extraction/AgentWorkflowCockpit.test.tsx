import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentWorkflowCockpit } from "./AgentWorkflowCockpit";

afterEach(cleanup);

describe("AgentWorkflowCockpit", () => {
  it("shows auditable workflow state and only the controls supplied by the live workflow", async () => {
    const user = userEvent.setup();
    const terminate = vi.fn();
    render(
      <AgentWorkflowCockpit
        goal="将这份文档转化为可审核的需求基线"
        statusLabel="Agent 正在运行"
        statusTone="working"
        steps={[
          { id: "evidence", label: "核对文件与页面证据", state: "complete" },
          { id: "structure", label: "建立原文结构", state: "current" },
          { id: "baseline", label: "确认需求基线", state: "pending" },
        ]}
        currentAction="正在核对 Section 5.3 的父条目边界"
        findings={["第 39 页编号从 5.3.4 跳到 5.3.6"]}
        controls={[
          {
            id: "terminate",
            label: "终止本次解析",
            tone: "danger",
            onClick: terminate,
          },
        ]}
      />,
    );

    const cockpit = screen.getByRole("region", { name: "Agent 工作流驾驶舱" });
    expect(cockpit).toHaveTextContent("将这份文档转化为可审核的需求基线");
    expect(cockpit).toHaveTextContent("正在核对 Section 5.3 的父条目边界");
    expect(cockpit).toHaveTextContent("第 39 页编号从 5.3.4 跳到 5.3.6");
    expect(screen.queryByRole("button", { name: "暂停" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "跳过" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "终止本次解析" }));
    expect(terminate).toHaveBeenCalledTimes(1);
  });

  it("keeps a compact status row visible while details are collapsed", async () => {
    const user = userEvent.setup();
    render(
      <AgentWorkflowCockpit
        goal="形成需求基线"
        statusLabel="等待人工核对"
        statusTone="ready"
        steps={[]}
        currentAction="等待确认"
        findings={["暂无异常"]}
        controls={[]}
      />,
    );

    await user.click(screen.getByRole("button", { name: /收起/ }));
    expect(screen.getByText("Agent 工作流")).toBeVisible();
    expect(screen.getByText("等待人工核对")).toBeVisible();
    expect(screen.getByText("形成需求基线")).not.toBeVisible();
    expect(screen.getByRole("button", { name: /展开/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});
