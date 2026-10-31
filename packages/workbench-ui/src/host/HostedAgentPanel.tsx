import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AgentPanelFrame } from "../features/workspace/AgentPanelFrame";
import type { DocumentAgentBinding } from "../features/workspace/DocumentAgentContext";
import { useApplicationHost, type ApplicationTaskReceipt } from "./ApplicationHost";
import { FixedWorkflowStatus, FixedWorkflowSummary } from "./FixedWorkflowStatus";

const executionLabels: Record<string, string> = { ACCEPTED: "已受理", RUNNING: "正在处理", WAITING: "等待中", SUCCEEDED: "本轮已结束", FAILED: "本轮未完成", INTERRUPTED: "已暂停", IDLE: "空闲" };
const executionLabel = (receipt: ApplicationTaskReceipt) => executionLabels[receipt.execution?.status || receipt.status] || "状态尚未确认";

/** Only host execution controls differ; saved business work continues in the shared external-work panel. */
export function HostedAgentPanel({ projectId, documentId, documentName, sourceSha256, workRef, revisionId, selectedRevisionId, binding, initialGoal, onAccepted, onClose }: {
  projectId: string; documentId: string; documentName?: string; sourceSha256: string; workRef?: string; initialGoal: string;
  revisionId?: string; binding?: DocumentAgentBinding | null;
  /** Invalidate pending operations as soon as the route changes, before its reader finishes loading. */
  selectedRevisionId?: string;
  /** Explicit start or return navigation; background reads and controls preserve the current view. */
  onAccepted: (receipt: ApplicationTaskReceipt) => void; onClose: () => void;
}) {
  const host = useApplicationHost()!;
  const scope = useMemo(() => ({ projectId, documentId, sourceSha256, ...(workRef ? { workRef } : {}) }), [projectId, documentId, sourceSha256, workRef]);
  const selection = useMemo(() => ({ host, scope, revisionId, selectedRevisionId }), [host, scope, revisionId, selectedRevisionId]);
  type Selection = typeof selection;
  const [goal, setGoal] = useState(initialGoal || "");
  const [requireMapConfirmation, setRequireMapConfirmation] = useState(false);
  const [result, setResult] = useState<{ selection: Selection; receipt: ApplicationTaskReceipt | null } | null>(null);
  const [busySelection, setBusySelection] = useState<Selection | null>(null);
  const [failure, setFailure] = useState<{ selection: Selection; message: string } | null>(null);
  const edited = useRef(false);
  const activeSelection = useRef<Selection | null>(null);
  const pendingAction = useRef<{ selection: Selection } | null>(null);
  const observation = useRef<{ selection: Selection; controller: AbortController } | null>(null);
  const operationRevision = useRef(0);
  const busy = busySelection === selection;
  const error = failure?.selection === selection ? failure.message : "";
  useLayoutEffect(() => {
    activeSelection.current = selection;
    return () => {
      if (activeSelection.current === selection) activeSelection.current = null;
      if (pendingAction.current?.selection === selection) pendingAction.current = null;
      if (observation.current?.selection === selection) { observation.current.controller.abort(); observation.current = null; }
    };
  }, [selection]);
  useEffect(() => {
    let disposed = false, timer: ReturnType<typeof setTimeout> | undefined;
    const current = () => !disposed && activeSelection.current === selection;
    async function refresh() {
      if (!current()) return;
      const observedRevision = operationRevision.current;
      if (pendingAction.current?.selection !== selection) {
        const request = { selection, controller: new AbortController() };
        observation.current = request;
        try {
          const value = await host.restoreTask(scope, { signal: request.controller.signal });
          if (current() && observedRevision === operationRevision.current) {
            setResult({ selection, receipt: value }); const draft = host.taskDraft(scope);
            if (!edited.current && draft) { setGoal(draft.goal); setRequireMapConfirmation(draft.requireMapConfirmation); }
          }
        } catch (cause) {
          if (current() && observedRevision === operationRevision.current) setFailure({ selection, message: cause instanceof Error ? cause.message : "原任务状态暂不可读取，请重试。" });
        } finally { if (observation.current === request) observation.current = null; }
      }
      if (current()) timer = setTimeout(refresh, 3000);
    }
    void refresh(); return () => { disposed = true; if (timer !== undefined) clearTimeout(timer); };
  }, [selection, host, scope]);
  const receipt = result?.selection === selection ? result.receipt : null;
  const bound = receipt && (!workRef || receipt.workRef === workRef) ? receipt : null;
  async function run(action?: "cancel" | "continue" | "reconnect") {
    if (activeSelection.current !== selection || pendingAction.current?.selection === selection || action && !bound) return;
    const operation = { selection };
    pendingAction.current = operation; operationRevision.current += 1;
    if (observation.current?.selection === selection) observation.current.controller.abort();
    setBusySelection(selection); setFailure(null);
    try {
      const next = action && bound ? await host.controlTask(bound, action) : await host.startTask({ ...scope, goal: goal.trim(), requireMapConfirmation });
      if (activeSelection.current === selection && pendingAction.current === operation) {
        setResult({ selection, receipt: next });
        if (!action) onAccepted(next);
      }
    } catch (cause) {
      if (activeSelection.current === selection && pendingAction.current === operation) setFailure({ selection, message: cause instanceof Error ? cause.message : "无法确认原任务，请保留请求并重试。" });
    } finally {
      if (activeSelection.current === selection && pendingAction.current === operation) { pendingAction.current = null; setBusySelection(null); }
    }
  }
  const setupMessage = host.taskSetup ? {
    CAPABILITY_NOT_READY: "业务能力包尚未就绪：请检查配套安装和服务状态。",
    FIXED_WORKFLOW_UNAVAILABLE: "当前能力包不提供固定业务流程，请安装配套版本。",
    PAIR_DEFAULT_MODEL_UNAVAILABLE: "PAIR 尚未提供可用默认模型：请在 PAIR 设置 → 模型中选择模型和推理档位。",
    PAIR_SESSION_MODEL_SELECTION_UNSUPPORTED: "当前 PAIR 会话不支持模型选择，请更新 PAIR 宿主。",
    PAIR_SESSION_MODEL_UNAVAILABLE: "PAIR 未能选择可用模型：请在 PAIR 设置 → 模型中选择可用模型和推理档位。",
    PAIR_SESSION_SERVICE_UNAVAILABLE: "PAIR 会话服务不可用，请检查宿主服务。",
    TASK_START_NOT_CONFIGURED: "PAIR 任务启动尚未配置：请确认能力包已安装并已选择默认模型。",
  }[host.taskSetup] || host.taskSetup : "";
  const controls = <div className="agent-control-actions">
    {bound && <button type="button" className="button secondary compact" disabled={busy} onClick={() => {
      const selected = workRef === bound.workRef && revisionId && binding?.revisionId === revisionId ? binding : null;
      void host.openSession(bound.sessionId, { projectId, documentId, sourceSha256, workRef: bound.workRef, revisionId,
        scope: selected ? "selected" : "document", ...(selected ? { nodeId: selected.selectedNodeId, sourceItemId: selected.selectedSourceItemId || undefined, derivedUnitId: selected.selectedDerivedUnitId } : {})
      }).catch(cause => { if (activeSelection.current === selection) setFailure({ selection, message: String(cause.message || cause) }); });
    }}>打开 PAIR 会话</button>}
    {bound?.controlCapabilities?.cancel && <button type="button" className="button secondary compact" disabled={busy} onClick={() => void run("cancel")}>停止本轮</button>}
    {bound?.controlCapabilities?.continue && <button type="button" className="button secondary compact" disabled={busy} onClick={() => void run("continue")}>继续同一任务</button>}
    {bound?.controlCapabilities?.reconnect && <button type="button" className="button secondary compact" disabled={busy} onClick={() => void run("reconnect")}>核对并重连原任务</button>}
    {error && <p className="agent-inline-notice is-error" role="alert">{error}</p>}
  </div>;
  if (workRef) return <section aria-label="PAIR 会话连接" className="agent-inline-notice">
    <strong>PAIR 会话</strong><p>{bound ? `已关联原任务 · ${executionLabel(bound)}` : "尚未开始；当前成果未关联此工作台保存的 PAIR 会话。"}</p>
    {bound && <><FixedWorkflowStatus state={bound.workflowState} unconfirmed={bound.workflowUnconfirmed} /><p>运行状态：{executionLabel(bound)}；业务成果状态单独以上述已保存阶段为准。</p></>}
    {controls}
  </section>;
  return <AgentPanelFrame onClose={onClose} footer={controls}>
    <section className="agent-dock-brief"><h3>按固定流程整理当前客户文档</h3><p>{host.modelLabel}。用户只需开始一次；可选补充说明不会取消固定阶段或输出检查。</p>
      <FixedWorkflowSummary workflow={host.workflow} />
      {!host.taskStartAvailable && <p role="status">{setupMessage || "当前宿主尚未配置本应用的任务启动能力；原件与已保存成果仍可核对。"}</p>}
      {bound ? <><p role="status">已找到原任务：{executionLabel(bound)}</p><FixedWorkflowStatus state={bound.workflowState} unconfirmed={bound.workflowUnconfirmed} />
          <p role="status">运行状态与业务成果分别核对；已保存阶段不代表人工接受。</p>
          <button className="button secondary" type="button" disabled={busy} onClick={() => onAccepted(bound)}>回到原任务成果</button></>
        : <><p role="status">{error ? "原任务状态暂时无法确认，请保留当前说明并重试。" : "尚未开始"}</p>
          {documentName ? <p>当前客户文档：{documentName}</p> : null}
          <label>补充说明（可选）<textarea aria-label="补充说明（可选）" value={goal} maxLength={12000} placeholder="例如：本次重点关注试验条件；不填写也会执行固定流程。" onChange={event => { edited.current = true; setGoal(event.target.value); }} disabled={busy} rows={5} /></label>
          <label className="agent-confirm-option"><input type="checkbox" checked={requireMapConfirmation} disabled={busy} onChange={event => { edited.current = true; setRequireMapConfirmation(event.target.checked); }} />先核对结构，再继续整理条目</label>
          <button type="button" className="button primary" disabled={busy || !host.taskStartAvailable} onClick={() => void run()}>{busy ? "正在确认原请求…" : "开始固定流程"}</button></>}
    </section>
  </AgentPanelFrame>;
}
