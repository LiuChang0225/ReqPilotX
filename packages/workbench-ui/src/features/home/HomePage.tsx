import { useCallback, useEffect } from "react";
import { api, request } from "../../shared/api/client";
import type { JsonRecord, Project, SorDocument } from "../../shared/api/contracts";
import { useAsync } from "../../shared/hooks/useAsync";
import { AppLink } from "../../shared/navigation";
import { Brand } from "../../shared/ui/Brand";
import { Icon } from "../../shared/ui/Icon";
import { StatusBadge } from "../../shared/ui/Feedback";
import { useApplicationHost } from "../../host/ApplicationHost";
import { useUiCopy, useUiLocale, type UiTranslate } from "../../host/HostLocaleContext";
import "./home.css";

const workspacePath = "/workspace?task=projects";
const settingsPath = `${workspacePath}&settings=agent`;
const loadProjects = () => api.projects();
const loadReadiness = () => api.readiness();
const loadAgentReadiness = () => request<JsonRecord>("/api/runtime/agent-readiness");

function createdDate(value: { created_at?: string }, locale: "zh" | "en"): string {
  const timestamp = Date.parse(value.created_at || "");
  return Number.isFinite(timestamp)
    ? new Intl.DateTimeFormat(locale === "en" ? "en-US" : "zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(timestamp)
    : "—";
}

function documentPath(projectId: string, documentId: string, entry: "quick" | "agent"): string {
  const params = new URLSearchParams({ task: "extraction", mode: "organize", checkpoint: "structure", entry, projectId, documentId });
  return `/workspace?${params.toString()}`;
}

function documentFormat(document: SorDocument, l: UiTranslate): string {
  const format = document.detected_format || document.file_type || document.file_name.split(".").slice(1).pop();
  return typeof format === "string" && format ? format.toUpperCase() : l("home.fileFallback", "文件");
}

function RecentDocuments({ project }: { project: Project }) {
  const l = useUiCopy();
  const locale = useUiLocale();
  const loadDocuments = useCallback(() => api.sorDocuments(project.id), [project.id]);
  const documents = useAsync(loadDocuments, [project.id], { retain: false });
  const recentDocuments = [...(documents.data || [])].sort((a, b) =>
    (Date.parse(b.created_at || "") || 0) - (Date.parse(a.created_at || "") || 0),
  ).slice(0, 5);

  return (
    <section className="product-home-documents" aria-labelledby="home-documents-title">
      <div className="product-home-section-heading">
        <div><h2 id="home-documents-title">{l("home.recentDocuments", "最近文档")}</h2><p className="product-home-document-project">{project.name}</p></div>
        <AppLink className="product-home-text-link" to={`${workspacePath}&projectId=${encodeURIComponent(project.id)}`}>{l("home.allDocuments", "全部文档")}<Icon name="chevron" size={15} /></AppLink>
      </div>
      {documents.loading ? (
        <div className="product-home-document-state" role="status">{l("home.loadingDocuments", "正在加载文档…")}</div>
      ) : documents.error ? (
        <div className="product-home-document-state" role="alert">
          <p>{l("home.loadDocumentsError", "暂时无法加载文档：{message}", { message: documents.error.message })}</p>
          <button type="button" className="product-home-text-link" onClick={() => void documents.refresh()}><Icon name="refresh" size={15} />{l("home.reloadDocuments", "重新加载文档")}</button>
        </div>
      ) : recentDocuments.length === 0 ? (
        <div className="product-home-document-state"><p>{l("home.noDocuments", "这个项目还没有客户文档。进入项目添加文件后，即可从这里继续。")}</p></div>
      ) : (
        <ul className="product-home-document-list">
          {recentDocuments.map((document) => (
            <li key={document.id}>
              <Icon name="document" size={21} />
              <div className="product-home-document-name">
                <strong>{document.file_name}</strong>
                <span>{documentFormat(document, l)}{document.created_at && Number.isFinite(Date.parse(document.created_at)) ? <> · <time dateTime={document.created_at}>{createdDate(document, locale)}</time> {l("common.uploaded", "上传")}</> : null}</span>
              </div>
              <div className="product-home-document-actions">
                <AppLink className="product-home-text-link" to={documentPath(project.id, document.id, "quick")} aria-label={l("home.quickReadAria", "快速读取：{name}", { name: document.file_name })}>{l("home.quickRead", "快速读取")}<Icon name="arrow" size={15} /></AppLink>
                <AppLink className="product-home-text-link product-home-document-agent" to={documentPath(project.id, document.id, "agent")} aria-label={l("home.agentOrganizeAria", "Agent 整理：{name}", { name: document.file_name })}>{l("home.agentOrganize", "Agent 整理")}</AppLink>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function agentStatus(data: JsonRecord | null, loading: boolean, error: Error | null, l: UiTranslate) {
  if (error) return { label: l("home.statusUnavailable", "状态未取得"), tone: "warning" as const, detail: l("home.agentStatusUnavailable", "暂时无法读取 Agent 状态，请刷新后重试。") };
  if (loading) return { label: l("home.agentChecking", "正在检查"), tone: "neutral" as const, detail: l("home.agentCheckingDetail", "正在读取当前 Agent 配置。") };
  if (!data || data.runtime_id === "none") return { label: l("home.agentUnconfigured", "未配置"), tone: "neutral" as const, detail: l("home.agentUnconfiguredDetail", "配置 Agent 后，可自动整理原文条目与分类。") };
  if (data.runtime_id === "provider-tool-loop" && data.ready === true && data.configured === true && data.readiness_scope === "configuration") {
    return { label: l("home.agentReady", "配置就绪"), tone: "success" as const, detail: l("home.agentReadyDetail", "在线连接与额度将在任务启动时检查。") };
  }
  if (data.runtime_id === "codex-app-server" && data.ready === true) {
    return { label: l("home.agentOnline", "已就绪"), tone: "success" as const, detail: l("home.agentOnlineDetail", "可以在文档中发起 Agent 整理。") };
  }
  const requiresLogin = String(data.reason_code || "").toLowerCase().includes("auth_required");
  return {
    label: requiresLogin ? l("home.agentLogin", "需要登录") : l("home.agentAction", "需要处理"),
    tone: "warning" as const,
    detail: requiresLogin ? l("home.agentLoginDetail", "完成 Agent 登录后，刷新状态即可。") : l("home.agentActionDetail", "检查 Agent 配置后，再发起整理任务。"),
  };
}

export function HomePage() {
  const l = useUiCopy();
  const locale = useUiLocale();
  const applicationHost = useApplicationHost();
  const projects = useAsync(loadProjects, []);
  const readiness = useAsync(loadReadiness, []);
  const agent = useAsync(loadAgentReadiness, []);

  useEffect(() => { if (!applicationHost) document.title = l("home.documentTitle", "ReqPilotX · 工作首页"); }, [applicationHost, l]);

  const recentProjects = [...(projects.data || [])].sort((a, b) =>
    (Date.parse(b.created_at || "") || 0) - (Date.parse(a.created_at || "") || 0),
  ).slice(0, 6);
  const agentState = applicationHost
    ? { label: l("home.hostProvided", "宿主提供"), tone: "neutral" as const, detail: l("home.hostManagedBy", "由 {label} 管理认证与运行；请在宿主设置中核对登录状态。", { label: applicationHost.label }) }
    : agentStatus(agent.data, agent.loading, agent.error, l);
  const modelName = applicationHost?.modelLabel ?? (typeof agent.data?.model_name === "string" ? agent.data.model_name : "");
  const serviceReady = !readiness.error && !readiness.loading && readiness.data?.runtime_ready === true;
  const serviceLabel = readiness.error ? l("home.serviceUnconfirmed", "连接未确认") : readiness.loading ? l("home.serviceChecking", "正在检查") : serviceReady ? l("home.serviceAvailable", "服务可用") : l("home.serviceAction", "需要处理");
  const refreshing = readiness.loading || agent.loading;

  return (
    <div className="product-home">
      <header className="product-home-header">
        <Brand />
        <nav aria-label={l("home.navAria", "首页导航")}>
          <span aria-current="page">{l("home.current", "工作首页")}</span>
          <AppLink to={settingsPath}><Icon name="settings" size={16} />{l("home.agentSettings", "Agent 设置")}</AppLink>
        </nav>
      </header>

      <main className="product-home-main">
        <div className="product-home-heading">
          <div>
            <h1>{l("home.title", "从文档开始，继续你的工作。")}</h1>
            <p>{l("home.subtitle", "选择项目，查看原文、整理需求并导出矩阵。")}</p>
          </div>
          <AppLink className="button primary" to={workspacePath}>{l("home.openWorkspace", "打开工作台")}<Icon name="arrow" /></AppLink>
        </div>

        <div className="product-home-work">
          <div className="product-home-library">
          {recentProjects[0] && !projects.error ? <RecentDocuments key={recentProjects[0].id} project={recentProjects[0]} /> : null}
          <section className="product-home-projects" aria-labelledby="home-projects-title">
            <div className="product-home-section-heading">
              <h2 id="home-projects-title">{l("home.continueProjects", "继续项目")}</h2>
              {projects.data && !projects.error ? <AppLink className="product-home-text-link" to={workspacePath}>{l("home.allProjects", "全部项目（{count}）", { count: projects.data.length })}<Icon name="chevron" size={15} /></AppLink> : null}
            </div>
            {projects.loading && !projects.data ? (
              <div className="product-home-project-loading" role="status">
                <span>{l("home.loadingProjects", "正在加载项目…")}</span>
                {[0, 1, 2].map((index) => <div className="product-home-skeleton" key={index} aria-hidden="true" />)}
              </div>
            ) : projects.error ? (
              <div className="product-home-empty" role="alert">
                <h3>{l("home.loadProjectsTitle", "暂时无法加载项目")}</h3>
                <p>{projects.error.message}</p>
                <button className="button ghost" type="button" onClick={() => void projects.refresh()} disabled={projects.loading}>
                  <Icon name="refresh" size={16} />{projects.loading ? l("home.retrying", "正在重试…") : l("home.reload", "重新加载")}
                </button>
              </div>
            ) : recentProjects.length === 0 ? (
              <div className="product-home-empty">
                <Icon name="folder" size={28} />
                <h3>{l("home.firstProject", "创建你的第一个项目")}</h3>
                <p>{l("home.firstProjectDescription", "打开工作台，创建项目并添加客户文档。已有原文随时可以继续查看。")}</p>
              </div>
            ) : (
              <ul className="product-home-project-list">
                {recentProjects.map((project) => (
                  <li key={project.id}>
                    <AppLink className="product-home-project-link" to={`${workspacePath}&projectId=${encodeURIComponent(project.id)}`} aria-label={l("home.continueProjectAria", "继续项目：{name}", { name: project.name })}>
                      <Icon name="folder" size={20} />
                      <span className="product-home-project-name"><strong>{project.name}</strong><span>{project.customer_name || l("home.unnamedCustomer", "未填写客户名称")}</span></span>
                      <span className="product-home-project-date"><span>{l("home.createdAt", "创建于")}</span><time dateTime={project.created_at}>{createdDate(project, locale)}</time></span>
                      <Icon name="chevron" size={17} />
                    </AppLink>
                  </li>
                ))}
              </ul>
            )}
          </section>
          </div>

          <section className="product-home-paths" aria-labelledby="home-paths-title">
            <div className="product-home-section-heading"><h2 id="home-paths-title">{l("home.chooseMethod", "选择处理方式")}</h2></div>
            <article className="product-home-path">
              <Icon name="document" size={22} />
              <div><h3>{l("home.quickRead", "快速读取")}</h3><p>{l("home.quickReadDescription", "先查看原文与捕获内容，再决定需要整理的范围。")}</p>
                <AppLink className="product-home-text-link" to={`${workspacePath}&entry=quick`}>{l("home.chooseDocumentRead", "选择文档读取")}<Icon name="arrow" size={16} /></AppLink>
              </div>
            </article>
            <article className="product-home-path">
              <Icon name="spark" size={22} />
              <div><h3>{l("home.agentOrganize", "Agent 整理")}</h3><p>{l("home.agentOrganizeDescription", "委托 Agent 形成条目与分类，回到原文核对并修订。")}</p>
                <AppLink className="product-home-text-link" to={`${workspacePath}&entry=agent`}>{l("home.chooseDocumentOrganize", "选择文档整理")}<Icon name="arrow" size={16} /></AppLink>
              </div>
            </article>
          </section>
        </div>

        <section className="product-home-runtime" aria-labelledby="home-runtime-title">
          <div className="product-home-section-heading">
            <h2 id="home-runtime-title">{l("home.runtime", "运行状态")}</h2>
            <button className="product-home-text-link" type="button" disabled={refreshing} onClick={() => void Promise.all([readiness.refresh(), agent.refresh()])}>
              <Icon name="refresh" size={15} />{refreshing ? l("home.checking", "正在检查…") : l("home.refreshStatus", "刷新状态")}
            </button>
          </div>
          <div className="product-home-runtime-grid" aria-live="polite">
            <div className="product-home-runtime-item">
              <div className="product-home-runtime-title"><h3>{l("home.projectService", "项目与文档服务")}</h3><StatusBadge tone={serviceReady ? "success" : readiness.loading ? "neutral" : "warning"}>{serviceLabel}</StatusBadge></div>
              <p>{readiness.error ? l("home.serviceConnectError", "暂时无法连接服务，请确认服务已启动后刷新。") : readiness.loading ? l("home.serviceCheckingDetail", "正在检查服务连接。") : serviceReady ? l("home.serviceReadyDetail", "基础服务检查通过。") : l("home.serviceFailedDetail", "服务检查未通过，请检查服务状态后刷新。")}</p>
            </div>
            <div className="product-home-runtime-item">
              <div className="product-home-runtime-title"><h3>{l("home.agent", "Agent")}</h3><StatusBadge tone={agentState.tone}>{agentState.label}</StatusBadge></div>
              <p>{modelName && !agent.error ? <span className="product-home-model">{modelName} · </span> : null}{agentState.detail}</p>
              <AppLink className="product-home-text-link" to={settingsPath}>{l("home.configureAgent", "配置 Agent")}<Icon name="arrow" size={15} /></AppLink>
            </div>
          </div>
        </section>
      </main>
      <footer className="product-home-footer">{l("home.footer", "ReqPilotX · 需求工程工作台")}</footer>
    </div>
  );
}
