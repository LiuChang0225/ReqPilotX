import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { KnowledgeDocViewer } from "./KnowledgeDocViewer";
import { renderKnowledgeInline } from "./knowledgeInline";

afterEach(cleanup);

it("renders emphasis and literal code in paragraphs, lists and tables while preserving wiki navigation", async () => {
  const navigate = vi.fn();
  const page = "# **工程说明**\n\n段落 **关键条件** 与 `value < 5`。\n- **列表条件** 与 `TBD`\n\n| **参数** | 依据 |\n| --- | --- |\n| **供电** `5 V` | [[specs/voltage|**电压规范**]] |\n| `A|B` | 原文空白保持 |\n| | 保留第二列 |";
  const { container } = render(<KnowledgeDocViewer nodeId="sample" pageContents={{ sample: page }} citationsMap={{}} onNavigateToNode={navigate} />);
  for (const label of ["工程说明", "关键条件", "列表条件", "参数", "供电", "电压规范"]) expect(screen.getByText(label).tagName).toBe("STRONG");
  for (const code of ["value < 5", "TBD", "5 V", "A|B"]) expect(screen.getByText(code).tagName).toBe("CODE");
  const rows = within(screen.getByRole("table")).getAllByRole("row");
  expect(rows).toHaveLength(4);
  expect(within(rows[2]).getAllByRole("cell")).toHaveLength(2);
  expect(within(rows[3]).getAllByRole("cell")[0]).toHaveTextContent("");
  await userEvent.setup().click(screen.getByRole("button", { name: /电压规范/ }));
  expect(navigate).toHaveBeenCalledWith("specs/voltage");
  expect(container.textContent).not.toContain("**关键条件**");
});

it("does not interpret HTML or wiki syntax inside code and preserves unmatched delimiters", () => {
  const link = vi.fn((_target, label, key) => <button key={key}>{label}</button>);
  const text = "`[[secret]] **literal**` <img src=x onerror=alert(1)> **unfinished \\*escaped\\*";
  const { container } = render(<p>{renderKnowledgeInline(text, link)}</p>);
  expect(screen.getByText("[[secret]] **literal**").tagName).toBe("CODE");
  expect(container.querySelector("img")).toBeNull();
  expect(container.textContent).toContain("<img src=x onerror=alert(1)> **unfinished *escaped*");
  expect(link).not.toHaveBeenCalled();
});
