import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { PluginWorkbenchContent } from "../features/workspace/PluginWorkbench";
import { usePluginWorkbench } from "../features/workspace/usePluginWorkbench";
import { DocumentAgentContext, type DocumentAgentBinding } from "../features/workspace/DocumentAgentContext";
import { WorkbenchHostContext } from "../features/workspace/WorkbenchHostContext";
import { externalExecutionLabel } from "../features/workspace/documentWorkProvider";
import type { PluginVersion } from "../features/workspace/documentWorkProvider";
import { matchingExecution, matchingSession, sameWork, validWorkSelection, type ReadonlyWorkbenchHost, type ReadonlyWorkSelection } from "./readonlyHost";
import type { WorkspaceRoute } from "../features/workspace/route";
import { ErrorBoundary } from "../app/ErrorBoundary";

export type ReqPilotXReadonlyPanelProps = {
  selection: ReadonlyWorkSelection; host: ReadonlyWorkbenchHost; documentName: string;
  theme?: "light" | "dark"; active?: boolean;
};
const emptySnapshot = () => null;
const noSubscription = () => () => {};

/** No app bootstrap, browser-history mutation, model configuration, or business writes. */
export function ReqPilotXReadonlyPanel(props: ReqPilotXReadonlyPanelProps) {
  if (!validWorkSelection(props.selection)) return <section className="reqpilotx-panel-root workspace-app" data-theme={props.theme || "light"}>
    <p role="alert">工作链接不完整，请提供准确的来源、工作及成果版本。</p>
  </section>;
  return <section className="reqpilotx-panel-root workspace-app" data-theme={props.theme || "light"} aria-label="ReqPilotX 只读工作面板">
    <ErrorBoundary embedded key={`${props.host.id}:${props.host.client.id}:${props.selection.projectId}:${props.selection.documentId}:${props.selection.sourceSha256}:${props.selection.workRef}`}><ReadonlyPanel {...props} /></ErrorBoundary>
  </section>;
}

