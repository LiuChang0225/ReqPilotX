import { useCallback, useEffect, useMemo, useRef, useState, type PropsWithChildren } from "react";
import { SourceItemAgentLoop, type AgentLoopTask } from "../extraction/SourceItemAgentLoop";
import { SourceDocumentStageResults, type SourceStage } from "../extraction/SourceDocumentStageResults";
import { Icon } from "../../shared/ui/Icon";
import { useWorkspace } from "./WorkspaceContext";
import { DocumentAgentContext, type DocumentAgentBinding } from "./DocumentAgentContext";
import { agentTaskLabel, documentDeliveryLabel, useDocumentWorkSummary } from "./documentWorkSummary";
import { PluginWorkbench } from "./PluginWorkbench";
import { usePluginWorkbench } from "./usePluginWorkbench";
import { rememberViewedWork } from "./documentWorkSelection";
import { ExternalAgentPanel } from "./ExternalAgentPanel";
import { consumeReadingHandoffIntent } from "./readingHandoff";
import { DocumentWorkPicker } from "./DocumentWorkPicker";
import { selectedPluginWork, type PluginWorkSummary } from "./documentWorkCatalog";

/** One document task, shared by structure, source review, analysis and delivery. */
export function DocumentWorkSurface({ children }: PropsWithChildren) {
  const { route, selectedDocument, refreshDocument } = useWorkspace();
  const enabled = Boolean(selectedDocument?.source_sha256 && !route.reviewCase && !route.requirementSetId && ["extraction", "analysis", "export"].includes(route.task));
  if (!enabled) return <>{children}</>;
  return <DocumentSurface key={`${route.projectId}:${route.documentId}:${selectedDocument!.source_sha256}`}>{children}</DocumentSurface>;
}
function DocumentSurface({ children }: PropsWithChildren) {
  const { route, updateRoute, selectedDocument, refreshDocument } = useWorkspace();
  const sourceSha256 = String(selectedDocument?.source_sha256 || "");
  const isExternal = Boolean(route.pluginSessionId);
  const plugin = usePluginWorkbench({ projectId: route.projectId, documentId: route.documentId, sessionId: route.pluginSessionId,
    sourceSha256, revisionId: route.resultRevision, onRevision: resultRevision => updateRoute({ resultRevision }, { replace: true }) });
  const [revision, setRevision] = useState(0);
  const summary = useDocumentWorkSummary(route.projectId, route.documentId, sourceSha256, revision);
  useEffect(() => {
    if (isExternal && plugin.state && (!plugin.pinned || plugin.version)) rememberViewedWork(route.projectId, route.documentId, sourceSha256, { producer:"EXTERNAL_PLUGIN", sessionId:route.pluginSessionId, revisionId:plugin.pinned || undefined });
    else if (!isExternal && summary) rememberViewedWork(route.projectId, route.documentId, sourceSha256, {producer:"NATIVE"});
  }, [isExternal, plugin.state?.session_id, plugin.version?.revision_id, plugin.pinned, summary, route.projectId, route.documentId, route.pluginSessionId, sourceSha256]);
  const [binding, setBinding] = useState<DocumentAgentBinding | null>(null);
  const [task, setTask] = useState<AgentLoopTask | null>(null);
  const [override, setOverride] = useState<AgentLoopTask | null>(null);
  const [opened, setOpened] = useState(() => window.innerWidth >= 1500);
  const [openRequest, setOpenRequest] = useState(0);
  const [requestedFeedbackScope, setRequestedFeedbackScope] = useState<{ id: number; scope: "selected" | "document" } | null>(null);
  const [initialGoal, setInitialGoal] = useState("");
  const [stage, setStage] = useState<(SourceStage & { taskId: string; sourceSha256: string }) | null>(null);
  const [historyHost, setHistoryHost] = useState<HTMLElement | null>(null);
  const autoStages = useRef(new Set<string>());
  const briefOpened = useRef(false);
  const bindingOwner = useRef<symbol | null>(null);
  const bindingRef = useRef(binding); bindingRef.current = binding;
  const trigger = useRef<HTMLButtonElement>(null);
  const currentTask = isExternal ? null : task || summary?.task || null;
  const baseline = (!binding?.revisionId ? binding?.baselineSha256 : "") || summary?.delivery?.baseline_sha256 || "";
  const bind = useCallback((next: DocumentAgentBinding) => {
    if (next.sourceSha256 !== sourceSha256 || isExternal && next.revisionId !== plugin.pinned) return () => {};
    const owner = Symbol("document-agent-binding"); bindingOwner.current = owner;
    bindingRef.current = next;
    setBinding(previous => previous?.baselineSha256 === next.baselineSha256 && previous.selectedSourceItemId === next.selectedSourceItemId && previous.selectedLabel === next.selectedLabel && previous.selectedNodeId === next.selectedNodeId && previous.revisionId === next.revisionId ? previous : next);
    return () => { if (bindingOwner.current === owner) { bindingOwner.current = null; bindingRef.current = null; setBinding(null); } };
  }, [sourceSha256, isExternal, plugin.pinned]);
  const activeBinding = isExternal ? binding?.revisionId === plugin.pinned ? binding : null : binding?.revisionId ? null : binding;
  const show = useCallback((goal = "", scope?: "selected" | "document") => {
    setInitialGoal(goal); setOpened(true); setOpenRequest(value => value + 1);
    if (scope) setRequestedFeedbackScope(value => ({ id: (value?.id || 0) + 1, scope }));
  }, []);
  useEffect(() => {
    if (!isExternal || !plugin.state || !plugin.reader || !plugin.pinned) return;
    const intent = consumeReadingHandoffIntent({ projectId: route.projectId, documentId: route.documentId, sourceSha256,
      sessionId: route.pluginSessionId, revisionId: plugin.pinned });
    if (intent) show("", "document");
  }, [isExternal, plugin.state?.session_id, plugin.reader?.revision_id, plugin.pinned, route.projectId, route.documentId, route.pluginSessionId, sourceSha256, show]);
  useEffect(() => {
    if (!isExternal && summary && !summary.pluginDraft && !summary.delivery && !summary.deliveryError && !summary.taskError && !currentTask?.task_id && !bindingRef.current?.baselineSha256 && !briefOpened.current) {
      briefOpened.current = true;
      show("完整阅读当前客户文档，先提供可核对的文档结构，再整理原文条目与分类；按已明确的粒度作必要拆分，基于实际内部依据分析，最后保存同一版本的 Matrix 草稿。");
    }
  }, [summary, currentTask?.task_id, show, isExternal]);
  const openStage = useCallback((value: SourceStage) => {
    if (!currentTask?.task_id || currentTask.source_sha256 && currentTask.source_sha256 !== sourceSha256) return;
    setStage({ ...value, taskId: currentTask.task_id, sourceSha256 });
    updateRoute({ task: "extraction", mode: "organize", checkpoint: value.stage === "MAP" ? "structure" : "list" });
    if (window.innerWidth < 1300) setOpened(false);
  }, [currentTask?.task_id, currentTask?.source_sha256, sourceSha256, updateRoute]);
  const receiveTask = useCallback((value: AgentLoopTask) => {
    if (value.source_sha256 && value.source_sha256 !== sourceSha256) return;
    setTask(value);
  }, [sourceSha256]);
  const stageCheckpoint = route.checkpoint;
  const stageRouteActive = !isExternal && route.task === "extraction" && route.mode === "organize";
  useEffect(() => {
    setStage(previous => previous && stageRouteActive && previous.sourceSha256 === sourceSha256
      && stageCheckpoint === (previous.stage === "MAP" ? "structure" : "list") ? previous : null);
  }, [stageRouteActive, stageCheckpoint, sourceSha256]);
  useEffect(() => {
    if (!stageRouteActive || !summary || summary.delivery || summary.deliveryError || baseline || bindingRef.current?.baselineSha256
      || currentTask?.source_sha256 && currentTask.source_sha256 !== sourceSha256) return;
    const first = currentTask?.stages?.find(item => item.stage === (stageCheckpoint === "list" ? "ITEMS" : "MAP"));
    const firstKey = first && currentTask ? `${currentTask.task_id}:${sourceSha256}:${first.artifact_sha256}` : "";
    if (first && currentTask && !autoStages.current.has(firstKey)) {
      autoStages.current.add(firstKey);
      const checkpoint = first.stage === "MAP" ? "structure" : "list";
      if (stageCheckpoint !== checkpoint) updateRoute({ task: "extraction", mode: "organize", checkpoint }, { replace: true });
      setStage(previous => previous || { ...first, taskId: currentTask.task_id, sourceSha256 });
    }
  }, [summary, baseline, currentTask, stageRouteActive, stageCheckpoint, sourceSha256, updateRoute]);

  const updated = useCallback(() => {
    setRevision(value => value + 1);
    bindingRef.current?.onUpdated();
    void refreshDocument();
  }, [refreshDocument]);
  const context = useMemo(() => ({ bind, task: currentTask, show, openStage, historyHost, producer: isExternal ? "EXTERNAL_PLUGIN" as const : "NATIVE" as const }), [bind, currentTask, show, openStage, historyHost, isExternal]);
  const root = `/api/projects/${encodeURIComponent(route.projectId)}/sor-documents/${encodeURIComponent(route.documentId)}/agent-structure/delivery/extraction-loop`;
  const sourceFileUrl = `/api/projects/${encodeURIComponent(route.projectId)}/sor-documents/${encodeURIComponent(route.documentId)}/file`;
  const stageCurrent = stage && stageRouteActive && stage.sourceSha256 === sourceSha256
    && stageCheckpoint === (stage.stage === "MAP" ? "structure" : "list") ? stage : null;
  const stageLatest = stage && currentTask?.stages?.find(value => value.stage === stage.stage);
  const stageChanged = Boolean(stage && currentTask && (stage.taskId !== currentTask.task_id || stageLatest?.artifact_sha256 !== stage.artifact_sha256));
  const selectWork = (item: PluginWorkSummary | null) => {
    if (item?.session_id === route.pluginSessionId || !item && !isExternal) return;
    if (route.pluginSessionId && plugin.pinned) rememberViewedWork(route.projectId, route.documentId, sourceSha256, { producer: "EXTERNAL_PLUGIN", sessionId: route.pluginSessionId, revisionId: plugin.pinned });
    if (!item) { rememberViewedWork(route.projectId, route.documentId, sourceSha256, { producer: "NATIVE" }); updateRoute({ pluginSessionId: undefined, resultRevision: undefined }); return; }
    const selected = selectedPluginWork(route.projectId, route.documentId, sourceSha256, item);
    rememberViewedWork(route.projectId, route.documentId, sourceSha256, selected.work);
    updateRoute({ pluginSessionId: selected.work.sessionId, resultRevision: selected.work.revisionId,
      ...(route.task === "extraction" ? { mode: "organize", checkpoint: selected.checkpoint } : {}) });
  };
  return <DocumentAgentContext.Provider value={context}>
    <section className={`document-work-surface${opened ? " is-agent-open" : ""}`} aria-label="文档任务与成果" data-producer={isExternal ? "EXTERNAL_PLUGIN" : "NATIVE"} data-viewed-revision={isExternal ? plugin.pinned || "" : baseline}>
      <header className="document-work-status">
        <div><DocumentWorkPicker projectId={route.projectId} documentId={route.documentId} sourceSha256={sourceSha256}
            selectedSession={route.pluginSessionId} selectedTitle={plugin.state?.goal || undefined} summary={summary} onSelect={selectWork} />
          {!isExternal && <span className="document-saved-label"><Icon name="document" size={14} />{documentDeliveryLabel(summary)}</span>}
          {isExternal && plugin.state ? <span role="status">{!plugin.state.revision_id ? "尚无阶段成果" : !plugin.current && plugin.pinned ? "正在核对历史版本" : plugin.state.map_confirmation_required ? "待核对结构" : "成果已保存"}</span> : null}
          {isExternal && Boolean(plugin.state?.versions.length) && <label className="work-version-picker">核对版本<select aria-label="核对版本" value={plugin.pinned || ""} onChange={event => plugin.selectRevision(event.target.value)}>{plugin.state!.versions.map(value => <option key={value.revision_id} value={value.revision_id}>v{value.number}{value.revision_id === plugin.state?.revision_id ? " · 最新" : ""}</option>)}</select></label>}
          {!isExternal && summary?.delivery && <span>{summary.delivery.source_item_count} 原文 · {summary.delivery.requirement_count} 需求</span>}
          {!isExternal && <span className="document-task-label" role="status">{summary?.taskError || (!summary && !currentTask ? "正在同步任务…" : agentTaskLabel(currentTask))}</span>}
        </div>
        <button ref={trigger} type="button" className="button secondary compact" aria-expanded={opened} onClick={() => opened ? setOpened(false) : show()}><Icon name="spark" size={15} />{opened ? "收起协作" : "Agent 协作"}</button>
      </header>
      {isExternal && plugin.error && <p className="agent-inline-notice is-error" role="alert">{plugin.error} <button className="button ghost compact" onClick={plugin.refresh}>重新同步</button></p>}
      {isExternal && plugin.notice && <p className="agent-inline-notice" role="status">{plugin.notice}</p>}
      {isExternal && plugin.state?.revision_id && plugin.pinned && !plugin.current && <p className="stage-update-notice" role="status">有新成果，正在核对的版本保持不变。<button className="button secondary compact" onClick={() => plugin.selectRevision(plugin.state!.revision_id!)}>查看最新成果</button></p>}
      <div className="document-work-layout">
        <div className="document-work-main">
          {!isExternal && !summary && <div className="document-loading" role="status"><Icon name="refresh" size={18} /><strong>正在读取当前文档成果</strong><span>正在同步工作版与任务状态…</span></div>}
          <div className="document-existing-surface" hidden={!isExternal && (Boolean(stageCurrent) || !summary)}>{isExternal ? <PluginWorkbench provider={plugin} projectId={route.projectId} documentId={route.documentId} sourceSha256={sourceSha256} documentName={selectedDocument!.file_name}/> : children}</div>
          {stageCurrent && <section className="document-stage-workspace" aria-label="阶段成果工作区">
            <header><div><h2>{stageCurrent.stage === "MAP" ? "核对文档结构" : "核对原文条目"}</h2><p>{selectedDocument?.file_name} · 已保存的阶段草稿</p></div>
              <button className="button ghost compact" type="button" onClick={() => setStage(null)}>{baseline ? "返回当前工作版" : "返回文档"}</button></header>
            {stageChanged && <p className="stage-update-notice" role="status">当前任务的阶段成果有更新，正在核对的版本保持不变。{stageLatest && <button type="button" className="button secondary compact" onClick={() => openStage(stageLatest)}>查看最新成果</button>}</p>}
            <SourceDocumentStageResults root={root} taskId={stageCurrent.taskId} sourceSha256={stageCurrent.sourceSha256}
              stages={[stageCurrent]} confirmationRequired={Boolean(!stageChanged && stageCurrent.taskId === currentTask?.task_id && currentTask.map_confirmation_required)}
              initialStage={stageCurrent.stage} workspace sourceFileUrl={sourceFileUrl}
              onConfirmed={value => { const next = value as AgentLoopTask; setOverride(next); receiveTask(next); updated(); }} />
          </section>}
        </div>
        <aside className="document-agent-pane" hidden={!opened} aria-label="Agent 任务工作区">
          {isExternal ? <ExternalAgentPanel provider={plugin} projectId={route.projectId} documentId={route.documentId} documentName={selectedDocument!.file_name} binding={activeBinding} openRequest={openRequest}
            requestedScope={requestedFeedbackScope || undefined}
            onClose={() => { setOpened(false); trigger.current?.focus(); }} onViewStage={(value, resultRevision) => { updateRoute({ task:"extraction",mode:"organize",checkpoint:value.stage === "MAP" ? "structure" : "list", ...(resultRevision ? {resultRevision} : {}) }); if (window.innerWidth < 1300) setOpened(false); }}/>
          : <SourceItemAgentLoop embedded projectId={route.projectId} documentId={route.documentId} sourceSha256={sourceSha256}
            baselineSha256={baseline} baselineRevision={baseline === summary?.delivery?.baseline_sha256 ? summary.delivery.baseline_revision : undefined} selectedSourceItemId={activeBinding?.selectedSourceItemId || ""}
            selectedLabel={activeBinding?.selectedLabel} documentName={selectedDocument?.file_name}
            mode={baseline ? "review" : "extract"} onUpdated={updated} onTaskChange={receiveTask}
            openRequest={openRequest} initialGoal={initialGoal} onOpenStage={openStage} taskOverride={override}
            requestedScope={requestedFeedbackScope || undefined}
            historyHostRef={setHistoryHost} startReady={Boolean(summary && !summary.deliveryError && !summary.taskError)}
            onClose={() => { setOpened(false); trigger.current?.focus(); }} />}
        </aside>
      </div>
    </section>
  </DocumentAgentContext.Provider>;
}
