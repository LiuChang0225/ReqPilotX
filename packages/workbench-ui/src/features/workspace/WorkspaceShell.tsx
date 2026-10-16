import {
  readExtractionCheckpoint,
  readViewedWork,
  rememberExtractionCheckpoint,
  rememberViewedWork,
  type ViewedWork,
} from "./documentWorkSelection";
import {
  type PropsWithChildren,
  useEffect,
  useRef,
  useState,
} from "react";
import { usePreferences } from "../../app/preferences";
import { isSaasAccountMode } from "../../shared/api/saasSession";
import { AppLink } from "../../shared/navigation";
import { cx } from "../../shared/lib/format";
import { knowledgeGate } from "../../shared/runtime/processingCapabilities";
import { Brand } from "../../shared/ui/Brand";
import { Dialog } from "../../shared/ui/Dialog";
import { Icon, type IconName } from "../../shared/ui/Icon";
import { SettingsPanel } from "../settings/SettingsPanel";
import { useWorkspace } from "./WorkspaceContext";
import type { WorkspaceTask } from "./route";

const primaryNavigation: Array<{
  task: WorkspaceTask;
  label: string;
  description: string;
  icon: IconName;
}> = [
  {
    task: "projects",
    label: "项目管理",
    description: "项目、来源与知识依据",
    icon: "projects",
  },
  {
    task: "reading",
    label: "AI 辅助阅读",
    description: "单文档阅读与结构地图",
    icon: "eye",
  },
  {
    task: "extraction",
    label: "需求提取",
    description: "结构整理与清单确认",
    icon: "tree",
  },
  {
    task: "analysis",
    label: "需求分析",
    description: "证据、建议与结论",
    icon: "analysis",
  },
  {
    task: "knowledge",
    label: "领域知识库",
    description: "项目依据与参考资料",
    icon: "knowledge",
  },
  {
    task: "export",
    label: "矩阵导出",
    description: "版本、比较与下载",
    icon: "matrix",
  },
];

const taskTitles: Record<WorkspaceTask, string> = {
  projects: "项目管理",
  reading: "AI 辅助阅读",
  extraction: "需求提取",
  analysis: "需求分析",
  knowledge: "领域知识库",
  export: "矩阵导出",
};

const SIDEBAR_PREFERENCE_KEY = "reqpilotx:workspace-sidebar:v2";
const SIDEBAR_RAIL_BREAKPOINT = 1300;

function initialSidebarMode(): "expanded" | "compact" {
  try {
    const stored = window.localStorage.getItem(SIDEBAR_PREFERENCE_KEY);
    if (stored === "expanded" || stored === "compact") return stored;
  } catch {
    // Local preference is optional.
  }
  return window.innerWidth >= 1280 ? "expanded" : "compact";
}

