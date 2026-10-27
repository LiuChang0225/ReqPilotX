import { useCallback, useEffect } from "react";
import { api, request } from "../../shared/api/client";
import type { JsonRecord, Project, SorDocument } from "../../shared/api/contracts";
import { useAsync } from "../../shared/hooks/useAsync";
import { AppLink } from "../../shared/navigation";
import { Brand } from "../../shared/ui/Brand";
import { Icon } from "../../shared/ui/Icon";
import { StatusBadge } from "../../shared/ui/Feedback";
import { useApplicationHost } from "../../host/ApplicationHost";
import "./home.css";

const workspacePath = "/workspace?task=projects";
const settingsPath = `${workspacePath}&settings=agent`;
const loadProjects = () => api.projects();
const loadReadiness = () => api.readiness();
const loadAgentReadiness = () => request<JsonRecord>("/api/runtime/agent-readiness");
const dateFormat = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });

function createdDate(value: { created_at?: string }): string {
  const timestamp = Date.parse(value.created_at || "");
  return Number.isFinite(timestamp) ? dateFormat.format(timestamp) : "—";
}

function documentPath(projectId: string, documentId: string, entry: "quick" | "agent"): string {
  const params = new URLSearchParams({ task: "extraction", mode: "organize", checkpoint: "structure", entry, projectId, documentId });
  return `/workspace?${params.toString()}`;
}

function documentFormat(document: SorDocument): string {
  const format = document.detected_format || document.file_type || document.file_name.split(".").slice(1).pop();
  return typeof format === "string" && format ? format.toUpperCase() : "文件";
}

