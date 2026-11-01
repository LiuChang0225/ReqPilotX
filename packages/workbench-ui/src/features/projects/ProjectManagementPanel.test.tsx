import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ProjectProcessingProfile,
  RequirementSet,
} from "../../shared/api/contracts";
import {
  WorkspaceProvider,
  type WorkspaceContextValue,
} from "../workspace/WorkspaceContext";
import { ProjectManagementPanel } from "./ProjectManagementPanel";
import { api } from "../../shared/api/client";
import { WorkbenchHostContext } from "../workspace/WorkbenchHostContext";

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function profile(
  patch: Partial<ProjectProcessingProfile> = {},
): ProjectProcessingProfile {
  return {
    schema_version: "project-processing-profile/v1",
    project_id: "project-1",
    document: {
      primary_parser: "pymupdf",
      enhancement_chain: [],
      complex_vision: null,
      auto_escalation: true,
    },
    llm_provider: "none",
    knowledge_backend: "none",
    revision: 1,
    canonical_sha256: "a".repeat(64),
    ...patch,
  };
}

function workspaceValue(
  requirementSets: RequirementSet[] = [],
  extractionStatus = "FAILED",
): WorkspaceContextValue {
  const noop = async () => {};
  return {
    route: {
      task: "projects",
      mode: "review",
      projectId: "project-1",
      documentId: "document-1",
      requirementSetId: "stale-set",
    },
    updateRoute: vi.fn(),
    projects: [{ id: "project-1", name: "正式项目", customer_name: "客户 A" }],
    documents: [
      {
        id: "document-1",
        project_id: "project-1",
        file_name: "正式规范.pdf",
        file_type: "pdf",
      },
    ],
    knowledge: [],
    workspace: null,
    requirementSets: {
      active_requirement_set_id: requirementSets[0]?.id || "",
      requirement_sets: requirementSets,
    },
    extraction: {
      project_id: "project-1",
      document_id: "document-1",
      job: {
        status: extractionStatus,
        started_at: "2026-08-09T00:00:00Z",
        finished_at: "2026-08-09T00:01:00Z",
      },
    },
    structure: null,
    structureOptimization: null,
    aiStatus: null,
    exports: [],
    readiness: {
      ready: true,
      runtime_ready: true,
      status: "READY",
      checks: [],
    },
    aiSetup: null,
    runtimeCapabilities: [],
    processingProfile: profile(),
    loading: false,
    refreshing: false,
    error: null,
    notice: "",
    busyAction: "",
    selectedProject: {
      id: "project-1",
      name: "正式项目",
      customer_name: "客户 A",
    },
    selectedDocument: {
      id: "document-1",
      project_id: "project-1",
      file_name: "正式规范.pdf",
      file_type: "pdf",
    },
    refreshAll: noop,
    refreshProject: noop,
    refreshDocument: noop,
    refreshExtraction: noop,
    refreshStructure: noop,
    refreshStructureOptimization: noop,
    refreshAi: noop,
    refreshExports: noop,
    runAction: async (_label, action) => action(),
    setNotice: vi.fn(),
  };
}

function renderPanel(
  requirementSets: RequirementSet[] = [],
  extractionStatus = "FAILED",
) {
  const value = workspaceValue(requirementSets, extractionStatus);
  render(
    <WorkspaceProvider value={value}>
      <ProjectManagementPanel />
    </WorkspaceProvider>,
  );
  return value;
}

