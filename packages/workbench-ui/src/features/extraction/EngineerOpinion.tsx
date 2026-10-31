import { useEffect, useRef, useState } from "react";
import { saasStorageKey } from "../../shared/api/saasSession";
import { useUiCopy } from "../../host/HostLocaleContext";

export type EngineerDecision = "COMPLIANT" | "PARTIALLY_COMPLIANT" | "NON_COMPLIANT" | "PENDING_EVIDENCE" | "NEEDS_CLARIFICATION" | "NOT_APPLICABLE";
export type EngineerRecord = { record_id: string; unit_id: string; source_item_id: string; requirement_display_id: string;
  decision: EngineerDecision | ""; reason: string; conditions: string; evidence_requests: string;
  state: "CURRENT" | "STALE" | "WITHDRAWN"; decided_at: string; origin: "USER_INPUT" | "USER_PROVIDED_FILE"; baseline_sha256: string };
export type EngineerResponseView = { available: boolean; response_revision: number; response_sha256: string;
  source_sha256?: string; session_id?: string; revision_id?: string; current_revision_id?: string; version?: string; baseline_sha256?: string;
  business_basis_sha256?: string;
  units: Record<string, { unit_id: string; source_item_id: string; requirement_display_id: string; unit_context_sha256: string }>;
  records: Record<string, EngineerRecord>; history: EngineerRecord[] };
export type EngineerChoice = { action: "SET" | "WITHDRAW"; decision?: EngineerDecision; reason: string; conditions: string; evidence_requests: string };
export const ENGINEER_DECISION_LABELS: Record<EngineerDecision, string> = { COMPLIANT: "符合", PARTIALLY_COMPLIANT: "部分符合", NON_COMPLIANT: "不符合",
  PENDING_EVIDENCE: "待内部证据", NEEDS_CLARIFICATION: "待澄清", NOT_APPLICABLE: "不适用" };
export function engineerDecisionLabel(value: EngineerDecision, l?: (key: string, fallback?: string) => string): string {
  return l ? l(`engineer.decision.${value}`, ENGINEER_DECISION_LABELS[value]) : ENGINEER_DECISION_LABELS[value];
}

