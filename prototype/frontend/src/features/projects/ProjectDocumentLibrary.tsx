import { readViewedWork, rememberViewedWork } from "../workspace/documentWorkSelection";
import { DocumentWorkChoices } from "../workspace/DocumentWorkPicker";
import { selectedPluginWork, type PluginWorkSummary } from "../workspace/documentWorkCatalog";
import { useEffect, useMemo, useState } from "react";
import { Icon } from "../../shared/ui/Icon";
import { formatBytes, formatDate } from "../../shared/lib/format";
import { useWorkspace } from "../workspace/WorkspaceContext";
import { agentTaskLabel, documentDeliveryLabel, documentWorkCheckpoint, isAgentTaskActive, readDocumentWorkSummary, type DocumentWorkSummary } from "../workspace/documentWorkSummary";
import type { UploadKind } from "../documents/uploadQueue";

export function ProjectDocumentLibrary({ onRemove, kind: controlledKind, onKindChange }: { kind?: UploadKind; onKindChange?: (kind: UploadKind) => void; onRemove: (kind: UploadKind, id: string, fileName: string) => void }) {
  const { route, updateRoute, projects, documents, knowledge, selectedProject, loading, refreshing, refreshProject } = useWorkspace();
  const [internalKind, setInternalKind] = useState<UploadKind>("sor");
  const kind = controlledKind || internalKind;
  const setKind = (value: UploadKind) => { setInternalKind(value); onKindChange?.(value); };
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [projectQuery, setProjectQuery] = useState("");
  const [preview, setPreview] = useState("");
  const [choosing, setChoosing] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => { setPreview(""); setChoosing(""); setFilter("all"); setQuery(""); setProjectQuery(""); }, [kind, route.projectId]);
  const [loaded, setLoaded] = useState<{ scope: string; summaries: Record<string, DocumentWorkSummary>; complete: boolean }>({ scope: "", summaries: {}, complete: false });
  const sourceScope = `${route.projectId}:${documents.map(d => `${d.id}:${d.source_sha256 || ""}`).join("|")}`;
  const summaries = loaded.scope === sourceScope ? loaded.summaries : {};
  const complete = loaded.scope === sourceScope && loaded.complete;
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let index = 0;
    const next: Record<string, DocumentWorkSummary> = {};
    setLoaded(previous => ({ scope: sourceScope, summaries: previous.scope === sourceScope ? previous.summaries : {}, complete: false }));
    const run = async () => {
      while (index < documents.length && !controller.signal.aborted) {
        const document = documents[index++];
        if (!document.source_sha256) continue;
        let value: DocumentWorkSummary | null;
        try { value = await readDocumentWorkSummary(route.projectId, document.id, String(document.source_sha256), controller.signal); }
        catch { value = { sourceSha256: String(document.source_sha256), delivery: null, task: null, deliveryError: "暂时无法读取工作版", taskError: "任务状态暂未同步" }; }
        if (value && !controller.signal.aborted) {
          next[document.id] = value;
          setLoaded(previous => ({ scope: sourceScope, summaries: { ...(previous.scope === sourceScope ? previous.summaries : {}), ...next }, complete: false }));
        }
      }
    };
    void Promise.all(Array.from({ length: Math.min(4, documents.length) }, run)).then(() => {
      if (controller.signal.aborted) return;
      setLoaded({ scope: sourceScope, summaries: next, complete: true });
      if (Object.values(next).some(value => isAgentTaskActive(value.task))) timer = setTimeout(() => setRefresh(value => value + 1), 5000);
    });
    return () => { controller.abort(); clearTimeout(timer); };
  }, [sourceScope, route.projectId, refresh]);
  const files = kind === "sor" ? documents : knowledge;
  const previewFile = files.find(file => file.id === preview);
  const choosingFile = kind === "sor" ? documents.find(file => file.id === choosing) : undefined;
  const visible = files.filter(file => {
    if (!file.file_name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) return false;
    if (kind !== "sor" || filter === "all") return true;
    const summary = summaries[file.id];
    if (filter === "active") return isAgentTaskActive(summary?.task);
    if (filter === "ready") return Boolean(summary?.delivery || summary?.task?.stages?.length || summary?.pluginDraft?.revision_id);
    return Boolean(!file.source_sha256 || summary?.pluginDraft?.map_confirmation_required || summary?.task?.map_confirmation_required || summary?.task?.job_status === "FAILED" || summary?.task?.job_status === "CANCELLED" || summary?.deliveryError || summary?.taskError);
  });
  const projectMatches = useMemo(() => projects.filter(p => `${p.name} ${p.customer_name}`.toLocaleLowerCase().includes(projectQuery.trim().toLocaleLowerCase())), [projects, projectQuery]);
  const open = (id: string) => {
    const summary = summaries[id];
    const file = documents.find(document => document.id === id);
    const remembered = readViewedWork(route.projectId, id, String(file?.source_sha256 || ""));
    if (!remembered && summary?.pluginDraft && file?.source_sha256) { setChoosing(id); return; }
    const plugin = remembered?.producer === "EXTERNAL_PLUGIN" ? {session_id:remembered.sessionId!, revision_id:remembered.revisionId} : null;
    updateRoute({ documentId: id, requirementSetId: "", task: "extraction", mode: "organize", checkpoint: plugin ? "list" : documentWorkCheckpoint(summary || null),
      pluginSessionId: plugin?.session_id, resultRevision: plugin?.revision_id || undefined });
  };
  const chooseWork = (item: PluginWorkSummary | null) => {
    if (!choosingFile?.source_sha256) return;
    const source = String(choosingFile.source_sha256);
    const selection = item ? selectedPluginWork(route.projectId, choosingFile.id, source, item) : { work: { producer: "NATIVE" as const }, checkpoint: documentWorkCheckpoint(summaries[choosingFile.id] || null) };
    rememberViewedWork(route.projectId, choosingFile.id, source, selection.work);
    setChoosing("");
    updateRoute({ documentId: choosingFile.id, requirementSetId: "", task: "extraction", mode: "organize", checkpoint: selection.checkpoint,
      pluginSessionId: "sessionId" in selection.work ? selection.work.sessionId : undefined, resultRevision: "revisionId" in selection.work ? selection.work.revisionId : undefined });
  };
  return <section className="project-document-library" aria-label="项目文档台账">
    <div className="document-library-context">
      <details className="project-picker"><summary><Icon name="projects" size={16} />{selectedProject?.name || "选择项目"}<Icon name="chevron-down" size={14} /></summary>
        <div><input type="search" aria-label="搜索项目" value={projectQuery} onChange={e => setProjectQuery(e.target.value)} placeholder="搜索项目名称或客户" />
          <div role="listbox" aria-label="项目列表">{projectMatches.map(project => <button role="option" aria-label={`${project.name} ${project.customer_name}`} aria-selected={route.projectId === project.id} type="button" key={project.id} onClick={event => { event.currentTarget.closest("details")?.removeAttribute("open"); updateRoute({ projectId: project.id, documentId: "", requirementSetId: "", task: "projects" }); }}><strong>{project.name}</strong><span>{project.customer_name}</span></button>)}{!projectMatches.length && <p role="status">没有匹配的项目，请调整搜索。</p>}</div>
        </div>
      </details>
      <span>{selectedProject?.customer_name}</span>
    </div>
    <div className="document-library-tabs" role="tablist" aria-label="来源类型">
      <button role="tab" type="button" aria-selected={kind === "sor"} onClick={() => { setKind("sor"); setFilter("all"); }}>客户文档 <span>{documents.length}</span></button>
      <button role="tab" type="button" aria-selected={kind === "knowledge"} onClick={() => { setKind("knowledge"); setFilter("all"); }}>内部知识 <span>{knowledge.length}</span></button>
    </div>
    <div className="records-toolbar">
      <div className="records-filters" aria-label="文档工作状态">{[["all", "全部"], ["attention", "需处理"], ["active", "处理中"], ["ready", "可审阅"]].map(([value, label]) => <button type="button" key={value} disabled={kind !== "sor" && value !== "all" || !complete && value !== "all"} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</button>)}</div>
      <label className="records-search"><Icon name="search" size={15} /><input type="search" aria-label="搜索文档" placeholder="搜索文档" value={query} onChange={e => setQuery(e.target.value)} /></label>
      <button className="button ghost compact" type="button" aria-label="同步文档成果" onClick={() => { setRefresh(value => value + 1); void refreshProject?.().catch(() => {}); }}><Icon name="refresh" size={15} className={!complete ? "is-spinning" : ""} /></button>
    </div>
    <div className="document-records-scroll"><table className="document-records"><thead><tr><th>文件</th><th>已有成果</th><th>当前任务</th><th>来源信息</th><th>操作</th></tr></thead><tbody>
      {visible.map(file => { const summary = summaries[file.id] || null; const active = isAgentTaskActive(summary?.task); const ready = Boolean(summary?.delivery || summary?.task?.stages?.length || summary?.pluginDraft?.revision_id);
        return <tr key={file.id}><td><button type="button" className="document-file-link" onClick={() => kind === "sor" ? open(file.id) : setPreview(file.id)}><Icon name="document" size={19} /><span><strong>{file.file_name}</strong><small>{formatBytes(file.source_size || file.file_size)}</small></span></button></td>
          <td>{kind === "sor" ? <><strong>{!file.source_sha256 ? "来源信息未就绪" : summary ? documentDeliveryLabel(summary) : complete ? "成果状态待同步" : "正在读取成果…"}</strong><small>{summary?.delivery ? `${summary.delivery.source_item_count} 原文 · ${summary.delivery.requirement_count} 需求` : summary?.task?.stages?.length ? "阶段草稿，尚未进入当前工作版" : ""}</small></> : <><strong>内部依据</strong><small>与客户需求来源分别保存</small></>}</td>
          <td><span className={`document-status-chip${active ? " is-active" : summary?.task?.map_confirmation_required || summary?.task?.job_status === "FAILED" ? " is-attention" : ""}`}>{kind === "sor" ? summary ? summary.taskError || (summary.pluginDraft && !summary.task ? summary.pluginDraft.map_confirmation_required ? "待核对结构" : summary.pluginDraft.revision_id ? "插件成果可核对" : "等待插件成果" : agentTaskLabel(summary.task)) : !file.source_sha256 ? "请同步文档信息" : complete ? "状态暂不可用" : "正在同步…" : String(file.status || "已保存")}</span></td>
          <td><span>{formatDate(file.created_at)}</span><small>{String(file.detected_format || file.file_type || "文件").toUpperCase()}</small></td>
          <td><div className="document-row-actions">{kind === "sor" && <button type="button" className="button secondary compact" onClick={() => open(file.id)}>{ready ? "打开成果" : active ? "查看任务" : "打开工作台"}<Icon name="arrow" size={13} /></button>}
            {kind === "sor" && summary?.pluginDraft && <button type="button" className="button secondary compact" onClick={() => setChoosing(current => current === file.id ? "" : file.id)}>选择工作</button>}
            <button className="icon-button" type="button" aria-label={`预览 ${file.file_name}`} onClick={() => setPreview(current => current === file.id ? "" : file.id)}><Icon name="eye" size={15} /></button>
            <button className="icon-button" type="button" aria-label={`移除 ${file.file_name}`} onClick={() => onRemove(kind, file.id, file.file_name)}><Icon name="trash" size={15} /></button></div></td>
        </tr>;
      })}
    </tbody></table>{!visible.length && <div className="records-empty"><Icon name="document" size={24} /><h3>{files.length ? "没有匹配的文档" : loading || refreshing ? "正在读取项目文档" : kind === "sor" ? "添加第一份客户文档" : "还没有内部知识依据"}</h3><p>{files.length ? "调整搜索或状态筛选。" : loading || refreshing ? "读取完成后，文档会显示在这里。" : "在上方添加文件，保存后即可进入对应工作。"}</p></div>}</div>
    <footer className="records-footer"><span>{visible.length} / {files.length} 份文件{kind === "sor" && !complete ? " · 正在同步已有成果" : ""}</span><span>已有成果与当前任务分别显示</span></footer>
    {choosingFile && Boolean(choosingFile.source_sha256) && <section className="document-library-work-choice" aria-label="选择文档工作"><header><strong>{choosingFile.file_name}</strong><button type="button" className="button ghost compact" onClick={() => setChoosing("")}>关闭工作选择</button></header>
      <DocumentWorkChoices key={`${route.projectId}:${choosingFile.id}:${choosingFile.source_sha256}`} projectId={route.projectId} documentId={choosingFile.id} sourceSha256={String(choosingFile.source_sha256)}
        summary={summaries[choosingFile.id] || null} onSelect={chooseWork} />
    </section>}
    {previewFile && <section className="document-preview-panel" aria-label="文档预览"><header><strong>{previewFile.file_name}</strong><button type="button" className="icon-button" aria-label="关闭文档预览" onClick={() => setPreview("")}><Icon name="close" /></button></header>
      {kind === "sor" && String(previewFile.file_name || "").toLowerCase().endsWith(".pdf") ? <iframe title="客户文档预览" src={`/pdf-viewer?embedded=1&fit=page&pdf=${encodeURIComponent(`/api/projects/${route.projectId}/sor-documents/${preview}/file`)}&page=1`} /> : kind === "sor" ? <a className="button secondary" href={`/api/projects/${encodeURIComponent(route.projectId)}/sor-documents/${encodeURIComponent(preview)}/file`} target="_blank" rel="noreferrer">打开原文件</a> : <button type="button" className="button secondary" onClick={() => updateRoute({ task: "knowledge", knowledgeSpace: "project", knowledgePage: previewFile.id })}>查看这份项目依据</button>}
    </section>}
  </section>;
}
