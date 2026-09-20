import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../shared/api/client";
import type { WorkspaceRoute } from "./route";
import { WorkspacePage } from "./WorkspacePage";

const harness = vi.hoisted(() => ({
  route: {
    task: "projects",
    mode: "organize",
    projectId: "project-a",
    documentId: "document-a",
    requirementSetId: "set-a",
  } as WorkspaceRoute,
  update: vi.fn(),
  failingAction: vi.fn<() => Promise<unknown>>(),
  longAction: vi.fn<() => Promise<unknown>>(),
  newAction: vi.fn<() => Promise<unknown>>(),
}));

type IntervalCallback = () => void | Promise<void>;

const intervalHarness = vi.hoisted(() => ({
  nextId: 0,
  intervals: new Map<
    number,
    { delay: number; invoke: IntervalCallback }
  >(),
}));

vi.mock("../../shared/hooks/useInterval", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    useInterval(callback: IntervalCallback, delay: number | null) {
      const latest = React.useRef(callback);
      const intervalId = React.useRef(0);
      latest.current = callback;
      if (intervalId.current === 0) {
        intervalHarness.nextId += 1;
        intervalId.current = intervalHarness.nextId;
      }

      React.useEffect(() => {
        const currentId = intervalId.current;
        if (delay === null) {
          intervalHarness.intervals.delete(currentId);
          return;
        }
        intervalHarness.intervals.set(currentId, {
          delay,
          invoke: () => latest.current(),
        });
        return () => {
          intervalHarness.intervals.delete(currentId);
        };
      }, [delay]);
    },
  };
});

vi.mock("./route", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./route")>();
  return {
    ...actual,
    useWorkspaceRoute: () => ({ route: harness.route, update: harness.update }),
  };
});

