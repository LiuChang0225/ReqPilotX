import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { KnowledgeGraphWorkbench } from "./KnowledgeGraphWorkbench";

afterEach(() => {
  cleanup();
});
beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function(this: HTMLDialogElement) { this.setAttribute("open", ""); } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function(this: HTMLDialogElement) { this.removeAttribute("open"); this.dispatchEvent(new Event("close")); } });
 window.history.replaceState(null, "", "/workspace?task=knowledge"); });

describe("KnowledgeGraphWorkbench Obsidian Pro Layout", () => {
  it("renders the 3-pane Obsidian layout with file tree, doc viewer, and inspector", () => {
    render(<KnowledgeGraphWorkbench />);

    expect(
      screen.getByText("领域知识库 · 合成装置示例")
    ).toBeInTheDocument();

    // Left tree
    expect(screen.getByTestId("knowledge-file-tree")).toBeInTheDocument();
    expect(screen.getByText("产品规格包线 (Specs)")).toBeInTheDocument();

    // Center doc viewer
    expect(screen.getByTestId("knowledge-doc-viewer")).toBeInTheDocument();

    // Right inspector
    expect(screen.getByTestId("knowledge-inspector")).toBeInTheDocument();
    expect(screen.getByText("局部关系图谱 (Local Graph)")).toBeInTheDocument();
  });

  it("supports switching view modes between doc, split and global graph", async () => {
    const user = userEvent.setup();
    render(<KnowledgeGraphWorkbench />);

    const graphTab = screen.getByRole("button", { name: "🕸️ 全景图谱" });
    await user.click(graphTab);

    // Global canvas active
    expect(screen.getByTestId("force-graph-canvas")).toBeInTheDocument();

    const docTab = screen.getByRole("button", { name: "📄 词条阅读" });
    await user.click(docTab);
    expect(screen.getByTestId("knowledge-doc-viewer")).toBeInTheDocument();
    expect(screen.queryByTestId("knowledge-inspector")).not.toBeInTheDocument();
    expect(docTab).toHaveAttribute("aria-pressed", "true");
  });

  it("displays source document citations in the doc viewer", () => {
    render(<KnowledgeGraphWorkbench />);

    // Default is specs/overview_architecture which has citations
    expect(
      screen.getByText("原始工程技术资料依据 (Source Evidence)")
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(/synthetic-startup-example\.md/)[0]
    ).toBeInTheDocument();
  });

  it("opens upload modal and dynamically grows knowledge base", async () => {
    const user = userEvent.setup();
    render(<KnowledgeGraphWorkbench />);

    const uploadBtn = screen.getByRole("button", { name: /添加预览资料/ });
    await user.click(uploadBtn);

    expect(screen.getByTestId("knowledge-upload-modal")).toBeInTheDocument();

    const titleInput = screen.getByPlaceholderText(/例如: PROJ-MOTOR-A/);
    fireEvent.change(titleInput, { target: { value: "新版技术规范补遗" } });

    fireEvent.change(screen.getByLabelText(/Markdown 正文/), { target: { value: "新增资料内容" } });
    expect(screen.getByText(/刷新后不会保留/)).toBeVisible();
    const ingestBtn = screen.getByRole("button", { name: /加入本次预览/ });
    await user.click(ingestBtn);

    expect(screen.queryByTestId("knowledge-upload-modal")).not.toBeInTheDocument();
    expect(screen.getByText(/^4 篇词条 ·/)).toBeInTheDocument();
    expect(screen.getByText("当前 4 篇词条")).toBeVisible();
    expect(screen.queryByText(/悬空死链/)).not.toBeInTheDocument();
  });

  it("reports empty search and exposes matching navigation as keyboard buttons", async () => {
    const user = userEvent.setup(); render(<KnowledgeGraphWorkbench />);
    const search = screen.getByRole("textbox", { name: "搜索当前知识库" });
    await user.type(search, "不存在的词条xyz");
    expect(screen.getByRole("status")).toHaveTextContent("没有匹配词条");
    await user.keyboard("{Escape}"); expect(search).toHaveValue("");
    await user.type(search, "启动");
    const results = screen.getAllByRole("button", { name: /^打开词条 / });
    expect(results.length).toBeGreaterThan(0);
    results[0].focus(); await user.keyboard("{Enter}"); expect(search).toHaveValue("");
  });

  it("strictly isolates spaces when switching to cus2 baseline", async () => {
    const user = userEvent.setup();
    render(<KnowledgeGraphWorkbench />);

    // Initially in irps baseline
    expect(screen.getByText("领域知识库 · 合成装置示例")).toBeInTheDocument();
    expect(screen.getByText(/^3 篇词条 ·/)).toBeInTheDocument();

    // Switch space to cus2
    const spaceSelect = screen.getByLabelText("知识库空间切换");
    await user.selectOptions(spaceSelect, "cus2");

    // Space title and counter cleanly updated to cus2
    expect(screen.getByText("领域知识库 · 合成控制器示例")).toBeInTheDocument();
    expect(screen.getByText(/^3 篇词条 ·/)).toBeInTheDocument();
    expect(screen.getByText("当前 3 篇词条")).toBeVisible();

    // cus2 documents are displayed in tree, viewer and inspector
    expect(
      screen.getAllByText(/控制器指示示例/)[0]
    ).toBeInTheDocument();

    // iRPS specific nodes are NOT present in file tree
    expect(screen.queryByText("启动指示示例")).not.toBeInTheDocument();
  });
});
