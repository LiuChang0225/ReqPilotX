import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../shared/api/client";
import type {
  ProjectProcessingProfile,
  RuntimeWorkerFleetStatusV1,
} from "../../shared/api/contracts";
import {
  WorkspaceProvider,
  type WorkspaceContextValue,
} from "../workspace/WorkspaceContext";
import { SettingsPanel } from "./SettingsPanel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

beforeEach(() => {
  vi.spyOn(api, "codexReadiness").mockResolvedValue({
    schema_version: "codex-runtime-readiness/v1",
    ready: true,
    runtime_id: "codex-app-server",
    model_name: "gpt-5.6-sol",
    configuration_source: "CODEX_RUNTIME",
    reason_code: null,
  });
  vi.spyOn(api, "workerFleet").mockResolvedValue(workerFleetPayload());
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
    llm_provider: "codex",
    knowledge_backend: "none",
    revision: 1,
    canonical_sha256: "a".repeat(64),
    ...patch,
  };
}

function workerFleetPayload(): RuntimeWorkerFleetStatusV1 {
  return {
    schema_version: "runtime-worker-fleet-status/v1",
    state: "READY",
    desired_connection_revision: 1,
    required_kind: "document.extract",
    revision_required: true,
    registered_worker_count: 1,
    live_worker_count: 1,
    eligible_worker_count: 1,
    idle_worker_count: 1,
    active_worker_count: 0,
    draining_worker_count: 0,
    stale_revision_worker_count: 0,
    workers: [
      {
        slot: "1",
        claim_profile: "general",
        accepted_kinds: ["document.extract"],
        connection_revision: 1,
        state: "IDLE",
        fresh: true,
        revision_match: true,
        claim_profile_match: true,
        eligible: true,
        last_seen_age_seconds: 2,
        source_revision_match: true,
        runtime_instance_match: true,
      },
    ],
  };
}

function workspaceValue(
  patch: Partial<WorkspaceContextValue> = {},
): WorkspaceContextValue {
  const noop = async () => {};
  return {
    route: {
      task: "projects",
      mode: "organize",
      projectId: "project-1",
      documentId: "",
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
    aiSetup: { connection_revision: 1 },
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
    ...patch,
  };
}

function renderPanel(patch: Partial<WorkspaceContextValue> = {}) {
  render(
    <WorkspaceProvider value={workspaceValue(patch)}>
      <SettingsPanel />
    </WorkspaceProvider>,
  );
}

describe("SettingsPanel Codex automatic runtime", () => {
  it("ends the loading message after failure and can retry", async () => {
    vi.mocked(api.codexReadiness).mockRejectedValueOnce(new Error("offline"));
    renderPanel();
    expect(await screen.findByText("Codex 状态尚未取得，可刷新状态重试。")).toBeVisible();
    expect(screen.queryByText("正在核验 ChatGPT 登录和目标模型…")).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "刷新状态" }));
    expect(await screen.findByText("已登录")).toBeVisible();
  });
  it("does not classify missing runtime or project data as a fault or legacy configuration", async () => {
    renderPanel({ selectedProject: null, processingProfile: null, readiness: null });
    await screen.findByText("已登录");
    expect(screen.getByText("本地核心状态未取得")).toBeVisible();
    expect(screen.queryByText("本地核心异常")).not.toBeInTheDocument();
    expect(screen.queryByText("旧自定义方案")).not.toBeInTheDocument();
  });
  it("shows one Codex path and no provider configuration controls", async () => {
    renderPanel();

    expect(await screen.findByText("已登录")).toBeInTheDocument();
    expect(screen.getByText("reqpilotx-document-reading")).toBeInTheDocument();
    expect(screen.getByText("reqpilotx-pdf-understanding")).toBeInTheDocument();
    expect(screen.getByText("reqpilotx-structure-tree-generation")).toBeInTheDocument();
    expect(screen.getByText("reqpilotx-requirement-recognition")).toBeInTheDocument();
    expect(screen.getByText("reqpilotx-requirement-decomposition")).toBeInTheDocument();
    expect(screen.getByText("自动方案")).toBeInTheDocument();
    expect(screen.queryByLabelText(/API Key/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });
  it("names a new project's capture-only profile without calling it a legacy provider", async () => {
    renderPanel({ processingProfile: profile({ llm_provider: "none", revision: 0 }) });
    await screen.findByText("已登录");
    expect(screen.getByText("仅原文捕获")).toBeVisible();
    expect(screen.getByRole("button", { name: "启用 Agent 自动方案" })).toBeEnabled();
    expect(screen.queryByText("旧自定义方案")).not.toBeInTheDocument();
  });

  it("reports Codex login as the recovery action", async () => {
    vi.mocked(api.codexReadiness).mockResolvedValue({
      schema_version: "codex-runtime-readiness/v1",
      ready: false,
      runtime_id: "codex-app-server",
      model_name: "gpt-5.6-sol",
      configuration_source: "CODEX_RUNTIME",
      reason_code: "reading_codex_auth_required",
    });
    renderPanel();

    expect(await screen.findByText(/Codex 尚未登录/)).toBeInTheDocument();
    expect(screen.getByText("需要处理")).toBeInTheDocument();
  });

  it("can replace a legacy project profile with the automatic path", async () => {
    const save = vi.spyOn(api, "saveProjectProcessingProfile").mockResolvedValue({
      profile: profile(),
    });
    const refreshProject = vi.fn().mockResolvedValue(undefined);
    renderPanel({
      processingProfile: profile({
        llm_provider: "aliyun",
        revision: 4,
      }),
      refreshProject,
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "恢复自动方案" }));

    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({
        expected_revision: 4,
        llm_provider: "codex",
        knowledge_backend: "none",
      }),
    );
    expect(refreshProject).toHaveBeenCalled();
  });
});
