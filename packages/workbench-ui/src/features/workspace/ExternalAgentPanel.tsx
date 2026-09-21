import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "../../shared/ui/Icon";
import { clearSourceFeedbackDraft, sourceFeedbackDraftKey, useSourceFeedbackDraft } from "../extraction/sourceItemDrafts";
import type { SourceStage } from "../extraction/SourceDocumentStageResults";
import type { DocumentAgentBinding } from "./DocumentAgentContext";
import { AgentPanelFrame } from "./AgentPanelFrame";
import { externalExecutionLabel, pluginFeedbackLabel } from "./documentWorkProvider";
import type { PluginWorkbenchProvider } from "./usePluginWorkbench";

export function ExternalAgentPanel({ provider, projectId, documentId, documentName, binding, onClose, onViewStage, openRequest, requestedScope }: {
  provider: PluginWorkbenchProvider; projectId: string; documentId: string; documentName: string;
  binding: DocumentAgentBinding | null; onClose: () => void;
  onViewStage: (stage: SourceStage, revision?: string) => void; openRequest: number;
  requestedScope?: { id: number; scope: "selected" | "document" };
}) {
  const { state, pinned, version } = provider;
  const [scope, setScope] = useState<"selected" | "document">("selected");
  const hasSelection = Boolean(binding?.selectedSourceItemId || binding?.selectedNodeId);
  const selected = scope === "selected" && hasSelection;
  const sourceIds = selected && binding?.selectedSourceItemId ? [binding.selectedSourceItemId] : [];
  const nodeId = selected ? binding?.selectedNodeId || "" : "";
  const target = selected ? binding?.selectedLabel || "当前原文范围" : "整个文档";
  const draftKey = sourceFeedbackDraftKey(projectId, documentId, `external:${state?.session_id || "pending"}:${sourceIds.join(",") || nodeId || "document"}`, pinned || "pending");
  const draft = useSourceFeedbackDraft(draftKey, version?.number || 0);
  const activeDraft = useRef(draftKey); activeDraft.current = draftKey;
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const input = useRef<HTMLTextAreaElement>(null), id = useId();
  useEffect(() => { if (openRequest) input.current?.focus(); }, [openRequest]);
  useEffect(() => { if (requestedScope) { setScope(requestedScope.scope); input.current?.focus(); } }, [requestedScope?.id, requestedScope?.scope]);
  useEffect(() => { setError(""); setNotice(""); }, [draftKey]);
  const capability = provider.capability("save_feedback");
  const save = async () => {
    if (!draft.text.trim() || provider.pending || !capability.enabled || !provider.reader) return;
    const sent = draft.text, key = draftKey;
    setError(""); setNotice("");
    try {
      await provider.saveFeedback(sent, nodeId, sourceIds);
      if (activeDraft.current !== key) return;
      clearSourceFeedbackDraft(key, sent);
      setNotice(`意见已保存到 v${version?.number || ""} · ${target}，待外部任务取回。`);
    } catch (cause) { if (activeDraft.current === key) setError(cause instanceof Error ? cause.message : "保存未完成，请重试。"); }
  };
  const copyBrief = async () => {
    const text = `继续当前 ReqPilotX 外部任务。文档：${documentName}；正在核对 v${version?.number || "未提供"}。请在原任务中取回工作台已保存的确认和反馈，核对其来源/版本后继续原范围，保存修订成果。\n工作台：${window.location.href}`;
    try { await navigator.clipboard.writeText(text); setNotice("接续说明已复制；请回到原外部任务继续。此操作未启动模型。"); }
    catch { setError("暂时无法复制，请使用当前工作台链接回到原外部任务继续。"); }
  };
  const visibleVersions = new Set(state?.versions.filter(v => v.number <= (version?.number || 0)).map(v => v.revision_id));
  const feedback = state?.feedback.filter(item => visibleVersions.has(item.expected_revision_id)) || [];
  const lastSync = state?.last_synced_at || state?.updated_at;
  return <AgentPanelFrame onClose={onClose} footer={<>
    <div className="agent-context-chips" aria-label="任务上下文"><span><Icon name="document" size={13}/>{documentName}</span><span>{version ? `核对版 v${version.number}` : state ? "来源已绑定" : "正在同步工作"}</span><span>{target}</span></div>
    <div className="agent-composer"><label htmlFor={`${id}-feedback`}>给外部任务的修改意见</label>
      <textarea ref={input} id={`${id}-feedback`} rows={3} value={draft.text} maxLength={12000} onChange={event => draft.update(event.target.value)} placeholder="记录要核对或修改的内容；保存后由原外部任务取回…" />
      <div className="agent-composer-toolbar"><label className="source-agent-loop-scope">修改范围<select value={selected ? "selected" : "document"} onChange={event => setScope(event.target.value as typeof scope)}>
        {hasSelection && <option value="selected">{binding?.selectedSourceItemId ? "当前条款" : "当前结构范围"}</option>}<option value="document">整个文档</option></select></label>
        <button type="button" className="button primary compact" disabled={!capability.enabled || !provider.reader || !draft.text.trim() || provider.pending} onClick={() => void save()}>{provider.pending ? "正在保存…" : "保存意见"}<Icon name="arrow" size={14}/></button>
      </div>
    </div>
    <div className="agent-composer-footnote"><span>{!draft.durable ? "本机暂存不可用，输入暂留在当前页面" : draft.text ? "草稿 · 尚未发送" : "保存意见不会唤醒或新建模型任务"}</span></div>
    {error && <p className="agent-inline-notice is-error" role="alert">{error}</p>}{notice && <p className="agent-inline-notice" role="status">{notice}</p>}
    {!capability.enabled && <p className="agent-policy-note">{capability.reason || "当前版本暂不可提交意见。"}</p>}
  </>}>
    <section className="agent-task-state" data-active={false} aria-label="当前任务状态"><span className="agent-state-mark"><Icon name="spark" size={17}/></span><div>
      <span className="agent-task-status" role="status">{state ? externalExecutionLabel(state) : "正在读取外部任务信息"}</span><h3>当前文档任务</h3>
      <p>成果由外部任务保存；此处展示已同步内容与意见回执。</p>
      {lastSync && <time className="agent-task-update" dateTime={lastSync}>最近同步 {new Date(lastSync).toLocaleString("zh-CN")}</time>}
      <details className="agent-task-goal"><summary>任务目标</summary><p>{state?.goal?.trim() || "外部任务未提供目标文本；已有成果和反馈继续保留。"}</p></details>
    </div></section>
    {state?.map_confirmation_required && <section className="agent-stage-results" aria-label="需要你处理"><strong>需要核对结构</strong><p>确认指定版本后，外部任务取回确认才会继续。</p>
      {state.stages.filter(stage => stage.stage === "MAP").map(stage => <button key={stage.stage} className="button secondary compact" onClick={() => onViewStage(stage, state.revision_id || undefined)}>查看待确认结构</button>)}
    </section>}
    {version && <section className="agent-stage-results" aria-label="阶段成果"><strong>当前核对版成果</strong><div className="agent-stage-result-list">{version.stages.map(stage => <button type="button" className="agent-stage-result" key={stage.stage} onClick={() => onViewStage(stage, pinned || undefined)}><Icon name={stage.stage === "MAP" ? "tree" : "requirement"} size={16}/><span>{stage.stage === "MAP" ? "文档结构" : "原文条目与分类"}<small>v{version.number} · 已保存</small></span><em>{stage.count} 项</em></button>)}</div></section>}
    <section className="external-feedback-list" aria-label="意见与修订记录"><strong>意见与修订记录</strong>
      {!feedback.length ? <p className="muted">{state && !state.revision_id ? "阶段成果尚未保存，形成后可围绕成果记录意见。" : !state || !version ? "正在读取当前核对版的意见记录…" : "当前核对版尚无已保存意见。"}</p> : feedback.map(item => <article key={item.feedback_id}><span>{pluginFeedbackLabel(state!, item, pinned)}</span><p>{item.text}</p><small>{item.target_label || "原文范围"} · 来源 v{state?.versions.find(v => v.revision_id === item.expected_revision_id)?.number || "—"}</small>
        {state?.feedback_resolutions?.[item.feedback_id] && visibleVersions.has(state.feedback_resolutions[item.feedback_id].revision_id) && <details><summary>修订说明</summary><p>{state.feedback_resolutions[item.feedback_id].reason}</p></details>}
      </article>)}
    </section>
    <details className="external-handoff" aria-label="外部任务接续"><summary>需要回到原任务时</summary><p>可复制当前工作的链接与接续说明，在原任务中打开；已有成果和意见继续保留。</p><button type="button" className="button secondary compact" onClick={() => void copyBrief()}>复制接续说明</button></details>
  </AgentPanelFrame>;
}
