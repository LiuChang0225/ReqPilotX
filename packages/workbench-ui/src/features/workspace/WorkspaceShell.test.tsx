import userEvent from "@testing-library/user-event";
import { readExtractionCheckpoint, readViewedWork, rememberExtractionCheckpoint, rememberViewedWork } from "./documentWorkSelection";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProjectProcessingProfile } from "../../shared/api/contracts";
import {
  WorkspaceProvider,
  type WorkspaceContextValue,
} from "./WorkspaceContext";
import { WorkspaceShell } from "./WorkspaceShell";

vi.mock("../../app/preferences", () => ({
  usePreferences: () => ({
    theme: "light",
    toggleTheme: vi.fn(),
    t: (key: string) => key,
  }),
}));

vi.mock("../settings/SettingsPanel", () => ({ SettingsPanel: () => <div data-testid="settings-panel">连接设置内容</div> }));

beforeEach(() => {
  window.history.replaceState({}, "", "/workspace?task=projects");
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
});

function profile(): ProjectProcessingProfile {
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
  };
}

function workspaceValue(): WorkspaceContextValue {
  const noop = async () => {};
  return {
    route: {
      task: "projects",
      mode: "organize",
      projectId: "project-1",
      documentId: "document-1",
      requirementSetId: "",
    },
    updateRoute: vi.fn(),
    projects: [],
    documents: [],
    knowledge: [],
    workspace: null,
    requirementSets: null,
    extraction: null,
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
    selectedDocument: null,
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

function renderShell() {
  render(
    <WorkspaceProvider value={workspaceValue()}>
      <WorkspaceShell>
        <p>内容</p>
      </WorkspaceShell>
    </WorkspaceProvider>,
  );
}

describe("WorkspaceShell navigation and capability truth", () => {
  it("mounts connection settings only while the dialog is open", async () => {
    renderShell();
    expect(screen.queryByTestId("settings-panel")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "打开 Agent 设置" }));
    expect(await screen.findByTestId("settings-panel")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "关闭设置" }));
    expect(screen.queryByTestId("settings-panel")).not.toBeInTheDocument();
  });

  it("opens settings from the home destination and removes the setting intent when closed", async () => {
    window.history.replaceState({}, "", "/workspace?task=projects&settings=agent");
    renderShell();
    expect(await screen.findByTestId("settings-panel")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "关闭设置" }));
    await waitFor(() => expect(window.location.search).toBe("?task=projects"));
    expect(screen.queryByTestId("settings-panel")).not.toBeInTheDocument();
  });
  it("exposes the active primary destination to assistive technology", () => {
    renderShell();

    expect(screen.getByRole("button", { name: "项目管理" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(
      screen.getByRole("button", { name: "需求整理" }),
    ).not.toHaveAttribute("aria-current");
    expect(
      screen.getByRole("button", { name: "AI 辅助阅读" }),
    ).not.toHaveAttribute("aria-current");
  });

  it("uses the selected project knowledge gate instead of global readiness", () => {
    renderShell();

    expect(screen.getByText("语义检索未启用")).toBeInTheDocument();
    expect(screen.queryByText("增强能力已连接")).not.toBeInTheDocument();
  });
});

describe("document task return navigation", () => {
  const sessionId = "plugin-" + "a".repeat(32);
  const revisionId = "b".repeat(64);
  const source = "c".repeat(64);
  const plugin = { producer: "EXTERNAL_PLUGIN" as const, sessionId, revisionId };
  const native = { producer: "NATIVE" as const };
  const scoped = () => ({ ...workspaceValue(), selectedDocument: { id: "document-1", project_id: "project-1", file_name: "source.pdf", source_sha256: source } });
  const show = (value: WorkspaceContextValue) => render(<WorkspaceProvider value={value}><WorkspaceShell><p>内容</p></WorkspaceShell></WorkspaceProvider>);

  it("returns from analysis to the pinned plugin ledger checkpoint", async () => {
    const value = scoped();
    rememberExtractionCheckpoint("project-1", "document-1", source, plugin, "list");
    value.route = { ...value.route, task: "analysis", pluginSessionId: sessionId, resultRevision: revisionId };
    show(value);
    await userEvent.click(screen.getByRole("button", { name: "需求整理" }));
    expect(value.updateRoute).toHaveBeenCalledWith({ task: "extraction", mode: "organize", checkpoint: "list" });
  });

  it("restores the producer and viewed version after returning through projects", async () => {
    const value = scoped();
    rememberViewedWork("project-1", "document-1", source, plugin);
    rememberExtractionCheckpoint("project-1", "document-1", source, plugin, "list");
    show(value);
    await userEvent.click(screen.getByRole("button", { name: "需求整理" }));
    expect(value.updateRoute).toHaveBeenCalledWith({ task: "extraction", mode: "organize", checkpoint: "list", pluginSessionId: sessionId, resultRevision: revisionId });
  });

  it("never replaces an explicit native task with a stale plugin preference", async () => {
    const value = scoped();
    value.route = { ...value.route, task: "analysis" };
    rememberViewedWork("project-1", "document-1", source, plugin);
    rememberExtractionCheckpoint("project-1", "document-1", source, native, "list");
    show(value);
    await userEvent.click(screen.getByRole("button", { name: "需求整理" }));
    expect(value.updateRoute).toHaveBeenCalledWith({ task: "extraction", mode: "organize", checkpoint: "list" });
    expect(readViewedWork("project-1", "document-1", source)).toEqual(native);
  });

  it("does not borrow a ledger checkpoint from another external version", async () => {
    const value = scoped();
    value.route = { ...value.route, task: "export", pluginSessionId: sessionId, resultRevision: "d".repeat(64) };
    rememberExtractionCheckpoint("project-1", "document-1", source, plugin, "list");
    show(value);
    await userEvent.click(screen.getByRole("button", { name: "需求整理" }));
    expect(value.updateRoute).toHaveBeenCalledWith({ task: "extraction", mode: "organize" });
  });

  it("remembers the current stage before leaving extraction and ignores reselecting the active task", async () => {
    const value = scoped();
    value.route = { ...value.route, task: "extraction", checkpoint: "list", pluginSessionId: sessionId, resultRevision: revisionId };
    show(value);
    expect(readExtractionCheckpoint("project-1", "document-1", source, plugin)).toBe("list");
    await userEvent.click(screen.getByRole("button", { name: "需求整理" }));
    expect(value.updateRoute).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "AI 辅助阅读" }));
    expect(value.updateRoute).toHaveBeenCalledWith({ task: "reading" });
  });

  it("does not recover plugin choices from a replaced source document", async () => {
    const value = scoped();
    value.selectedDocument = { ...value.selectedDocument!, source_sha256: "d".repeat(64) };
    rememberViewedWork("project-1", "document-1", source, plugin);
    show(value);
    await userEvent.click(screen.getByRole("button", { name: "需求整理" }));
    expect(value.updateRoute).toHaveBeenCalledWith({ task: "extraction", mode: "organize" });
  });
});