describe("ProjectManagementPanel workflow truth", () => {
  it("routes standalone source registration into native project evidence without legacy indexing", () => {
    const value = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "添加知识依据" }));
    expect(value.updateRoute).toHaveBeenCalledWith({ task: "knowledge", knowledgeSpace: "project", knowledgePage: undefined, knowledgeView: undefined });
  });

  it("preserves legacy file selection for an external presentation that omits the native flag", () => {
    const value = workspaceValue();
    const { container } = render(<WorkbenchHostContext.Provider value={{ readOnly: false, resourceUrl: path => path, pdfViewerUrl: () => "" }}><WorkspaceProvider value={value}><ProjectManagementPanel /></WorkspaceProvider></WorkbenchHostContext.Provider>);
    const click = vi.spyOn(container.querySelectorAll('input[type="file"]')[1] as HTMLInputElement, "click");
    fireEvent.click(screen.getByRole("button", { name: "添加知识依据" }));
    expect(click).toHaveBeenCalledTimes(1);
    expect(value.updateRoute).not.toHaveBeenCalled();
  });
  it("creates the first project without a selected project", async () => {
    const value = workspaceValue();
    value.projects = []; value.documents = []; value.selectedProject = null;
    value.route = { ...value.route, projectId: "", documentId: "" };
    const create = vi.spyOn(api, "createProject").mockResolvedValue({ id: "new-project" } as never);
    render(<WorkspaceProvider value={value}><ProjectManagementPanel /></WorkspaceProvider>);
    expect(screen.getByRole("button", { name: "添加客户来源" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "创建项目" }));
    fireEvent.change(screen.getByRole("textbox", { name: "项目名称" }), { target: { value: " 新项目 " } });
    fireEvent.change(screen.getByRole("textbox", { name: "客户名称" }), { target: { value: " 客户 B " } });
    fireEvent.click(screen.getByRole("button", { name: "创建并进入" }));
    await waitFor(() => expect(create).toHaveBeenCalledWith("新项目", "客户 B"));
    expect(value.updateRoute).toHaveBeenCalledWith(expect.objectContaining({ projectId: "new-project", documentId: "", requirementSetId: "" }), { replace: true });
  });

  it("keeps confirmed project creation separate from a failed list refresh", async () => {
    const value = workspaceValue();
    value.refreshAll = vi.fn().mockRejectedValue(Error("list unavailable"));
    const runAction = vi.spyOn(value, "runAction");
    const create = vi.spyOn(api, "createProject").mockResolvedValue({ id: "saved-project" } as never);
    render(<WorkspaceProvider value={value}><ProjectManagementPanel /></WorkspaceProvider>);
    fireEvent.click(screen.getByRole("button", { name: "新建项目" }));
    fireEvent.change(screen.getByRole("textbox", { name: "项目名称" }), { target: { value: "新项目" } });
    fireEvent.change(screen.getByRole("textbox", { name: "客户名称" }), { target: { value: "客户" } });
    fireEvent.click(screen.getByRole("button", { name: "创建并进入" }));
    await waitFor(() => expect(value.refreshAll).toHaveBeenCalledTimes(1));
    expect(create).toHaveBeenCalledTimes(1);
    expect(runAction).toHaveBeenCalledWith("创建项目", expect.any(Function), { refresh: "none", scope: "project" });
    expect(value.updateRoute).toHaveBeenCalledWith(expect.objectContaining({ projectId: "saved-project" }), { replace: true });
    expect(value.setNotice).toHaveBeenCalledWith(expect.stringContaining("项目已保存"));
    expect(screen.queryByRole("textbox", { name: "项目名称" })).not.toBeInTheDocument();
  });

  it("restores saved pending creation on reload before refreshing the list", async () => {
    const value = workspaceValue();
    const intent = { commandId: "pending", kind: "project.create" as const, scope: "", payload: { name: "Pending", customer_name: "Customer" } };
    vi.spyOn(api, "pendingEntryCommands").mockReturnValue([intent]);
    vi.spyOn(api, "recoverPendingEntryCommands").mockResolvedValue({ recovered: [{ intent, result: { id: "recovered", name: "Pending", customer_name: "Customer" } }], failures: [] });
    value.refreshAll = vi.fn().mockRejectedValue(Error("list unavailable"));
    render(<WorkspaceProvider value={value}><ProjectManagementPanel /></WorkspaceProvider>);
    await waitFor(() => expect(value.updateRoute).toHaveBeenCalledWith(expect.objectContaining({ projectId: "recovered" }), { replace: true }));
    expect(screen.queryByRole("textbox", { name: "项目名称" })).not.toBeInTheDocument();
  });

  it("keeps project selection reachable when no project is selected", () => {
    const value = workspaceValue(); value.selectedProject = null;
    render(<WorkspaceProvider value={value}><ProjectManagementPanel /></WorkspaceProvider>);
    fireEvent.click(screen.getByText("选择项目", { selector: "summary" }));
    expect(screen.getByRole("searchbox", { name: "搜索项目" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: "正式项目 客户 A" }));
    expect(value.updateRoute).toHaveBeenCalledWith(expect.objectContaining({ projectId: "project-1", documentId: "" }));
  });

  it("preserves the selected document and removal dialog when deletion fails", async () => {
    const value = workspaceValue(); value.runAction = vi.fn().mockResolvedValue(null);
    render(<WorkspaceProvider value={value}><ProjectManagementPanel /></WorkspaceProvider>);
    fireEvent.click(screen.getByRole("button", { name: "移除 正式规范.pdf" }));
    fireEvent.click(screen.getByRole("button", { name: "确认移除" }));
    await waitFor(() => expect(value.runAction).toHaveBeenCalled());
    expect(value.updateRoute).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "移除来源" })).toBeInTheDocument();
  });

  it("selects the next document only after a successful deletion", async () => {
    const value = workspaceValue();
    value.documents.push({ id: "document-2", project_id: "project-1", file_name: "下一份.pdf" });
    vi.spyOn(api, "deleteSorDocument").mockResolvedValue({ deleted: true } as never);
    render(<WorkspaceProvider value={value}><ProjectManagementPanel /></WorkspaceProvider>);
    fireEvent.click(screen.getByRole("button", { name: "移除 正式规范.pdf" }));
    fireEvent.click(screen.getByRole("button", { name: "确认移除" }));
    await waitFor(() => expect(value.updateRoute).toHaveBeenCalledWith({ documentId: "document-2", requirementSetId: "" }, { replace: true }));
  });

  it("does not let a late deletion change the document in another project", async () => {
    const value = workspaceValue(); let finish!: (value: unknown) => void;
    value.runAction = vi.fn(() => new Promise(resolve => { finish = resolve; })) as WorkspaceContextValue["runAction"];
    const view = render(<WorkspaceProvider value={value}><ProjectManagementPanel /></WorkspaceProvider>);
    fireEvent.click(screen.getByRole("button", { name: "移除 正式规范.pdf" }));
    fireEvent.click(screen.getByRole("button", { name: "确认移除" }));
    const other = { ...value, route: { ...value.route, projectId: "project-2", documentId: "document-2" } };
    view.rerender(<WorkspaceProvider value={other}><ProjectManagementPanel /></WorkspaceProvider>);
    await act(async () => { finish({ deleted: true }); });
    expect(value.updateRoute).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "移除来源" })).not.toBeInTheDocument();
  });

  it("keeps a successfully uploaded file saved when list refresh fails", async () => {
    const value = workspaceValue(); value.refreshProject = vi.fn().mockRejectedValue(new Error("offline"));
    vi.spyOn(api, "uploadSorDocument").mockResolvedValue({ id: "uploaded" } as never);
    const { container } = render(<WorkspaceProvider value={value}><ProjectManagementPanel /></WorkspaceProvider>);
    fireEvent.change(container.querySelectorAll('input[type="file"]')[0], { target: { files: [new File(["source"], "new.pdf")] } });
    await screen.findByText(/已保存 1 份/);
    expect(screen.queryByText("上传失败")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    await waitFor(() => expect(value.setNotice).toHaveBeenCalledWith(expect.stringContaining("new.pdf 已保存，文档列表暂未更新")));
  });

  it("allows the same source file to be added to a different project", async () => {
    const value = workspaceValue();
    const upload = vi.spyOn(api, "uploadSorDocument").mockResolvedValue({ id: "uploaded" } as never);
    const file = new File(["source"], "shared.pdf", { lastModified: 1 });
    const view = render(<WorkspaceProvider value={value}><ProjectManagementPanel /></WorkspaceProvider>);
    fireEvent.change(view.container.querySelectorAll('input[type="file"]')[0], { target: { files: [file] } });
    await screen.findByText(/已保存 1 份/);
    const other = { ...value, route: { ...value.route, projectId: "project-2" } };
    view.rerender(<WorkspaceProvider value={other}><ProjectManagementPanel /></WorkspaceProvider>);
    fireEvent.change(view.container.querySelectorAll('input[type="file"]')[0], { target: { files: [file] } });
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
    expect(upload.mock.calls.map(call => call[0])).toEqual(["project-1", "project-2"]);
  });

  it("does not start the old project's next queued upload after a project switch", async () => {
    const value = workspaceValue();
    const upload = vi.spyOn(api, "uploadSorDocument").mockImplementation((_project, _file, options) => new Promise((_resolve, reject) => {
      options?.signal?.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")));
    }));
    const view = render(<WorkspaceProvider value={value}><ProjectManagementPanel /></WorkspaceProvider>);
    fireEvent.change(view.container.querySelectorAll('input[type="file"]')[0], { target: { files: [new File(["1"], "one.pdf"), new File(["2"], "two.pdf")] } });
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
    const other = { ...value, route: { ...value.route, projectId: "project-2" } };
    view.rerender(<WorkspaceProvider value={other}><ProjectManagementPanel /></WorkspaceProvider>);
    await waitFor(() => expect(screen.getAllByText(/已取消/)).toHaveLength(2));
    expect(upload).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getAllByRole("button", { name: "重试" })[0]);
    expect(value.setNotice).toHaveBeenCalledWith("请先切回该文件所属项目，再重试上传。");
    expect(upload).toHaveBeenCalledTimes(1);
  });
});