function ReadonlyPanel({ selection: initial, host, documentName, theme = "light", active = true }: ReqPilotXReadonlyPanelProps) {
  const initialKey = JSON.stringify(initial);
  const [local, setLocal] = useState({ initialKey, selection: initial });
  const selection = local.initialKey === initialKey ? local.selection : initial;
  const selectionRef = useRef(selection); selectionRef.current = selection;
  const [issue, setIssue] = useState("");
  const [opening, setOpening] = useState(false);
  const selectedContextRef = useRef<DocumentAgentBinding | null>(null);
  const owner = useRef<symbol | null>(null);
  const versionsRef = useRef<PluginVersion[]>([]);
  const changeSelection = useCallback((patch: Partial<ReadonlyWorkSelection>) => {
    const next = { ...selectionRef.current, ...patch };
    selectionRef.current = next;
    setLocal({ initialKey, selection: next });
    host.onSelectionChange?.(next);
  }, [initialKey, host.onSelectionChange]);
  const onRevision = useCallback((revisionId: string) => {
    const version = versionsRef.current.find(value => value.revision_id === revisionId);
    changeSelection({ revisionId, ...(version && !version.stages.some(stage => stage.stage === "ITEMS") ? { view: "structure" as const } : {}) });
  }, [changeSelection]);
  const provider = usePluginWorkbench({ projectId: selection.projectId, documentId: selection.documentId,
    sourceSha256: selection.sourceSha256, sessionId: selection.workRef, revisionId: selection.revisionId,
    onRevision, readClient: host.client, readOnly: true, pollIntervalMs: active && !host.subscribeWorkUpdates ? 5000 : 0 });
  versionsRef.current = provider.state?.versions || [];
  const binding = matchingSession(selection, host.sessionBinding);
  const reported = useSyncExternalStore(host.execution?.subscribe || noSubscription, host.execution?.getSnapshot || emptySnapshot, emptySnapshot);
  const execution = matchingExecution(binding, reported);
  const refreshRef = useRef(provider.refresh); refreshRef.current = provider.refresh;
  useEffect(() => host.subscribeWorkUpdates?.(event => {
    if (sameWork(selectionRef.current, event) && /^[a-f0-9]{64}$/.test(event.revisionId)) refreshRef.current();
  }), [host.subscribeWorkUpdates]);
  const bind = useCallback((next: DocumentAgentBinding) => {
    if (next.sourceSha256 !== selectionRef.current.sourceSha256 || next.revisionId !== selectionRef.current.revisionId) return () => {};
    const token = Symbol(); owner.current = token; selectedContextRef.current = next;
    return () => { if (owner.current === token) { owner.current = null; selectedContextRef.current = null; } };
  }, []);
  const canDiscuss = Boolean(binding && host.openSession && !opening);
  const show = useCallback((_goal = "", scope: "selected" | "document" = "document") => {
    if (!binding || !host.openSession || opening) return;
    const captured = selectionRef.current;
    const currentContext = selectedContextRef.current?.revisionId === captured.revisionId ? selectedContextRef.current : null;
    if (scope === "selected" && !currentContext) { setIssue("当前选择的来源尚未就绪，请稍后重试。"); return; }
    setIssue(""); setOpening(true);
    const context = scope === "selected" && currentContext ? { scope, nodeId: currentContext.selectedNodeId,
      sourceItemId: currentContext.selectedSourceItemId || undefined, derivedUnitId: currentContext.selectedDerivedUnitId } : { scope: "document" as const };
    Promise.resolve().then(() => host.openSession!(binding, { ...captured, ...context })).catch(error => setIssue(error instanceof Error ? error.message : "关联会话暂不可打开，请重试。")).finally(() => setOpening(false));
  }, [binding, host.openSession, opening]);
  const agent = useMemo(() => ({ producer: "EXTERNAL_PLUGIN" as const, collaborationAvailable: canDiscuss,
    task: null, historyHost: null, bind, show, openStage: () => {} }), [canDiscuss, bind, show]);
  const presentation = useMemo(() => ({ readOnly: true, resourceUrl: host.resourceUrl, pdfViewerUrl: host.pdfViewerUrl }), [host.resourceUrl, host.pdfViewerUrl]);
  const view = provider.version && !provider.version.stages.some(stage => stage.stage === "ITEMS") ? "structure" : selection.view || "structure";
  const route: WorkspaceRoute = { task: view === "analysis" || view === "export" ? view : "extraction", mode: "organize",
    checkpoint: view === "items" ? "list" : "structure", projectId: selection.projectId, documentId: selection.documentId,
    requirementSetId: "", pluginSessionId: selection.workRef, resultRevision: provider.pinned || undefined };
  const status = execution ? ({ RUNNING: "会话正在处理", WAITING: "会话正在等待", IDLE: "会话当前空闲", SUCCEEDED: "会话本轮已结束", FAILED: "会话本轮未完成", INTERRUPTED: "会话已暂停" })[execution.status]
    : host.execution ? "宿主尚未报告当前工作的运行状态" : externalExecutionLabel(provider.state);
  return <WorkbenchHostContext.Provider value={presentation}><DocumentAgentContext.Provider value={agent}>
      <header className="readonly-panel-header"><div><strong title={documentName}>{documentName}</strong><span>ReqPilotX · 只读工作面板</span></div>
        <label>核对版本<select aria-label="核对版本" value={provider.pinned || ""} onChange={event => provider.selectRevision(event.target.value)} disabled={!provider.state?.versions.length}>
          {!provider.state?.versions.some(version => version.revision_id === provider.pinned) && <option value={provider.pinned || ""}>{!provider.state ? "正在同步版本" : provider.pinned ? "指定版本不可用" : "尚无成果"}</option>}{provider.state?.versions.map(version => <option key={version.revision_id} value={version.revision_id}>v{version.number}{version.revision_id === provider.state?.revision_id ? " · 最新" : ""}</option>)}
        </select></label>
        <button type="button" className="button ghost compact" onClick={provider.refresh}>同步成果</button>
        <button type="button" className="button secondary compact" disabled={!canDiscuss} onClick={() => show()}>{opening ? "正在打开会话…" : `打开关联${host.label}会话`}</button>
      </header>
      <div className="readonly-panel-status"><span role="status">{execution && !execution.connected ? "运行状态连接中断" : status}</span>
        {execution?.connected && execution.status === "RUNNING" && execution.activityLabel && <span>{execution.activityLabel}</span>}
        {execution && <time dateTime={execution.observedAt}>报告于 {new Date(execution.observedAt).toLocaleTimeString("zh-CN")}</time>}
        <span>{provider.version ? `当前核对 v${provider.version.number}` : provider.state?.revision_id ? "正在读取指定成果" : provider.state ? "尚无阶段成果" : "正在同步成果"}</span>
        {!binding && <span>{host.sessionBinding ? "会话未绑定当前工作" : "尚未关联宿主会话"}</span>}
      </div>
      {issue && <p role="alert" className="readonly-panel-notice">{issue}</p>}
      {provider.error && <p role="alert" className="readonly-panel-notice">{provider.error}</p>}
      {provider.state?.revision_id && provider.pinned && !provider.current && <p className="readonly-panel-notice">已有更新的成果，当前核对版保留。<button type="button" className="button ghost compact" onClick={() => provider.selectRevision(provider.state!.revision_id!)}>查看最新成果</button></p>}
      <nav className="readonly-panel-views" aria-label="业务视图">{([ ["structure", "原文结构"], ["items", "原文台账"], ["analysis", "响应与证据"], ["export", "版本与导出"] ] as const).map(([value, label]) => <button type="button" key={value} aria-current={view === value ? "page" : undefined} disabled={value !== "structure" && !provider.reader?.outline} onClick={() => changeSelection({ view: value })}>{label}</button>)}</nav>
      <div className="readonly-panel-content"><PluginWorkbenchContent provider={provider} projectId={selection.projectId} documentId={selection.documentId}
        sourceSha256={selection.sourceSha256} documentName={documentName} route={route} updateRoute={patch => {
          if (patch.task === "analysis" || patch.task === "export") changeSelection({ view: patch.task });
          else changeSelection({ view: patch.checkpoint === "list" ? "items" : "structure" });
        }} /></div>
  </DocumentAgentContext.Provider></WorkbenchHostContext.Provider>;
}
