import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { UploadQueueDialog } from "./UploadQueueDialog";
import { HostLocaleProvider } from "../../host/HostLocaleContext";
import type { UploadQueueItem } from "./uploadQueue";

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
});

it("localizes the upload queue actions and progress names while keeping the filename intact", () => {
  const file = new File(["fixture"], "客户原件.pdf", { type: "application/pdf" });
  const item: UploadQueueItem = { id: "u1", projectId: "p", kind: "sor", file, status: "uploading", progress: 40, error: "" };
  const onCancel = vi.fn();
  render(<HostLocaleProvider t={key => key === "workbench.locale" ? "en" : key}>
    <UploadQueueDialog open items={[item]} currentProjectId="p" onClose={vi.fn()} onCancel={onCancel}
      onRetry={vi.fn()} onRemove={vi.fn()} onClearFinished={vi.fn()} />
  </HostLocaleProvider>);
  expect(screen.getByRole("heading", { name: "1 file in this batch" })).toBeVisible();
  expect(screen.getByLabelText("客户原件.pdf upload progress")).toBeVisible();
  expect(screen.getByText(/Uploading/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onCancel).toHaveBeenCalledWith("u1");
  expect(screen.getByRole("button", { name: "Continue in background" })).toBeVisible();
  expect(screen.queryByText("正在上传")).not.toBeInTheDocument();
});