function RecentDocuments({ project }: { project: Project }) {
  const loadDocuments = useCallback(() => api.sorDocuments(project.id), [project.id]);
  const documents = useAsync(loadDocuments, [project.id], { retain: false });
  const recentDocuments = [...(documents.data || [])].sort((a, b) =>
    (Date.parse(b.created_at || "") || 0) - (Date.parse(a.created_at || "") || 0),
  ).slice(0, 5);

  return (
    <section className="product-home-documents" aria-labelledby="home-documents-title">
      <div className="product-home-section-heading">
        <div><h2 id="home-documents-title">最近文档</h2><p className="product-home-document-project">{project.name}</p></div>
        <AppLink className="product-home-text-link" to={`${workspacePath}&projectId=${encodeURIComponent(project.id)}`}>全部文档<Icon name="chevron" size={15} /></AppLink>
      </div>
      {documents.loading ? (
        <div className="product-home-document-state" role="status">正在加载文档…</div>
      ) : documents.error ? (
        <div className="product-home-document-state" role="alert">
          <p>暂时无法加载文档：{documents.error.message}</p>
          <button type="button" className="product-home-text-link" onClick={() => void documents.refresh()}><Icon name="refresh" size={15} />重新加载文档</button>
        </div>
      ) : recentDocuments.length === 0 ? (
        <div className="product-home-document-state"><p>这个项目还没有客户文档。进入项目添加文件后，即可从这里继续。</p></div>
      ) : (
        <ul className="product-home-document-list">
          {recentDocuments.map((document) => (
            <li key={document.id}>
              <Icon name="document" size={21} />
              <div className="product-home-document-name">
                <strong>{document.file_name}</strong>
                <span>{documentFormat(document)}{document.created_at && Number.isFinite(Date.parse(document.created_at)) ? <> · <time dateTime={document.created_at}>{createdDate(document)}</time> 上传</> : null}</span>
              </div>
              <div className="product-home-document-actions">
                <AppLink className="product-home-text-link" to={documentPath(project.id, document.id, "quick")} aria-label={`快速读取：${document.file_name}`}>快速读取<Icon name="arrow" size={15} /></AppLink>
                <AppLink className="product-home-text-link product-home-document-agent" to={documentPath(project.id, document.id, "agent")} aria-label={`Agent 整理：${document.file_name}`}>Agent 整理</AppLink>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function agentStatus(data: JsonRecord | null, loading: boolean, error: Error | null) {
  if (error) return { label: "状态未取得", tone: "warning" as const, detail: "暂时无法读取 Agent 状态，请刷新后重试。" };
  if (loading) return { label: "正在检查", tone: "neutral" as const, detail: "正在读取当前 Agent 配置。" };
  if (!data || data.runtime_id === "none") return { label: "未配置", tone: "neutral" as const, detail: "配置 Agent 后，可自动整理原文条目与分类。" };
  if (data.runtime_id === "provider-tool-loop" && data.ready === true && data.configured === true && data.readiness_scope === "configuration") {
    return { label: "配置就绪", tone: "success" as const, detail: "在线连接与额度将在任务启动时检查。" };
  }
  if (data.runtime_id === "codex-app-server" && data.ready === true) {
    return { label: "已就绪", tone: "success" as const, detail: "可以在文档中发起 Agent 整理。" };
  }
  const requiresLogin = String(data.reason_code || "").toLowerCase().includes("auth_required");
  return {
    label: requiresLogin ? "需要登录" : "需要处理",
    tone: "warning" as const,
    detail: requiresLogin ? "完成 Agent 登录后，刷新状态即可。" : "检查 Agent 配置后，再发起整理任务。",
  };
}

export function HomePage() {
  const applicationHost = useApplicationHost();
  const projects = useAsync(loadProjects, []);
  const readiness = useAsync(loadReadiness, []);
  const agent = useAsync(loadAgentReadiness, []);

  useEffect(() => { if (!applicationHost) document.title = "ReqPilotX · 工作首页"; }, [applicationHost]);

  const recentProjects = [...(projects.data || [])].sort((a, b) =>
    (Date.parse(b.created_at || "") || 0) - (Date.parse(a.created_at || "") || 0),
  ).slice(0, 6);
  const agentState = applicationHost
    ? { label: "宿主提供", tone: "neutral" as const, detail: `由 ${applicationHost.label} 管理认证与运行；请在宿主设置中核对登录状态。` }
    : agentStatus(agent.data, agent.loading, agent.error);
  const modelName = applicationHost?.modelLabel ?? (typeof agent.data?.model_name === "string" ? agent.data.model_name : "");
  const serviceReady = !readiness.error && !readiness.loading && readiness.data?.runtime_ready === true;
  const serviceLabel = readiness.error ? "连接未确认" : readiness.loading ? "正在检查" : serviceReady ? "服务可用" : "需要处理";
  const refreshing = readiness.loading || agent.loading;

  return (
    <div className="product-home">
      <header className="product-home-header">
        <Brand />
        <nav aria-label="首页导航">
          <span aria-current="page">工作首页</span>
          <AppLink to={settingsPath}><Icon name="settings" size={16} />Agent 设置</AppLink>
        </nav>
      </header>

      <main className="product-home-main">
        <div className="product-home-heading">
          <div>
            <h1>从文档开始，继续你的工作。</h1>
            <p>选择项目，查看原文、整理需求并导出矩阵。</p>
          </div>
          <AppLink className="button primary" to={workspacePath}>打开工作台<Icon name="arrow" /></AppLink>
        </div>

        <div className="product-home-work">
          <div className="product-home-library">
          {recentProjects[0] && !projects.error ? <RecentDocuments key={recentProjects[0].id} project={recentProjects[0]} /> : null}
          <section className="product-home-projects" aria-labelledby="home-projects-title">
            <div className="product-home-section-heading">
              <h2 id="home-projects-title">继续项目</h2>
              {projects.data && !projects.error ? <AppLink className="product-home-text-link" to={workspacePath}>全部项目（{projects.data.length}）<Icon name="chevron" size={15} /></AppLink> : null}
            </div>
            {projects.loading && !projects.data ? (
              <div className="product-home-project-loading" role="status">
                <span>正在加载项目…</span>
                {[0, 1, 2].map((index) => <div className="product-home-skeleton" key={index} aria-hidden="true" />)}
              </div>
            ) : projects.error ? (
              <div className="product-home-empty" role="alert">
                <h3>暂时无法加载项目</h3>
                <p>{projects.error.message}</p>
                <button className="button ghost" type="button" onClick={() => void projects.refresh()} disabled={projects.loading}>
                  <Icon name="refresh" size={16} />{projects.loading ? "正在重试…" : "重新加载"}
                </button>
              </div>
            ) : recentProjects.length === 0 ? (
              <div className="product-home-empty">
                <Icon name="folder" size={28} />
                <h3>创建你的第一个项目</h3>
                <p>打开工作台，创建项目并添加客户文档。已有原文随时可以继续查看。</p>
              </div>
            ) : (
              <ul className="product-home-project-list">
                {recentProjects.map((project) => (
                  <li key={project.id}>
                    <AppLink className="product-home-project-link" to={`${workspacePath}&projectId=${encodeURIComponent(project.id)}`} aria-label={`继续项目：${project.name}`}>
                      <Icon name="folder" size={20} />
                      <span className="product-home-project-name"><strong>{project.name}</strong><span>{project.customer_name || "未填写客户名称"}</span></span>
                      <span className="product-home-project-date"><span>创建于</span><time dateTime={project.created_at}>{createdDate(project)}</time></span>
                      <Icon name="chevron" size={17} />
                    </AppLink>
                  </li>
                ))}
              </ul>
            )}
          </section>
          </div>

          <section className="product-home-paths" aria-labelledby="home-paths-title">
            <div className="product-home-section-heading"><h2 id="home-paths-title">选择处理方式</h2></div>
            <article className="product-home-path">
              <Icon name="document" size={22} />
              <div><h3>快速读取</h3><p>先查看原文与捕获内容，再决定需要整理的范围。</p>
                <AppLink className="product-home-text-link" to={`${workspacePath}&entry=quick`}>选择文档读取<Icon name="arrow" size={16} /></AppLink>
              </div>
            </article>
            <article className="product-home-path">
              <Icon name="spark" size={22} />
              <div><h3>Agent 整理</h3><p>委托 Agent 形成条目与分类，回到原文核对并修订。</p>
                <AppLink className="product-home-text-link" to={`${workspacePath}&entry=agent`}>选择文档整理<Icon name="arrow" size={16} /></AppLink>
              </div>
            </article>
          </section>
        </div>

        <section className="product-home-runtime" aria-labelledby="home-runtime-title">
          <div className="product-home-section-heading">
            <h2 id="home-runtime-title">运行状态</h2>
            <button className="product-home-text-link" type="button" disabled={refreshing} onClick={() => void Promise.all([readiness.refresh(), agent.refresh()])}>
              <Icon name="refresh" size={15} />{refreshing ? "正在检查…" : "刷新状态"}
            </button>
          </div>
          <div className="product-home-runtime-grid" aria-live="polite">
            <div className="product-home-runtime-item">
              <div className="product-home-runtime-title"><h3>项目与文档服务</h3><StatusBadge tone={serviceReady ? "success" : readiness.loading ? "neutral" : "warning"}>{serviceLabel}</StatusBadge></div>
              <p>{readiness.error ? "暂时无法连接服务，请确认服务已启动后刷新。" : readiness.loading ? "正在检查服务连接。" : serviceReady ? "基础服务检查通过。" : "服务检查未通过，请检查服务状态后刷新。"}</p>
            </div>
            <div className="product-home-runtime-item">
              <div className="product-home-runtime-title"><h3>Agent</h3><StatusBadge tone={agentState.tone}>{agentState.label}</StatusBadge></div>
              <p>{modelName && !agent.error ? <span className="product-home-model">{modelName} · </span> : null}{agentState.detail}</p>
              <AppLink className="product-home-text-link" to={settingsPath}>配置 Agent<Icon name="arrow" size={15} /></AppLink>
            </div>
          </div>
        </section>
      </main>
      <footer className="product-home-footer">ReqPilotX · 需求工程工作台</footer>
    </div>
  );
}