export function EngineerOpinion({ unitId, view, onSave, onLoad, defaultExpanded = false, draftScope, suggestion, readOnly = false }: { unitId: string; view?: EngineerResponseView | null;
  readOnly?: boolean;
  defaultExpanded?: boolean; draftScope?: string; suggestion?: { decision: string; reason: string; clarification_question?: string };
  onLoad?: () => Promise<void>;
  onSave: (unitId: string, choice: EngineerChoice) => Promise<void> }) {
  const l = useUiCopy();
  const current = view?.records[unitId];
  const [decision, setDecision] = useState<EngineerDecision>(current?.decision || "PENDING_EVIDENCE");
  const [reason, setReason] = useState(current?.reason || "");
  const [conditions, setConditions] = useState(current?.conditions || "");
  const [evidence, setEvidence] = useState(current?.evidence_requests || "");
  const [saving, setSaving] = useState(false), [message, setMessage] = useState("");
  const [open, setOpen] = useState(defaultExpanded);
  const [draftUnavailable, setDraftUnavailable] = useState(false);
  const businessScope = `${draftScope || ""}:${unitId}:${view?.units[unitId]?.unit_context_sha256 || "unloaded"}`;
  const draftKey = draftScope ? saasStorageKey(`reqpilotx:engineer-draft:${businessScope}`) : "";
  const scopeRef = useRef(businessScope); scopeRef.current = businessScope;
  useEffect(() => {
    let draft: { decision?: EngineerDecision; reason?: string; conditions?: string; evidence?: string } | null = null;
    try { draft = draftKey ? JSON.parse(localStorage.getItem(draftKey) || "null") : null; } catch { setDraftUnavailable(true); }
    setDecision(draft?.decision || current?.decision || "PENDING_EVIDENCE");
    setReason(draft?.reason ?? current?.reason ?? ""); setConditions(draft?.conditions ?? current?.conditions ?? "");
    setEvidence(draft?.evidence ?? current?.evidence_requests ?? ""); setMessage(""); setSaving(false);
  }, [draftKey, unitId, current?.record_id, current?.state]);
  useEffect(() => { if (!readOnly && open && !view && onLoad) void onLoad().catch(error => setMessage(error.message)); }, [readOnly, open, view, onLoad]);
  const change = (patch: Partial<{ decision: EngineerDecision; reason: string; conditions: string; evidence: string }>) => {
    if (saving) return;
    const next = { decision, reason, conditions, evidence, ...patch };
    setDecision(next.decision); setReason(next.reason); setConditions(next.conditions); setEvidence(next.evidence);
    if (draftKey) { try { localStorage.setItem(draftKey, JSON.stringify(next)); setDraftUnavailable(false); } catch { setDraftUnavailable(true); } }
  };
  const history = view?.history.filter(record => record.unit_id === unitId) || [];
  const save = async (action: "SET" | "WITHDRAW") => {
    if (readOnly) return;
    const startedScope = businessScope, sentDraftKey = draftKey;
    const sentDraft = JSON.stringify({ decision, reason, conditions, evidence });
    setSaving(true); setMessage("");
    try { await onSave(unitId, { action, decision, reason, conditions, evidence_requests: evidence });
      let newerDraft = false;
      try {
        const currentDraft = sentDraftKey ? localStorage.getItem(sentDraftKey) : null;
        newerDraft = Boolean(currentDraft && currentDraft !== sentDraft);
        if (sentDraftKey && currentDraft === sentDraft) localStorage.removeItem(sentDraftKey);
      } catch { /* Preserve local input if draft persistence is unavailable. */ }
      if (scopeRef.current === startedScope) setMessage(newerDraft ? l("engineer.previousSavedDraftKept", "上一份意见已保存，后续草稿仍保留。") : action === "SET" ? l("engineer.saved", "工作意见已保存。") : l("engineer.withdrawn", "已撤回，历史意见仍保留。")); }
    catch (error) { if (scopeRef.current === startedScope) setMessage(error instanceof Error ? error.message : l("engineer.saveFailed", "保存失败，请同步后重试。")); }
    finally { if (scopeRef.current === startedScope) setSaving(false); }
  };
  return <section className="engineer-opinion" aria-label={l("engineer.aria", "工程师工作意见")}>
    {current && current.state !== "WITHDRAWN" && <div>
      <strong>{current.state === "STALE" ? l("engineer.stale", "前版工程师意见已失效，需重新确认") : l("engineer.current", "当前工程师工作意见")}</strong>
      <p>{current.decision ? engineerDecisionLabel(current.decision, l) : ""} · {current.reason}</p>
      {current.conditions && <p>{l("engineer.conditions", "适用条件：{value}", { value: current.conditions })}</p>}
      {current.evidence_requests && <p>{l("engineer.evidenceRequests", "证据请求：{value}", { value: current.evidence_requests })}</p>}
    </div>}
    {!readOnly && <details open={open} onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>{current?.state === "CURRENT" ? l("engineer.edit", "修改工程师工作意见") : l("engineer.record", "记录工程师工作意见")}</summary>
      <p className="muted">{l("engineer.saveHint", "保存后作为本版工程师响应，Agent 建议和来源继续保留。")}</p>
      {suggestion && suggestion.decision in ENGINEER_DECISION_LABELS && <button type="button" className="button secondary compact" disabled={saving || suggestion.reason.length > 8000} onClick={() => change({ decision: suggestion.decision as EngineerDecision, reason: suggestion.reason, evidence: suggestion.clarification_question || "" })}>{l("engineer.useSuggestion", "引用 Agent 建议为草稿")}</button>}
      {!view?.available ? <div><p role="status">{view ? l("engineer.storageOff", "工程师响应存储尚未启用，请先完成应用更新。") : l("engineer.readingEntry", "正在读取当前单元的工程师响应入口…")}</p>
        {onLoad && <button type="button" className="button secondary compact" onClick={() => void onLoad().catch(error => setMessage(error.message))}>{l("engineer.reload", "重新读取")}</button>}</div> : <form onSubmit={event => { event.preventDefault(); void save("SET"); }}><fieldset disabled={saving}>
        <label>{l("engineer.decision", "工程师判断")}<select value={decision} onChange={event => change({ decision: event.target.value as EngineerDecision })}>
          {Object.entries(ENGINEER_DECISION_LABELS).map(([value]) => <option value={value} key={value}>{engineerDecisionLabel(value as EngineerDecision, l)}</option>)}
        </select></label>
        <label>{l("engineer.reason", "判断理由")}<textarea value={reason} maxLength={8000} rows={3} onChange={event => change({ reason: event.target.value })} required /></label>
        <details className="engineer-response-extras"><summary>{l("engineer.extras", "补充适用条件与证据请求")}</summary>
        <label>{l("engineer.conditionsLabel", "适用条件")}<textarea value={conditions} maxLength={4000} rows={2} onChange={event => change({ conditions: event.target.value })} /></label>
        <label>{l("engineer.evidenceLabel", "证据请求")}<textarea value={evidence} maxLength={4000} rows={2} onChange={event => change({ evidence: event.target.value })} /></label>
        </details>
        <div className="source-item-delivery-actions">
          <button type="submit" className="button primary compact" disabled={saving || !view.units[unitId] || !reason.trim()}>{saving ? l("common.saving", "正在保存…") : l("engineer.save", "保存为本版工作意见")}</button>
          {current && current.state !== "WITHDRAWN" && <button type="button" className="button secondary compact" disabled={saving} onClick={() => void save("WITHDRAW")}>{l("engineer.withdraw", "撤回人工意见")}</button>}
        </div>
      </fieldset></form>}
    </details>}
    {draftUnavailable && <p role="status">{l("engineer.draftUnavailable", "本机草稿暂存不可用；当前输入保留在此页。")}</p>}
    {message && <p role="status">{message}</p>}
    {history.length > 0 && <details><summary>{l("engineer.history", "历史工程师意见 · {count} 条", { count: history.length })}</summary>
      {history.map((record, index) => <blockquote key={`${record.record_id}:${index}`}>
        <p>{record.decision ? engineerDecisionLabel(record.decision, l) : l("engineer.withdrawnShort", "撤回")} · {record.reason}</p>
        {record.conditions && <p>{l("engineer.conditions", "适用条件：{value}", { value: record.conditions })}</p>}{record.evidence_requests && <p>{l("engineer.evidenceRequests", "证据请求：{value}", { value: record.evidence_requests })}</p>}
        <cite>{record.decided_at} · {l("engineer.previous", "前版工作意见")}</cite>
      </blockquote>)}
    </details>}
  </section>;
}
