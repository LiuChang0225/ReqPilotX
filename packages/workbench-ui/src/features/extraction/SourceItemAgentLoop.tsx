/** Operate: a source-bound Agent companion for the existing engineering workbench.
 * Neutral paper, green task state, compact evidence labels and a bounded composer.
 * Read the real task and its results, then steer the affected source scope.
 * A nonmodal portal keeps the source workbench and all controls reachable.
 * Reference: Beautiful UI task rows / context chips / prompt bar, adapted in place.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ApiError, request } from "../../shared/api/client";
import { saasStorageKey } from "../../shared/api/saasSession";
import { Icon } from "../../shared/ui/Icon";
import { AgentPanelFrame } from "../workspace/AgentPanelFrame";
import { useDocumentAgent } from "../workspace/DocumentAgentContext";
import { agentTaskLabel, agentProgressLabel } from "../workspace/documentWorkSummary";
import { readAgentEventTail, retainedCommand, settleCommand, reducePublicMessages, messageKey, type PublicAgentMessage, type PublicAgentEvent } from "./agentTaskTransport";
import { SourceDocumentStageResults, type SourceStage } from "./SourceDocumentStageResults";
import { useSaasNativeAgentAvailability } from "./saasAgentAvailability";

export type AgentLoopTask = {
  task_id: string; job_id: string; job_status: string; runtime_status?: string;
  source_sha256?: string; thread_id?: string; turn_id?: string;
  goal?: string; task_mode?: string; updated_at?: string;
  can_steer: boolean; can_interrupt: boolean; can_resume: boolean;
  stages?: SourceStage[]; map_confirmation_required?: boolean;
  analysis_policy?: { status: string; reason: string };
  progress?: { message?: string }; error_message?: string; final_message?: string;
  result?: { baseline_revision_artifact?: { baseline_artifact_sha256: string; baseline_revision: number }; message?: string; status?: string; application_saved?: boolean };
};
type PublicEvent = PublicAgentEvent;
const ACTIVE = new Set(["PENDING", "RUNNING", "CANCEL_REQUESTED"]);
const labels: Record<string, string> = { PENDING: "已排队", RUNNING: "处理中", CANCEL_REQUESTED: "正在暂停", SUCCEEDED: "本轮已结束", FAILED: "本轮未完成", CANCELLED: "已暂停" };
const toolLabels: Record<string, string> = { reqpilot_context: "读取任务与当前版本", reqpilot_skill: "读取工作方法",
  reqpilot_read: "查阅原文与工件", reqpilot_candidate: "准备修订建议", reqpilot_run: "执行校验与交付工具", reqpilot_publish: "准备保存工作版" };
function activityLabel(event: PublicEvent) {
  if (event.message || event.text) return event.message || event.text;
  if (event.name || event.tool_name) return `${toolLabels[event.name || event.tool_name || ""] || "处理任务"}${event.type === "tool_completed" ? event.success ? " · 已完成" : " · 需要修正" : ""}`;
  return ({ "thread/started": "任务会话已创建", "turn/started": "Agent 开始处理", "turn/completed": "本轮处理已结束", steer_delivered: "新增反馈已送达", runtime_error: "执行中出现问题，成果已保留" } as Record<string, string>)[event.type || ""] || "任务状态已更新";
}

function PublicMessageText({ text, documentName, sourceUrl }: { text: string; documentName?: string; sourceUrl: string }) {
  const parts: ReactNode[] = []; let cursor = 0;
  for (const match of text.matchAll(/:codex-file-citation\{path="([^"]+)" purpose="source"\}/g)) {
    parts.push(text.slice(cursor, match.index));
    parts.push(documentName && match[1] === `source/${documentName}`
      ? <a key={match.index} href={sourceUrl} target="_blank" rel="noreferrer">{documentName} · 查看原件</a> : match[0]);
    cursor = match.index! + match[0].length;
  }
  parts.push(text.slice(cursor));
  return <p>{parts}</p>;
}

export interface SourceItemAgentLoopProps {
  projectId: string; documentId: string; sourceSha256: string; baselineSha256: string;
  selectedSourceItemId: string; selectedLabel?: string; documentName?: string; baselineRevision?: number;
  onUpdated: () => void; mode?: "review" | "extract"; openRequest?: number;
  onTaskChange?: (task: AgentLoopTask) => void; initialGoal?: string;
  embedded?: boolean; onClose?: () => void; onOpenStage?: (stage: SourceStage) => void;
  historyHostRef?: (element: HTMLDivElement | null) => void; taskOverride?: AgentLoopTask | null;
  startReady?: boolean;
}
function BoundAgentEntry(props: SourceItemAgentLoopProps) {
  const workspace = useDocumentAgent()!;
  const callbacks = useRef(props); callbacks.current = props;
  useEffect(() => {
    return workspace.bind({ sourceSha256: props.sourceSha256, baselineSha256: props.baselineSha256,
      selectedSourceItemId: props.selectedSourceItemId, selectedLabel: props.selectedLabel,
      onUpdated: () => callbacks.current.onUpdated() });
  }, [workspace.bind, props.sourceSha256, props.baselineSha256, props.selectedSourceItemId, props.selectedLabel]);
  useEffect(() => { if (workspace.task) callbacks.current.onTaskChange?.(workspace.task); }, [workspace.task]);
  useEffect(() => { if (props.openRequest) workspace.show(props.initialGoal); }, [props.openRequest, props.initialGoal, workspace.show]);
  return null;
}
export function SourceItemAgentLoop(props: SourceItemAgentLoopProps) {
  const workspace = useDocumentAgent();
  return workspace && !props.embedded ? <BoundAgentEntry {...props} /> : <SourceItemAgentLoopController {...props} />;
}
function SourceItemAgentLoopController({ projectId, documentId, sourceSha256, baselineSha256, selectedSourceItemId,
  selectedLabel, documentName, baselineRevision, onUpdated, mode = "review", openRequest = 0, onTaskChange, initialGoal = "",
  embedded = false, onClose, onOpenStage, historyHostRef, taskOverride, startReady = true }: SourceItemAgentLoopProps) {
  const availability = useSaasNativeAgentAvailability();
  const root = `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/delivery/${mode === "extract" ? "extraction-loop" : "loop"}`;
  const key = `${root}:${sourceSha256}`, identity = useRef(key); identity.current = key;
  const draftKey = saasStorageKey(`reqpilotx:agent-loop-draft:${key}:${baselineSha256}:${selectedSourceItemId || "document"}`);
  const draftIdentity = useRef(draftKey); draftIdentity.current = draftKey;
  const updated = useRef(onUpdated); updated.current = onUpdated;
  const taskChanged = useRef(onTaskChange); taskChanged.current = onTaskChange;
  const id = useId();
  const [task, setTask] = useState<AgentLoopTask | null>(null), [text, setText] = useState("");
  const [analysisMode, setAnalysisMode] = useState<"AUTO" | "SKIP">("AUTO");
  const [confirmMap, setConfirmMap] = useState(false);
  const [scope, setScope] = useState<"selected" | "document">("selected");
  const [pending, setPending] = useState(false), [error, setError] = useState("");
  const [draftStorageUnavailable, setDraftStorageUnavailable] = useState(false);
  const [notice, setNotice] = useState(""), [events, setEvents] = useState<PublicEvent[]>([]), [retry, setRetry] = useState(0);
  const [messages, setMessages] = useState<PublicAgentMessage[]>([]);
  const [connection, setConnection] = useState<"connecting" | "live" | "retrying" | "idle" | "polling">("idle");
  const cursor = useRef(0), refreshed = useRef("");
  const streamTask = useRef(""), currentTask = useRef("");
  const disclosure = useRef<HTMLDetailsElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const focusComposerRequested = useRef(false);
  const [expanded, setExpanded] = useState(embedded);
  const [historyLoading, setHistoryLoading] = useState(false);
  const unresolvedCommand = useRef<{ signature: string; id: string } | null>(null);
  const openedStage = useRef("");
  useEffect(() => {
    if (taskOverride?.task_id && (!taskOverride.source_sha256 || taskOverride.source_sha256 === sourceSha256)) {
      currentTask.current = `${taskOverride.task_id}:${taskOverride.job_id}`; setTask(taskOverride);
    }
  }, [taskOverride, sourceSha256]);
  const latestStage = task?.stages?.map(stage => stage.artifact_sha256).join(":") || "";
  useEffect(() => {
    if (mode === "extract" && latestStage && openedStage.current !== latestStage) {
      openedStage.current = latestStage; setExpanded(true);
    }
  }, [mode, latestStage]);
  useEffect(() => {
    if (openRequest > 0) { focusComposerRequested.current = true; setExpanded(true); if (initialGoal) setText(current => current || initialGoal); }
  }, [openRequest, initialGoal]);
  useEffect(() => {
    if (expanded && focusComposerRequested.current) {
      focusComposerRequested.current = false;
      composer.current?.focus();
    }
  }, [expanded, openRequest]);
  const active = !!task && ACTIVE.has(task.job_status);
  useEffect(() => { try { setText(localStorage.getItem(draftKey) || ""); setDraftStorageUnavailable(false); } catch { setText(""); setDraftStorageUnavailable(true); } }, [draftKey]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setTask(null); setError(""); setNotice(""); setEvents([]); setMessages([]); setHistoryLoading(false); setPending(false); cursor.current = 0; refreshed.current = "";
    currentTask.current = ""; streamTask.current = "";
    const discover = async () => {
      try {
        const value = await request<AgentLoopTask | null>(`${root}/latest`, { signal: controller.signal });
        if (!controller.signal.aborted && value?.source_sha256 && value.source_sha256 !== sourceSha256) {
          currentTask.current = ""; setTask(null); setEvents([]); setMessages([]);
          setError("任务属于此前的原文件版本，请回到当前文档后重新发起或查看原版本历史。");
          return;
        }
        if (!controller.signal.aborted && value?.task_id) { currentTask.current = `${value.task_id}:${value.job_id}`; setTask(value); taskChanged.current?.(value); }
      } catch (cause) {
        if (!controller.signal.aborted && !(cause instanceof ApiError && cause.status === 404)) setError("暂时无法读取 Agent 任务，请重试。");
      } finally { if (!controller.signal.aborted) timer = setTimeout(() => void discover(), 15000); }
    };
    void discover();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [key, root, retry]);
  useEffect(() => {
    if (!task?.task_id) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const taskId = task.task_id;
    const readingIdentity = `${taskId}:${task.job_id}`;
    if (streamTask.current !== taskId) { streamTask.current = taskId; cursor.current = 0; setEvents([]); }
    const read = async () => {
      try {
        const value = await request<AgentLoopTask>(`${root}/${encodeURIComponent(taskId)}`, { signal: controller.signal });
        if (controller.signal.aborted || currentTask.current !== readingIdentity) return;
        if (value.source_sha256 && value.source_sha256 !== sourceSha256) {
          currentTask.current = ""; setTask(null); setEvents([]); setMessages([]);
          setError("任务属于此前的原文件版本，请回到当前文档后重新发起或查看原版本历史。");
          return;
        }
        setTask(value);
        taskChanged.current?.(value);
        setError("");
        const activity = await readAgentEventTail(root, taskId, cursor.current, controller.signal);
        if (controller.signal.aborted || currentTask.current !== readingIdentity) return;
        cursor.current = activity.cursor;
        setHistoryLoading(activity.hasMore);
        if (activity.events.length) {
          setEvents(old => [...old, ...activity.events].slice(-40));
          setMessages(old => reducePublicMessages(old, activity.events));
        }
        if (activity.hasMore || ACTIVE.has(value.job_status)) timer = setTimeout(() => void read(), activity.hasMore ? 100 : 2500);
      } catch {
        if (!controller.signal.aborted) {
          setError("任务状态暂时无法同步，后台任务会保留，正在重试。");
          timer = setTimeout(() => void read(), 5000);
        }
      }
    };
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [root, task?.task_id, task?.job_id, sourceSha256]);
  useEffect(() => {
    if (!task?.task_id || !sourceSha256) return;
    setMessages([]);
    if (typeof EventSource === "undefined") { setConnection("polling"); return; }
    const taskId = task.task_id;
    const stream = new EventSource(`${root}/${encodeURIComponent(taskId)}/stream?source_sha256=${encodeURIComponent(sourceSha256)}`);
    let disposed = false;
    setConnection("connecting");
    stream.addEventListener("open", () => { if (!disposed) setConnection("live"); });
    stream.addEventListener("activity", event => {
      if (disposed) return;
      try {
        const batch = JSON.parse((event as MessageEvent).data);
        if (batch.task_id !== taskId || batch.source_sha256 !== sourceSha256) return;
        setConnection("live");
        setMessages(old => reducePublicMessages(old, batch.events || [], batch.messages || []));
      } catch { setConnection("retrying"); }
    });
    stream.addEventListener("complete", () => {
      if (!disposed) { setConnection("idle"); setMessages(old => old.map(message => message.status === "streaming" ? { ...message, status: "interrupted" } : message)); }
      stream.close();
    });
    stream.addEventListener("unavailable", () => { if (!disposed) setConnection("polling"); stream.close(); });
    stream.onerror = () => { if (!disposed) setConnection("retrying"); };
    return () => { disposed = true; stream.close(); };
  }, [root, task?.task_id, task?.job_id, sourceSha256, retry]);
  useEffect(() => {
    const published = task?.result?.baseline_revision_artifact?.baseline_artifact_sha256;
    if (task?.job_status === "SUCCEEDED" && (task.result?.application_saved || published && published !== baselineSha256) && refreshed.current !== task.job_id) {
      refreshed.current = task.job_id; updated.current();
    }
  }, [task, baselineSha256]);
  const mutate = async (action: "start" | "steer" | "interrupt" | "resume") => {
    if ((action === "start" || action === "resume") && !availability.ready) return;
    if (action !== "start" && (!task || task.source_sha256 && task.source_sha256 !== sourceSha256)) return;
    if (pending || (action === "start" || action === "steer") && !text.trim()) return;
    const current = key, sentText = text, sentDraftKey = draftKey;
    const sourceItemIds = scope === "selected" && selectedSourceItemId ? [selectedSourceItemId] : [];
    setPending(true); setError("");
    const signature = JSON.stringify({ action, task: action === "start" ? "" : task?.task_id, sourceSha256, baselineSha256, sourceItemIds, text: action === "interrupt" ? "" : sentText.trim(), analysisMode, confirmMap });
    const commandStorage = `reqpilotx:agent-command:${key}`;
    const commandId = unresolvedCommand.current?.signature === signature ? unresolvedCommand.current.id : retainedCommand(commandStorage, signature);
    unresolvedCommand.current = { signature, id: commandId };
    const command = { command_id: commandId };
    const body = action === "start" ? { ...command, expected_source_sha256: sourceSha256,
      ...(mode === "review" ? { expected_baseline_sha256: baselineSha256, source_item_ids: sourceItemIds } : { analysis_mode: analysisMode, require_map_confirmation: confirmMap }), goal: sentText.trim(), origin: "USER_REQUEST" }
      : action === "steer" ? { ...command, feedback: sentText.trim(), source_item_ids: sourceItemIds, origin: "USER_REQUEST" }
      : action === "resume" ? { ...command, ...(sentText.trim() ? { goal: sentText.trim() } : {}), source_item_ids: sourceItemIds, origin: "USER_REQUEST" } : command;
    try {
      const value = await request<AgentLoopTask>(action === "start" ? root : `${root}/${encodeURIComponent(task!.task_id)}/${action}`,
        { method: "POST", body: JSON.stringify(body) });
      settleCommand(commandStorage, commandId); unresolvedCommand.current = null;
      if (identity.current !== current) return;
      if (value.task_id) { currentTask.current = `${value.task_id}:${value.job_id}`; setTask(value); taskChanged.current?.(value); }
      if (action === "start") { cursor.current = 0; setEvents([]); }
      if (action !== "interrupt" && draftIdentity.current === sentDraftKey) setText(latest => {
        if (latest !== sentText) return latest;
        try { localStorage.removeItem(sentDraftKey); } catch { setDraftStorageUnavailable(true); }
        return "";
      });
      setNotice(action === "steer" ? "补充意见已保存，将进入当前任务。" : action === "interrupt" ? "暂停请求已保存，已产生的成果会保留。" : "任务已保存，可以继续查看原文。");
    } catch (cause) {
      settleCommand(commandStorage, commandId, cause);
      if (identity.current === current) setError(cause instanceof Error ? cause.message : "操作未完成，请重试。");
    } finally { if (identity.current === current) setPending(false); }
  };
  const status = agentTaskLabel(task);
  const progress = active ? agentProgressLabel(task?.progress?.message)
    : task?.result?.application_saved ? `本轮成果已保存${task.result.baseline_revision_artifact?.baseline_revision ? `至第 ${task.result.baseline_revision_artifact.baseline_revision} 版` : ""}，可查看本次变化或导出 Matrix 草稿。`
    : undefined;
  const closePanel = () => {
    if (embedded && onClose) { onClose(); return; }
    setExpanded(false);
    disclosure.current?.querySelector("summary")?.focus();
  };
  const saveDraft = (value: string) => {
    setText(value);
    try { localStorage.setItem(draftKey, value); setDraftStorageUnavailable(false); }
    catch { setDraftStorageUnavailable(true); }
  };
  const suggestions = mode === "extract"
    ? ["先整理文档结构", "检查表格与图片"]
    : ["复核当前分类", "检查原文与派生"];
  const suggestionGoals: Record<string, string> = {
    "先整理文档结构": "完整阅读当前文档，先整理来源忠实的文档结构，保留原编号、逻辑表格和图片，形成可核对的结构草稿。",
    "检查表格与图片": "核对当前文档的表格与图片，检查续表、表头、共享条件和来源位置，列出尚未读清的范围。",
    "复核当前分类": "结合完整原文、所属章节和适用条件，复核当前范围的需求属性与分类，说明需要修正的地方。",
    "检查原文与派生": "核对当前范围的原文与已有派生，检查条件、阈值、单位和例外是否完整保留，修订实际不一致的内容。",
  };
  const panel = <AgentPanelFrame id={`${id}-panel`} hidden={!embedded && !expanded} embedded={embedded} onClose={closePanel}
    footer={<>
        <div className="agent-context-chips" aria-label="任务上下文"><span><Icon name="document" size={13} />{documentName || "当前文档"}</span>
          {baselineSha256 && <span><Icon name="matrix" size={13} />{baselineRevision ? `工作版 v${baselineRevision}` : "当前工作版"}</span>}
          {selectedSourceItemId && scope === "selected" && <span><Icon name="requirement" size={13} />{selectedLabel || "已选条目"}</span>}
        </div>
        {!text && !active && <div className="agent-prompt-suggestions" aria-label="任务建议">{suggestions.map(suggestion => <button type="button" key={suggestion} onClick={() => saveDraft(suggestionGoals[suggestion])}>{suggestion}<Icon name="arrow" size={12} /></button>)}</div>}
        <div className="agent-composer">
          <label htmlFor={`${id}-goal`}>{active ? "补充本轮意见" : "任务或修改意见"}</label>
          <textarea ref={composer} id={`${id}-goal`} rows={3} value={text} maxLength={12000} onChange={event => saveDraft(event.target.value)}
            placeholder={active ? "补充发现，或调整本轮的处理方向…" : "描述要核对、查证或修改的内容…"} />
          <div className="agent-composer-toolbar">
            <label className="source-agent-loop-scope" htmlFor={`${id}-scope`}>修改范围
              <select id={`${id}-scope`} value={selectedSourceItemId ? scope : "document"} onChange={event => setScope(event.target.value as "selected" | "document")}>
                {selectedSourceItemId && <option value="selected">当前条目</option>}<option value="document">当前文档</option>
              </select>
            </label>
            <div className="source-agent-loop-actions">
              {!active && <button type="button" className="button primary compact" disabled={!availability.ready || !startReady || pending || !sourceSha256 || mode === "review" && !baselineSha256 || !text.trim()} onClick={() => void mutate("start")}>开始新任务<Icon name="arrow" size={14} /></button>}
              {active && task?.can_steer && <button type="button" className="button primary compact" disabled={pending || !text.trim()} onClick={() => void mutate("steer")}>补充给 Agent<Icon name="arrow" size={14} /></button>}
            </div>
          </div>
        </div>
        <div className="agent-composer-footnote"><span>{draftStorageUnavailable ? "本机草稿暂存不可用，未发送文字仅保留在当前页面。" : text ? "草稿 · 尚未发送" : "意见将随当前来源与范围提交"}</span>
          <div>{task?.can_interrupt && <button type="button" className="button ghost compact" disabled={pending} onClick={() => void mutate("interrupt")}><Icon name="stop" size={12} />暂停任务</button>}
            {task?.can_resume && <button type="button" className="button secondary compact" disabled={!availability.ready || pending || !!task.map_confirmation_required && !text.trim()} onClick={() => void mutate("resume")}>继续此任务<Icon name="play" size={12} /></button>}
          </div>
        </div>
        {availability.message && <p role="status">{availability.message} <button type="button" className="button ghost compact" onClick={availability.refresh}>刷新 Agent 配置</button></p>}
</>}>

        <section className="agent-task-state" data-active={active} aria-label="当前任务状态">
          <span className="agent-state-mark" aria-hidden="true"><Icon name={active ? "refresh" : task?.result?.application_saved ? "check" : "spark"} size={17} /></span>
          <div><span className="agent-task-status" role="status">{status}</span>
            {task?.updated_at && <time className="agent-task-update" dateTime={task.updated_at}>更新于 {new Date(task.updated_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</time>}
            <h3>{task?.task_id ? "当前文档任务" : mode === "extract" ? "从原文件开始处理" : "交给 Agent 查证与修订"}</h3>
            {task?.goal && <details className="agent-task-goal"><summary>任务目标</summary><p>{task.goal}</p></details>}
            {active && messages.some(message => message.text === progress) ? null : progress ? <p role="status">{progress}</p> : <p>{mode === "extract" ? "读取原文，逐步形成可核对的结构、条目和草稿。" : "围绕当前范围查阅来源、核对判断，并保存修订结果。"}</p>}
          </div>
        </section>
        {task && Boolean(task.stages?.length) && (onOpenStage ? <section className="agent-stage-results" aria-label="阶段成果"><strong>{task.map_confirmation_required ? "需要你处理" : "已可核对的成果"}</strong><div className="agent-stage-result-list">{task.stages?.map(stage => <button key={stage.stage} className="agent-stage-result" type="button" onClick={() => onOpenStage(stage)}><Icon name={stage.stage === "MAP" ? "tree" : "requirement"} size={16} /><span>{stage.stage === "MAP" ? "核对文档结构" : "查看条目与分类"}<small>已保存的阶段草稿</small></span><em>{stage.count} 项</em><Icon name="chevron" size={14} /></button>)}</div>{task.map_confirmation_required && <p>请在主工作区核对这一版结构，再确认或提出修改。</p>}</section> : <SourceDocumentStageResults root={root.replace(/\/loop$/, "/extraction-loop")} taskId={task.task_id} sourceSha256={sourceSha256}
          stages={task.stages || []} confirmationRequired={!!task.map_confirmation_required} onConfirmed={value => {
            const next = value as AgentLoopTask; currentTask.current = `${next.task_id}:${next.job_id}`; setTask(next); taskChanged.current?.(next);
          }} />)}
        {(messages.length > 0 || active) && <section className="agent-public-messages" aria-label="Agent 公开进展">
          <div className="agent-message-heading"><strong>进展与回复</strong><span role="status">{connection === "retrying" ? "连接恢复中 · 已收内容保留" : connection === "connecting" ? "正在同步" : connection === "polling" ? "定时同步" : active && connection === "live" ? "实时连接" : ""}</span></div>
          {messages.length === 0 && active && <p className="agent-message-wait">等待 Agent 的下一条进展；已保存的成果可随时核对。</p>}
          {messages.slice(-3).reverse().map(message => <article key={messageKey(message)} data-streaming={message.status === "streaming"}>
            <div className="agent-message-meta">{message.time_unix && <time dateTime={new Date(message.time_unix * 1000).toISOString()}>{new Date(message.time_unix * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</time>}
              <span>{message.status === "streaming" ? connection === "retrying" ? "等待恢复" : "正在回复" : message.status === "interrupted" ? "回复未完成" : message.phase === "final" || message.phase === "final_answer" ? "本轮回复" : "进展"}</span></div>
            <PublicMessageText text={message.text || "回复正在生成…"} documentName={documentName} sourceUrl={`/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/file`} />
          </article>)}
        </section>}
        {error && <p className="agent-inline-notice is-error" role="alert">{error} <button type="button" className="button ghost compact" onClick={() => setRetry(value => value + 1)}>重新同步</button></p>}
        {task?.error_message && <p className="agent-inline-notice is-error" role="alert">{task.error_message}</p>}
        {notice && <p className="agent-inline-notice" role="status">{notice}</p>}


        {(task?.final_message || task?.result?.message) && !messages.some(message => message.text === (task.final_message || task.result?.message)) && <details className="agent-result-section" open={!task?.stages?.length}>
          <summary><Icon name="document" size={14} />本轮结果<Icon name="chevron-down" size={13} /></summary>
          <p className="source-agent-loop-result">{task.final_message || task.result?.message}</p>
        </details>}
        {events.length > 0 && <details className="agent-activity-list"><summary><Icon name="tree" size={14} />已发生的处理动作<span>{historyLoading ? "正在补齐历史 · " : ""}{events.length}{events.length === 40 ? " 条近期记录" : " 条"}</span><Icon name="chevron-down" size={13} /></summary>
          <ol>{events.map((event, index) => <li key={`${event.sequence ?? index}:${index}`}><span className="agent-activity-dot" aria-hidden="true" /><span>{activityLabel(event)}</span></li>)}</ol>
        </details>}
        {task?.analysis_policy?.status === "SKIPPED" && <p className="agent-policy-note">符合性分析未执行：{task.analysis_policy.reason === "USER_SKIPPED" ? "本次已选择跳过" : "未提供内部依据"}。原文整理和人工响应仍可继续。</p>}
        <div ref={historyHostRef} className="agent-legacy-records" />
        {mode === "extract" && !task && <fieldset className="agent-task-options"><legend>本次处理</legend>
          <p className="agent-policy-note">默认由 Agent 自主阅读、整理、自查和修订，持续保存成果；可随时补充意见。</p>
          <label>内部知识依据 <select value={analysisMode} onChange={e => setAnalysisMode(e.target.value as "AUTO" | "SKIP")}>
            <option value="AUTO">使用项目已登记资料；没有则跳过符合性分析</option><option value="SKIP">本次不做符合性分析</option></select></label>
          <label className="agent-confirm-option"><input type="checkbox" checked={confirmMap} onChange={e => setConfirmMap(e.target.checked)} />先核对文档结构，确认后继续</label>
        </fieldset>}
  </AgentPanelFrame>;
  return <>{!embedded && <>
    <details ref={disclosure} open={expanded} className="native-hierarchy-status-details source-agent-loop"
      data-active={active} onToggle={event => setExpanded(event.currentTarget.open)}
      onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); closePanel(); } }}>
      <summary aria-controls={`${id}-panel`} aria-expanded={expanded}
        onClick={() => { if (!expanded) focusComposerRequested.current = true; }}>
        <Icon name="spark" size={15} /><span>Agent 协作</span>
        {active && <span className="agent-trigger-state"><i aria-hidden="true" />处理中</span>}
        <Icon name="chevron-down" size={13} />
      </summary>
    </details>
    </>}{embedded ? panel : createPortal(panel, document.body)}</>;
}
