import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { KnowledgeUploadModal } from "./KnowledgeUploadModal";

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function(this: HTMLDialogElement) { this.setAttribute("open", ""); } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function(this: HTMLDialogElement) { this.removeAttribute("open"); this.dispatchEvent(new Event("close")); } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("preserves input and recovers from a failed file read without claiming an upload", async () => {
  let reader: { onerror?: () => void; onload?: (event: { target: { result: string } }) => void; abort: () => void };
  vi.stubGlobal("FileReader", class { constructor() { reader = this; } onerror?: () => void; onload?: (event: { target: { result: string } }) => void; abort() {} readAsText() {} });
  const save = vi.fn(); render(<KnowledgeUploadModal isOpen onClose={vi.fn()} onIngestSuccess={save} />);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("词条/规范名称"), "原有名称");
  await user.type(screen.getByLabelText(/Markdown 正文/), "原有正文");
  fireEvent.change(screen.getByLabelText("选择 Markdown 或文本文件"), { target: { files: [new File(["data"], "new.md")] } });
  expect(screen.getByRole("button", { name: "加入本次预览" })).toBeDisabled();
  act(() => reader.onerror?.());
  expect(screen.getByRole("alert")).toHaveTextContent("文件读取失败");
  expect(screen.getByLabelText("词条/规范名称")).toHaveValue("原有名称");
  expect(screen.getByLabelText(/Markdown 正文/)).toHaveValue("原有正文");
  await user.click(screen.getByRole("button", { name: "加入本次预览" }));
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ name: "原有名称" }), [], "原有正文");
});

it("rejects unsupported files inline and does not accept empty entries", () => {
  const save = vi.fn(); render(<KnowledgeUploadModal isOpen onClose={vi.fn()} onIngestSuccess={save} />);
  fireEvent.change(screen.getByLabelText("词条/规范名称"), { target: { value: "只有标题" } });
  expect(screen.getByRole("button", { name: "加入本次预览" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("选择 Markdown 或文本文件"), { target: { files: [new File(["data"], "source.pdf")] } });
  expect(screen.getByRole("alert")).toHaveTextContent("支持 .md / .txt");
  expect(screen.getByText(/未上传或写入项目知识库/)).toBeVisible();
  expect(save).not.toHaveBeenCalled();
});
