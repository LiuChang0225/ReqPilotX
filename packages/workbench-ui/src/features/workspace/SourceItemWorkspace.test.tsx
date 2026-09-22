import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourceItemWorkspace } from "./SourceItemWorkspace";
import { WorkspaceProvider, type WorkspaceContextValue } from "./WorkspaceContext";
import * as client from "../../shared/api/client";

vi.mock("../extraction/NativeStructureReferenceDock", () => ({ NativeStructureReferenceDock: (props: { purpose: string; sourceSha256: string }) => <div data-testid="native-ledger">{props.purpose}:{props.sourceSha256}</div> }));
const state = { baseline_sha256: "baseline", baseline_revision: 27, source_sha256: "source", source_item_count: 449,
  requirement_count: 341, non_requirement_count: 108, analysis_unit_count: 380, assessed_unit_count: 0, pending_unit_count: 380,
  decomposition: { records: { a: [{ requirement_display_id: "REQ-001-S01" }] } }, active_jobs: [], notices: [] };
function context() { return { route: { task: "analysis", mode: "organize", projectId: "p", documentId: "d", requirementSetId: "" },
  updateRoute: vi.fn(), selectedDocument: { source_sha256: "source" } } as unknown as WorkspaceContextValue; }
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("native ledger workspace entry", () => {
  it("opens analysis against the existing source ledger without requiring a duplicate confirmed set", async () => {
    vi.spyOn(client, "request").mockResolvedValue(state);
    const value = context();
    render(<WorkspaceProvider value={value}><SourceItemWorkspace task="analysis" fallback={<div>legacy empty</div>} /></WorkspaceProvider>);
    expect(await screen.findByTestId("native-ledger")).toHaveTextContent("analysis:source");
    expect(screen.queryByText("legacy empty")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "原文审阅" }));
    expect(value.updateRoute).toHaveBeenCalledWith({ task: "extraction", mode: "organize", checkpoint: "list" });
  });
  it("shows current working version and counts at the export entry", async () => {
    vi.spyOn(client, "request").mockResolvedValue(state);
    render(<WorkspaceProvider value={context()}><SourceItemWorkspace task="export" fallback={<div>legacy empty</div>} /></WorkspaceProvider>);
    expect(await screen.findByRole("heading", { name: "导出当前工作版" })).toBeInTheDocument();
    expect(screen.getByText("v27")).toBeInTheDocument();
    expect(screen.getByText("449 项")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出 Matrix 草稿" })).toBeEnabled();
  });
  it("preserves explicit confirmed-version routes and never masks service errors as no ledger", async () => {
    const request = vi.spyOn(client, "request").mockRejectedValue(new Error("连接暂不可用"));
    const value = context(); value.route.requirementSetId = "confirmed-set";
    const view = render(<WorkspaceProvider value={value}><SourceItemWorkspace task="analysis" fallback={<div>confirmed view</div>} /></WorkspaceProvider>);
    expect(screen.getByText("confirmed view")).toBeInTheDocument(); expect(request).not.toHaveBeenCalled();
    view.rerender(<WorkspaceProvider value={context()}><SourceItemWorkspace task="analysis" fallback={<div>confirmed view</div>} /></WorkspaceProvider>);
    expect(await screen.findByText("连接暂不可用")).toBeInTheDocument();
    expect(screen.queryByText("confirmed view")).not.toBeInTheDocument();
  });
});

it("does not replace a failed native read with a legacy workflow",async()=>{
  vi.spyOn(client,"request").mockRejectedValue(new client.ApiError("当前成果不存在",404,null));
  const value=context();
  render(<WorkspaceProvider value={value}><SourceItemWorkspace task="analysis" fallback={<div>unrelated legacy</div>}/></WorkspaceProvider>);
  expect(await screen.findByRole("heading",{name:"先整理原文条目"})).toBeVisible();expect(screen.queryByText("unrelated legacy")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"前往需求提取"}));
  expect(value.updateRoute).toHaveBeenCalledWith({task:"extraction",mode:"organize",checkpoint:"structure"});
});
it("gives an ungenerated ledger a recovery path without hiding other conflicts",async()=>{
  const request=vi.spyOn(client,"request").mockRejectedValue(new client.ApiError("原文条目尚未生成",409,null));
  const view=render(<WorkspaceProvider value={context()}><SourceItemWorkspace task="export" fallback={<div>unrelated legacy</div>}/></WorkspaceProvider>);
  expect(await screen.findByText(/还没有可导出的原文工作版/)).toBeVisible();
  request.mockRejectedValue(new client.ApiError("工作版版本冲突",409,null));
  fireEvent.click(screen.getByRole("button",{name:"同步已有成果"}));
  expect(await screen.findByText("工作版版本冲突")).toBeVisible();
  expect(screen.queryByRole("heading",{name:"先整理原文条目"})).not.toBeInTheDocument();
});