vi.mock("./WorkspaceShell", () => ({
  WorkspaceShell: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("../projects/ProjectManagementPanel", async () => {
  const { useWorkspace } =
    await vi.importActual<typeof import("./WorkspaceContext")>(
      "./WorkspaceContext",
    );
  return {
    ProjectManagementPanel: () => {
      const workspace = useWorkspace();
      return (
        <div>
          <output data-testid="refreshing">{String(workspace.refreshing)}</output>
          <output data-testid="workspace-error">
            {workspace.error?.message || ""}
          </output>
          <output data-testid="workspace-notice">{workspace.notice}</output>
          <output data-testid="busy-action">{workspace.busyAction}</output>
          <button
            type="button"
            onClick={() =>
              void workspace.runAction(
                "旧失败动作",
                () => harness.failingAction(),
                { refresh: "none", scope: "document" },
              )
            }
          >
            触发失败动作
          </button>
          <button
            type="button"
            onClick={() =>
              void workspace.runAction(
                "旧长动作",
                () => harness.longAction(),
                { refresh: "none", scope: "document" },
              )
            }
          >
            启动旧长动作
          </button>
          <button
            type="button"
            onClick={() =>
              void workspace.runAction(
                "新动作",
                () => harness.newAction(),
                { refresh: "none", scope: "document" },
              )
            }
          >
            启动新动作
          </button>
        </div>
      );
    },
  };
});

vi.mock("../extraction/RequirementTreePanel", async () => {
  const { useWorkspace } =
    await vi.importActual<typeof import("./WorkspaceContext")>(
      "./WorkspaceContext",
    );
  return {
    RequirementTreePanel: () => {
      const workspace = useWorkspace();
      const progress = workspace.extraction?.job?.progress;
      return (
        <output data-testid="extraction-page-progress">
          {[
            Number(progress?.completed_pages || 0),
            Number(progress?.total_pages || 0),
            Number(progress?.current_page || 0),
          ].join("/")}
        </output>
      );
    },
  };
});
vi.mock("../reading/ReadingWorkspacePage", () => ({
  ReadingWorkspacePage: ({ embedded }: { embedded?: boolean }) => (
    <output data-testid="reading-workspace-embedded">{String(embedded)}</output>
  ),
}));
vi.mock("../matrix/RequirementMatrixWorkbench", () => ({
  RequirementMatrixWorkbench: () => null,
}));
vi.mock("../exports/ExportsPanel", () => ({ ExportsPanel: () => null }));

function resolvedApi() {
  vi.spyOn(api, "projects").mockResolvedValue([
    { id: "project-a", name: "A", customer_name: "Customer A" },
    { id: "project-b", name: "B", customer_name: "Customer B" },
  ]);
  vi.spyOn(api, "readiness").mockResolvedValue({} as never);
  vi.spyOn(api, "aiSetup").mockResolvedValue({} as never);
  vi.spyOn(api, "runtimeCapabilities").mockResolvedValue({
    schema_version: "runtime-capability-catalog/v1",
    connection_revision: 0,
    capabilities: [],
  });
  vi.spyOn(api, "sorDocuments").mockImplementation(async (projectId) => [
    {
      id: projectId === "project-b" ? "document-b" : "document-a",
      project_id: projectId,
      file_name: "source.pdf",
    },
  ]);
  vi.spyOn(api, "knowledgeDocuments").mockResolvedValue([]);
  vi.spyOn(api, "projectProcessingProfile").mockResolvedValue({
    profile: {
      schema_version: "project-processing-profile/v1",
      project_id: "project-a",
      document: {
        primary_parser: "pymupdf",
        enhancement_chain: [],
        complex_vision: null,
        auto_escalation: false,
      },
      llm_provider: "none",
      knowledge_backend: "none",
      revision: 1,
      canonical_sha256: "a".repeat(64),
    },
  });
  vi.spyOn(api, "workspace").mockResolvedValue({} as never);
  vi.spyOn(api, "requirementSets").mockResolvedValue({
    active_requirement_set_id: "set-a",
    requirement_sets: [],
  });
  vi.spyOn(api, "extractionStatus").mockResolvedValue({} as never);
  vi.spyOn(api, "structure").mockResolvedValue({} as never);
  vi.spyOn(api, "structureOptimizationStatus").mockResolvedValue({} as never);
}

function activeInterval(delay: number) {
  const matches = [...intervalHarness.intervals.values()].filter(
    (interval) => interval.delay === delay,
  );
  expect(matches).toHaveLength(1);
  return matches[0];
}

beforeEach(() => {
  harness.route = {
    task: "projects",
    mode: "organize",
    projectId: "project-a",
    documentId: "document-a",
    requirementSetId: "set-a",
  };
  harness.update.mockReset();
  harness.failingAction.mockReset();
  harness.longAction.mockReset();
  harness.newAction.mockReset();
  intervalHarness.nextId = 0;
  intervalHarness.intervals.clear();
  resolvedApi();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("WorkspacePage scope lifecycle", () => {
  it("renders reading inside the shared workspace for a document reading task", async () => {
    harness.route = { ...harness.route, task: "reading" };

    render(<WorkspacePage />);

    expect(
      await screen.findByTestId("reading-workspace-embedded"),
    ).toHaveTextContent("true");
  });

  it("renders newer page progress after each active extraction status poll", async () => {
    harness.route = { ...harness.route, task: "extraction" };
    let progress = {
      completed_pages: 0,
      total_pages: 3,
      current_page: 1,
    };
    vi.mocked(api.extractionStatus).mockImplementation(async () => ({
      job: { job_id: "job-a", status: "RUNNING", progress },
    }));
    render(<WorkspacePage />);

    await waitFor(() =>
      expect(screen.getByTestId("extraction-page-progress")).toHaveTextContent(
        "0/3/1",
      ),
    );
    const interval = activeInterval(2500);
    progress = {
      completed_pages: 1,
      total_pages: 3,
      current_page: 2,
    };

    await act(async () => {
      await interval.invoke();
    });

    expect(screen.getByTestId("extraction-page-progress")).toHaveTextContent(
      "1/3/2",
    );
  });

  it("does not let a slower status request get superseded by overlapping polls", async () => {
    harness.route = { ...harness.route, task: "extraction" };
    let deferPoll = false;
    let resolvePoll:
      | ((value: {
          job: {
            job_id: string;
            status: string;
            progress: {
              completed_pages: number;
              total_pages: number;
              current_page: number;
            };
          };
        }) => void)
      | undefined;
    vi.mocked(api.extractionStatus).mockImplementation(async () => {
      if (!deferPoll) {
        return {
          job: {
            job_id: "job-a",
            status: "RUNNING",
            progress: {
              completed_pages: 0,
              total_pages: 3,
              current_page: 1,
            },
          },
        };
      }
      return new Promise((resolve) => {
        resolvePoll = resolve;
      });
    });
    render(<WorkspacePage />);

    await waitFor(() =>
      expect(screen.getByTestId("extraction-page-progress")).toHaveTextContent(
        "0/3/1",
      ),
    );
    const interval = activeInterval(2500);
    deferPoll = true;
    const callsBeforePoll = vi.mocked(api.extractionStatus).mock.calls.length;

    const firstPoll = interval.invoke();
    await waitFor(() => expect(resolvePoll).toBeDefined());
    const overlappingPoll = interval.invoke();
    expect(api.extractionStatus).toHaveBeenCalledTimes(callsBeforePoll + 1);

    resolvePoll?.({
      job: {
        job_id: "job-a",
        status: "RUNNING",
        progress: {
          completed_pages: 1,
          total_pages: 3,
          current_page: 2,
        },
      },
    });
    await act(async () => {
      await Promise.all([firstPoll, overlappingPoll]);
    });

    expect(screen.getByTestId("extraction-page-progress")).toHaveTextContent(
      "1/3/2",
    );
  });

  it("polls only extraction status while an extraction job remains active", async () => {
    harness.route = { ...harness.route, task: "extraction" };
    vi.mocked(api.extractionStatus).mockResolvedValue({
      job: { job_id: "job-a", status: "RUNNING" },
    });
    render(<WorkspacePage />);

    await waitFor(() => expect(intervalHarness.intervals.size).toBe(1));
    const interval = activeInterval(2500);
    const extractionCalls = vi.mocked(api.extractionStatus).mock.calls.length;
    const structureCalls = vi.mocked(api.structure).mock.calls.length;

    await act(async () => {
      await interval.invoke();
    });

    expect(api.extractionStatus).toHaveBeenCalledTimes(extractionCalls + 1);
    expect(api.structure).toHaveBeenCalledTimes(structureCalls);
  });

  it("refreshes structure exactly once when the active extraction becomes terminal", async () => {
    harness.route = { ...harness.route, task: "extraction" };
    let status = "RUNNING";
    vi.mocked(api.extractionStatus).mockImplementation(async () => ({
      job: { job_id: "job-a", status },
    }));
    render(<WorkspacePage />);

    await waitFor(() => expect(intervalHarness.intervals.size).toBe(1));
    const interval = activeInterval(2500);
    const structureCalls = vi.mocked(api.structure).mock.calls.length;
    status = "COMPLETED";

    await act(async () => {
      await interval.invoke();
    });

    await waitFor(() =>
      expect(api.structure).toHaveBeenCalledTimes(structureCalls + 1),
    );
    await waitFor(() => expect(intervalHarness.intervals.size).toBe(0));
    expect(api.structure).toHaveBeenCalledTimes(structureCalls + 1);
  });

  it("does not refresh structure from a late extraction poll after switching document scope", async () => {
    harness.route = { ...harness.route, task: "extraction" };
    let deferProjectAPoll = false;
    let resolveProjectAPoll:
      | ((value: { job: { job_id: string; status: string } }) => void)
      | undefined;
    vi.mocked(api.extractionStatus).mockImplementation(
      async (projectId) => {
        if (projectId === "project-b") {
          return { job: { job_id: "job-b", status: "COMPLETED" } };
        }
        if (!deferProjectAPoll) {
          return { job: { job_id: "job-a", status: "RUNNING" } };
        }
        return new Promise((resolve) => {
          resolveProjectAPoll = resolve;
        });
      },
    );
    const view = render(<WorkspacePage />);

    await waitFor(() => expect(intervalHarness.intervals.size).toBe(1));
    const interval = activeInterval(2500);
    deferProjectAPoll = true;
    const latePoll = interval.invoke();
    await waitFor(() => expect(resolveProjectAPoll).toBeDefined());

    harness.route = {
      ...harness.route,
      projectId: "project-b",
      documentId: "document-b",
      requirementSetId: "set-b",
    };
    view.rerender(<WorkspacePage />);
    await waitFor(() =>
      expect(api.extractionStatus).toHaveBeenCalledWith(
        "project-b",
        "document-b",
      ),
    );
    await waitFor(() => expect(intervalHarness.intervals.size).toBe(0));
    const structureCalls = vi.mocked(api.structure).mock.calls.length;

    resolveProjectAPoll?.({
      job: { job_id: "job-a", status: "COMPLETED" },
    });
    await act(async () => {
      await latePoll;
    });

    expect(api.structure).toHaveBeenCalledTimes(structureCalls);
  });

  it("clears old notice and error when the project/document scope becomes empty", async () => {
    harness.failingAction.mockRejectedValue(new Error("旧范围错误"));
    const user = userEvent.setup();
    const view = render(<WorkspacePage />);

    await user.click(
      await screen.findByRole("button", { name: "触发失败动作" }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("workspace-error")).toHaveTextContent(
        "旧范围错误",
      ),
    );
    expect(screen.getByTestId("workspace-notice")).toHaveTextContent(
      "旧失败动作未完成",
    );

    harness.route = {
      ...harness.route,
      projectId: "",
      documentId: "",
      requirementSetId: "",
    };
    view.rerender(<WorkspacePage />);

    await waitFor(() =>
      expect(screen.getByTestId("workspace-error")).toHaveTextContent(""),
    );
    expect(screen.getByTestId("workspace-notice")).toBeEmptyDOMElement();
  });

  it("resets a stale document refresh when the scope becomes empty", async () => {
    let resolveWorkspace: ((value: unknown) => void) | undefined;
    vi.mocked(api.workspace).mockReturnValue(
      new Promise((resolve) => {
        resolveWorkspace = resolve;
      }) as never,
    );
    const view = render(<WorkspacePage />);

    await waitFor(() =>
      expect(screen.getByTestId("refreshing")).toHaveTextContent("true"),
    );

    harness.route = {
      ...harness.route,
      projectId: "",
      documentId: "",
      requirementSetId: "",
    };
    view.rerender(<WorkspacePage />);

    await waitFor(() =>
      expect(screen.getByTestId("refreshing")).toHaveTextContent("false"),
    );

    await act(async () => {
      resolveWorkspace?.({});
    });
  });

  it("lets a new scope start work while the old action is pending and ignores the late failure", async () => {
    let rejectOldAction: ((reason: Error) => void) | undefined;
    harness.longAction.mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectOldAction = reject;
      }),
    );
    harness.newAction.mockResolvedValue("new-result");
    const user = userEvent.setup();
    const view = render(<WorkspacePage />);

    await user.click(
      await screen.findByRole("button", { name: "启动旧长动作" }),
    );
    expect(screen.getByTestId("busy-action")).toHaveTextContent("旧长动作");

    harness.route = {
      ...harness.route,
      projectId: "project-b",
      documentId: "document-b",
      requirementSetId: "set-b",
    };
    view.rerender(<WorkspacePage />);

    await waitFor(() =>
      expect(screen.getByTestId("busy-action")).toBeEmptyDOMElement(),
    );
    await user.click(screen.getByRole("button", { name: "启动新动作" }));
    await waitFor(() => expect(harness.newAction).toHaveBeenCalledOnce());
    expect(screen.getByTestId("workspace-notice")).toHaveTextContent(
      "新动作已完成",
    );

    await act(async () => {
      rejectOldAction?.(new Error("旧动作迟到失败"));
    });

    expect(screen.getByTestId("workspace-error")).toBeEmptyDOMElement();
    expect(screen.getByTestId("workspace-notice")).toHaveTextContent(
      "新动作已完成",
    );
  });
});

describe("unavailable direct workspace entries", () => {
  it("keeps an explicit missing project instead of redirecting a plugin entry to another source", async () => {
    harness.route = { ...harness.route, task: "extraction", projectId: "missing-project", requirementSetId: "", pluginSessionId: "plugin-" + "a".repeat(32), resultRevision: "b".repeat(64) };
    render(<WorkspacePage />);
    expect(await screen.findByRole("heading", { name: "当前入口不可用" })).toBeInTheDocument();
    expect(screen.getByText(/链接中的项目不存在/)).toBeInTheDocument();
    expect(harness.update).not.toHaveBeenCalled();
    expect(screen.queryByTestId("extraction-page-progress")).not.toBeInTheDocument();
  });

  it("keeps a missing document explicit and offers a deliberate recovery to project management", async () => {
    harness.route = { ...harness.route, task: "extraction", documentId: "missing-document", requirementSetId: "", pluginSessionId: "plugin-" + "a".repeat(32), resultRevision: "b".repeat(64) };
    render(<WorkspacePage />);
    expect(await screen.findByRole("heading", { name: "当前入口不可用" })).toBeInTheDocument();
    expect(screen.getByText(/链接中的文档不在当前项目中/)).toBeInTheDocument();
    expect(harness.update).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "返回项目管理" }));
    expect(harness.update).toHaveBeenCalledWith({ task: "projects", projectId: "project-a", documentId: "", requirementSetId: "", pluginSessionId: undefined, resultRevision: undefined, reviewCase: undefined });
  });

  it("still selects a default document only when the route did not specify one", async () => {
    harness.route = { ...harness.route, documentId: "", requirementSetId: "" };
    render(<WorkspacePage />);
    await waitFor(() => expect(harness.update).toHaveBeenCalledWith({ documentId: "document-a", requirementSetId: "" }, { replace: true }));
  });
});


it("does not default an incomplete external link to the first native source", async () => {
  harness.route = { ...harness.route, task: "extraction", projectId: "", documentId: "", requirementSetId: "", pluginSessionId: "plugin-" + "a".repeat(32) };
  render(<WorkspacePage />);
  expect(await screen.findByRole("heading", { name: "当前入口不可用" })).toBeInTheDocument();
  await waitFor(() => expect(api.projects).toHaveBeenCalled());
  expect(screen.getByText(/插件成果链接缺少项目或文档信息/)).toBeInTheDocument();
  expect(harness.update).not.toHaveBeenCalled();
});
