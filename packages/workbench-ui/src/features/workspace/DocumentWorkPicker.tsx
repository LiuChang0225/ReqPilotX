import { useEffect, useRef, useState } from "react";
import { Icon } from "../../shared/ui/Icon";
import { useDocumentWorkCatalog, type PluginWorkSummary } from "./documentWorkCatalog";
import type { DocumentWorkSummary } from "./documentWorkSummary";

type Props = { projectId: string; documentId: string; sourceSha256: string; selectedSession?: string; selectedTitle?: string;
  nativeSelected?: boolean; summary: DocumentWorkSummary | null; onSelect: (item: PluginWorkSummary | null) => void };
export function DocumentWorkChoices({ projectId, documentId, sourceSha256, selectedSession, nativeSelected = false, summary, onSelect }: Props) {
  const catalog = useDocumentWorkCatalog(projectId, documentId, sourceSha256);
  return <section className="document-work-choices" aria-label="可选文档工作">
    <div className="work-choices-heading"><strong>选择工作</strong><button type="button" className="button ghost compact" onClick={catalog.refresh} disabled={catalog.loading}>同步列表</button></div>
    <p>每个任务单独保存成果。切换工作会保留各自查看版本和草稿。</p>
    {catalog.loading && <p role="status">正在读取当前文档的工作列表…</p>}
    {catalog.error && <p role="alert">{catalog.error} 当前工作保持不变。</p>}
    <div className="work-choice-list">
      {(catalog.value?.sessions || []).map(item => <button type="button" key={item.session_id} aria-pressed={selectedSession === item.session_id} onClick={() => onSelect(item)}>
        <strong>{item.title || `外部任务 ${item.session_id.slice(-8)}`}</strong>
        <span>{item.revision_id ? `最新成果 v${item.revision_number} · ${item.stages.map(stage => `${stage.stage === "MAP" ? "结构" : "条目"} ${stage.count} 项`).join(" · ")}` : "仅有来源 · 尚无阶段成果"}</span>
        <small>外部任务 · {item.session_id.slice(-8)}{selectedSession === item.session_id ? " · 当前工作" : ""}</small>
      </button>)}
      {catalog.value && !catalog.loading && !catalog.value.sessions.length && <p>当前来源尚无外部任务。</p>}
      <button type="button" aria-pressed={nativeSelected} onClick={() => onSelect(null)}><strong>原生工作</strong><span>{summary?.delivery ? `原文工作版 v${summary.delivery.baseline_revision}` : summary?.task?.task_id ? "已有原生任务" : "尚未开始；打开后可设置任务"}</span><small>应用内 Agent{nativeSelected ? " · 当前工作" : ""}</small></button>
    </div>
    {!!catalog.value?.unavailable.length && <details className="work-unavailable"><summary>暂不可用的工作 · {catalog.value.unavailable.length}</summary>{catalog.value.unavailable.map((item, index) => <p key={`${item.session_id}:${index}`}><strong>{item.session_id.slice(-8)}</strong> · {item.reason}</p>)}</details>}
  </section>;
}

export function DocumentWorkPicker(props: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);
  useEffect(() => setOpen(false), [props.projectId, props.documentId, props.sourceSha256]);
  return <div ref={root} className="document-work-picker" onKeyDown={event => { if (event.key === "Escape" && open) { event.stopPropagation(); setOpen(false); trigger.current?.focus(); } }}>
    <button ref={trigger} type="button" className="work-picker-trigger" aria-label="选择当前文档工作" aria-expanded={open} onClick={() => setOpen(value => !value)}>
      <Icon name="document" size={14}/><span title={props.selectedTitle || ""}>{props.selectedSession ? props.selectedTitle || `外部任务 ${props.selectedSession.slice(-8)}` : "原生工作"}</span><Icon name="chevron-down" size={13}/>
    </button>
    {open && <div className="work-picker-popover"><DocumentWorkChoices {...props} nativeSelected={!props.selectedSession} onSelect={item => { props.onSelect(item); setOpen(false); trigger.current?.focus(); }}/></div>}
  </div>;
}