export function WorkspaceShell({ children }: PropsWithChildren) {
  const {
    route,
    updateRoute,
    selectedProject,
    selectedDocument,
    documents,
    workspace,
    readiness,
    runtimeCapabilities,
    processingProfile,
    refreshing,
    refreshDocument,
    notice,
  } = useWorkspace();
  const { theme, toggleTheme } = usePreferences();
  const [sidebarMode, setSidebarMode] = useState(initialSidebarMode);
  const [railViewport, setRailViewport] = useState(
    () => window.innerWidth < SIDEBAR_RAIL_BREAKPOINT,
  );
  const [sidebarOverlayOpen, setSidebarOverlayOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const sidebarRef = useRef<HTMLElement>(null);
  const sidebarToggleRef = useRef<HTMLButtonElement>(null);
  const requirementCount = workspace?.requirements?.length || 0;
  const analyzedCount =
    workspace?.requirements?.filter(
      (item) =>
        item.analysis_status &&
        !["PENDING", "QUEUED"].includes(String(item.analysis_status)),
    ).length || 0;
  const counts: Partial<Record<WorkspaceTask, number>> = {
    projects: documents.length,
    extraction: requirementCount,
    analysis: analyzedCount,
  };
  const currentKnowledgeGate = knowledgeGate(
    processingProfile,
    runtimeCapabilities,
  );
  const knowledgeStatusLabel = !selectedProject
    ? "选择项目查看能力"
    : !processingProfile
      ? "正在读取项目能力"
      : processingProfile.knowledge_backend === "none"
        ? "语义检索未启用"
        : currentKnowledgeGate.ready
          ? "知识检索已连接"
          : "知识检索待连接";

  useEffect(() => {
    try {
      window.localStorage.setItem(SIDEBAR_PREFERENCE_KEY, sidebarMode);
    } catch {
      // The preference is convenience state, never a requirement for use.
    }
  }, [sidebarMode]);

  useEffect(() => {
    const listener = () => {
      const nextRailViewport = window.innerWidth < SIDEBAR_RAIL_BREAKPOINT;
      setRailViewport(nextRailViewport);
      if (!nextRailViewport) setSidebarOverlayOpen(false);
    };
    window.addEventListener("resize", listener);
    return () => window.removeEventListener("resize", listener);
  }, []);

  useEffect(() => {
    if (!sidebarOverlayOpen) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const sidebar = sidebarRef.current;
    sidebar
      ?.querySelector<HTMLElement>(
        "button:not(:disabled), a[href], [tabindex]:not([tabindex='-1'])",
      )
      ?.focus();
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setSidebarOverlayOpen(false);
        return;
      }
      if (event.key !== "Tab" || !sidebar) return;
      const focusable = Array.from(
        sidebar.querySelectorAll<HTMLElement>(
          "button:not(:disabled), a[href], [tabindex]:not([tabindex='-1'])",
        ),
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1) || first;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", listener);
    return () => {
      window.removeEventListener("keydown", listener);
      document.body.style.overflow = previousBodyOverflow;
      (previouslyFocused || sidebarToggleRef.current)?.focus();
    };
  }, [sidebarOverlayOpen]);

  const toggleSidebar = () => {
    if (railViewport) {
      setSidebarOverlayOpen((open) => !open);
      return;
    }
    setSidebarMode((mode) => (mode === "expanded" ? "compact" : "expanded"));
  };

  const isDocumentTask = ["extraction", "analysis", "export"].includes(route.task);
  const sourceSha256 = selectedDocument?.id === route.documentId
    ? String(selectedDocument.source_sha256 || "") : "";
  const viewedWork: ViewedWork = route.pluginSessionId
    ? { producer: "EXTERNAL_PLUGIN", sessionId: route.pluginSessionId, revisionId: route.resultRevision }
    : { producer: "NATIVE" };

  useEffect(() => {
    if (!isDocumentTask || route.requirementSetId || route.reviewCase || !sourceSha256) return;
    // An explicit native URL wins immediately, even while its summary is still loading.
    if (!route.pluginSessionId) rememberViewedWork(route.projectId, route.documentId, sourceSha256, { producer: "NATIVE" });
    if (route.task === "extraction" && route.mode === "organize" && route.checkpoint && route.checkpoint !== "review") {
      rememberExtractionCheckpoint(route.projectId, route.documentId, sourceSha256, viewedWork, route.checkpoint);
    }
  }, [isDocumentTask, route.task, route.mode, route.checkpoint, route.projectId, route.documentId, route.pluginSessionId, route.resultRevision, route.requirementSetId, route.reviewCase, sourceSha256]);

  const chooseTask = (task: WorkspaceTask) => {
    if (task === route.task) {
      if (railViewport) setSidebarOverlayOpen(false);
      return;
    }
    const canRestore = !isDocumentTask && !route.requirementSetId && !route.reviewCase && ["extraction", "analysis", "export"].includes(task);
    const saved = canRestore ? readViewedWork(route.projectId, route.documentId, sourceSha256) : null;
    const nextWork = saved || viewedWork;
    const checkpoint = task === "extraction" && route.mode === "organize" && !route.requirementSetId && !route.reviewCase
      ? readExtractionCheckpoint(route.projectId, route.documentId, sourceSha256, nextWork) : undefined;
    updateRoute({
      task,
      ...(saved?.producer === "EXTERNAL_PLUGIN" ? { pluginSessionId: saved.sessionId, resultRevision: saved.revisionId } : {}),
      ...(task === "extraction" ? { mode: route.mode, ...(checkpoint ? { checkpoint } : {}) } : {}),
    });
    if (railViewport) setSidebarOverlayOpen(false);
  };

  return (
    <div
      className={cx(
        "workspace-app",
        `is-sidebar-${railViewport ? "compact" : sidebarMode}`,
        sidebarOverlayOpen && "is-sidebar-overlay-open",
      )}
    >
      <a className="skip-link" href="#workspace-content">
        跳到主工作区
      </a>
      <aside ref={sidebarRef} className="workspace-sidebar">
        <div className="workspace-brand">
          <div className="workspace-brand-lockup">
            <Brand compact />
          </div>
          <button
            ref={sidebarToggleRef}
            className="sidebar-toggle"
            type="button"
            aria-label={
              railViewport
                ? sidebarOverlayOpen
                  ? "关闭工作台导航"
                  : "打开工作台导航"
                : sidebarMode === "expanded"
                  ? "收起工作台导航"
                  : "展开工作台导航"
            }
            aria-expanded={
              railViewport ? sidebarOverlayOpen : sidebarMode === "expanded"
            }
            onClick={toggleSidebar}
          >
            <Icon
              name={
                railViewport
                  ? sidebarOverlayOpen
                    ? "panel-close"
                    : "panel-open"
                  : sidebarMode === "expanded"
                    ? "panel-close"
                    : "panel-open"
              }
            />
          </button>
        </div>

        <button
          aria-label={`当前项目：${selectedProject?.name || "未选择"}`}
          className="project-context"
          type="button"
          onClick={() => chooseTask("projects")}
        >
          <small>当前项目</small>
          <strong>{selectedProject?.name || "选择项目"}</strong>
          <span>{selectedDocument?.file_name || "尚未选择客户来源"}</span>
        </button>

        <nav className="workspace-navigation" aria-label="工作台导航">
          <span className="nav-group-label">工作空间</span>
          {primaryNavigation.filter(item => !isSaasAccountMode() || item.task !== "knowledge").map((item) => {
            const active = route.task === item.task;
            const count = counts[item.task];
            return (
              <button
                aria-label={item.label}
                aria-current={active ? "page" : undefined}
                title={`${item.label} · ${item.description}`}
                key={item.task}
                type="button"
                className={cx("nav-item", active && "is-active")}
                onClick={() => chooseTask(item.task)}
              >
                <Icon name={item.icon} size={19} />
                <span>
                  <strong>{item.label}</strong>
                  <small>{item.description}</small>
                </span>
                {Boolean(count) && <em>{count}</em>}
              </button>
            );
          })}
        </nav>

        <div className="sidebar-runtime">
          <span
            className={cx(
              "runtime-indicator",
              (route.reviewCase || readiness?.runtime_ready) && "is-ready",
            )}
          />
          <div>
            <strong>
              {route.reviewCase ? "本地原文核对" : readiness?.runtime_ready ? "本地核心就绪" : "检查基础服务"}
            </strong>
            <small>
              {route.reviewCase ? "候选与反馈分别保留" : knowledgeStatusLabel}
            </small>
          </div>
        </div>
      </aside>

      {sidebarOverlayOpen && (
        <button
          className="sidebar-backdrop"
          type="button"
          aria-label="关闭工作台导航"
          onClick={() => setSidebarOverlayOpen(false)}
        />
      )}

      <section className="workspace-body">
        <header className="workspace-topbar">
          <div className="workspace-context-strip">
            <div className="workspace-breadcrumb">
              <span>{selectedProject?.name || "ReqPilotX"}</span>
              <i>/</i>
              <strong>{taskTitles[route.task]}</strong>
            </div>
          </div>
          <div className="workspace-toolbar">
            {notice && (
              <span className="workspace-notice" role="status" aria-live="polite">
                {notice}
              </span>
            )}
            <button
              className="button ghost compact workspace-sync-button"
              type="button"
              aria-label="同步当前文档的最新状态，不会重新提取"
              title="同步最新状态；不会重新提取或修改结构"
              onClick={() => void refreshDocument()}
              disabled={refreshing}
            >
              <Icon
                name="refresh"
                className={refreshing ? "is-spinning" : ""}
              />
              <span>{refreshing ? "同步中" : "同步"}</span>
            </button>
            {!isSaasAccountMode() && <button
              className="icon-button"
              type="button"
              aria-label="管理员设置"
              title="管理员设置"
              onClick={() => setSettingsOpen(true)}
            >
              <Icon name="settings" />
            </button>}
            <button
              className="icon-button"
              type="button"
              aria-label="切换主题"
              onClick={toggleTheme}
            >
              <Icon name={theme === "light" ? "moon" : "sun"} />
            </button>
            <AppLink
              className="icon-button"
              to="/home"
              target="_blank"
              aria-label="打开产品说明"
              title="产品说明"
            >
              <Icon name="home" />
            </AppLink>
          </div>
        </header>
        <main id="workspace-content" className="workspace-content" tabIndex={-1}>
          {children}
        </main>
      </section>

      <Dialog
        open={settingsOpen}
        title="管理员设置"
        className="settings-dialog"
        onClose={() => setSettingsOpen(false)}
      >
        <header className="dialog-command-header">
          <div>
            <p className="section-label">可选能力</p>
            <h2>管理员设置</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭设置"
            onClick={() => setSettingsOpen(false)}
          >
            <Icon name="close" />
          </button>
        </header>
        {route.reviewCase ? <p>当前入口用于核对已生成的原文候选；运行配置在常规项目入口管理。</p> : <SettingsPanel />}
      </Dialog>
    </div>
  );
}
