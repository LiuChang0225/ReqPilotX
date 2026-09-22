import { useCallback, useEffect, useRef, useState, type MutableRefObject } from "react";
import { asArray, asRecord, request } from "../../shared/api/client";
import type { JsonRecord, RequirementMatrixNode } from "../../shared/api/contracts";
import { clearScopedUiStateIfUnchanged, useScopedUiState } from "../../shared/hooks/useScopedUiState";
import { classificationLabels } from "../matrix/classificationFacets";
import type { MatrixMutationRequest } from "../matrix/RequirementMatrixWorkbench";

type SaveState = "" | "未保存" | "保存中…" | "保存失败，请重试" | `已保存 ${string}`;
interface ReviewState { revision: number; sourceRevision: string; writeToken: string; targets: Record<string, JsonRecord> }
export function CandidateReviewInspector({ node, topics, saveRef, onSaveState, onSaved, resetSignal, beginMutation, isMutationCurrent, isTargetCurrent }: {
  node: RequirementMatrixNode | null; topics: Record<string, string>;
  saveRef: MutableRefObject<(() => Promise<boolean>) | null>; onSaveState: (state: SaveState) => void;
  onSaved: () => Promise<void>; resetSignal: number;
  beginMutation: () => MatrixMutationRequest;
  isMutationCurrent: (request: MatrixMutationRequest) => boolean;
  isTargetCurrent: (target: string) => boolean;
}) {
  const meta = asRecord(node?.candidate_review);
  const target = node?.node_id || "";
  const draftKey = `candidate-feedback:${meta.sourceRevision}:${target}`;
  const [draft, setDraft] = useScopedUiState(draftKey, "");
  const latestDraft = useRef(draft); latestDraft.current = draft;
  const [state, setState] = useState<ReviewState | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState("");
  const active = useRef(target); active.current = target;
  const consumedResetSignal = useRef(resetSignal);
  useEffect(() => () => { active.current = ""; }, []);
  const load = useCallback(async () => {
    try { const next = await request<ReviewState>("/api/review-state"); setState(current => !current || next.revision >= current.revision ? next : current); setError(""); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  }, []);
  useEffect(() => { setPending(false); setMessage(""); void load(); onSaveState(draft ? "未保存" : ""); }, [target, load, onSaveState]);
  useEffect(() => {
    if (consumedResetSignal.current === resetSignal) return;
    consumedResetSignal.current = resetSignal;
    setDraft(""); onSaveState("");
  }, [resetSignal, setDraft, onSaveState]);
  const save = useCallback(async (action: "comment" | "confirm" | "undo" = "comment") => {
    if (!node || !meta.sourceRevision || !state || pending || (action === "comment" && !draft.trim())) return false;
    const selected = target;
    const mutation = beginMutation();
    const lastEvent = asRecord(state.targets[target]).lastEventId;
    setPending(true); setError(""); onSaveState("保存中…");
    try {
      const response = await request<{ state: ReviewState }>("/api/review", { method: "POST",
        headers: { "X-Review-Token": state.writeToken },
        body: JSON.stringify({ mutationId: crypto.randomUUID(), expectedRevision: state.revision,
          sourceRevision: meta.sourceRevision, targetId: target, action,
          note: action === "undo" ? "" : draft, ...(action === "undo" ? { undoOf: lastEvent } : {}) }),
      });
      const cleared = clearScopedUiStateIfUnchanged(draftKey, draft);
      if (active.current === selected) {
        setState(response.state);
        if (latestDraft.current === draft) setDraft("");
        setMessage(action === "undo" ? "已撤销本条最近一次核对操作" : action === "confirm" ? "已记录本条核对确认" : "反馈已保存");
      }
      if (isMutationCurrent(mutation)) {
        if (isTargetCurrent(selected)) onSaveState(cleared ? `已保存 ${new Date().toLocaleTimeString("zh-CN")}` : "未保存");
        await onSaved();
      }
      return true;
    } catch (cause) {
      if (active.current === selected) setError(cause instanceof Error ? cause.message : String(cause));
      if (isMutationCurrent(mutation) && isTargetCurrent(selected)) onSaveState("保存失败，请重试");
      return false;
    } finally { if (active.current === selected) setPending(false); }
  }, [draft, draftKey, meta.sourceRevision, node, onSaveState, onSaved, pending, setDraft, state, target, beginMutation, isMutationCurrent, isTargetCurrent]);
  useEffect(() => { saveRef.current = () => save(); return () => { saveRef.current = null; }; }, [save, saveRef]);
  if (!node || !meta.sourceRevision) return <div className="matrix-inspector-content">选择原文条目或派生项，查看分类并记录反馈。</div>;
  const review = asRecord(state?.targets[target]);
  return <div className="matrix-inspector-content">
    <header className="matrix-inspector-heading"><div><span>原文核对与反馈{node.source_title_role === "AGENT_ASSIGNED" ? " · Agent 拟名/编号" : ""}</span><h2>{node.source_number} {node.display_title}</h2></div></header>
    <section className="matrix-inspector-section"><p className="section-label">需求类别</p>
      <p>{classificationLabels(node).join("；") || `保留内容 · ${String(meta.contentRole || "不单列需求")}`}</p>
      <p className="section-label">主题标签</p><p>{(node.topic_tags || []).map(tag => topics[tag] || tag).join("；") || "—"}</p>
      <p>{String(meta.reason || "")}</p>
    </section>
    {meta.previousDecision ? <section className="matrix-inspector-section" aria-label="前后判定差异">
      <p className="section-label">本次修订 · {String(meta.sourceId || "")} {node.requirement_code}</p>
      <p>前版判定：{String(meta.previousDecision)}</p>
      <details><summary>查看前版判定依据</summary><p>{String(meta.previousReason || "")}</p></details>
      {meta.previousRequirementId && meta.requirementness === "NON_REQUIREMENT" ?
        <p>{String(meta.previousRequirementId)} 保留为历史编号；原文继续保留并可查询。</p> : null}
      {asArray<string>(meta.feedbackSources).length > 0 ? <details><summary>查看反馈来源绑定</summary>
        {asArray<string>(meta.feedbackSources).map(reference => <p key={reference}>{reference}</p>)}
      </details> : null}
    </section> : null}
    {asArray<JsonRecord>(meta.openIssues).length > 0 ? <section className="matrix-inspector-section" aria-label="尚未解决的来源问题">
      <p className="section-label">尚未解决</p>
      {asArray<JsonRecord>(meta.openIssues).map(issue => <p key={String(issue.id)}>{String(issue.summary)}</p>)}
    </section> : null}
    {meta.ownerFeedback ? <section className="matrix-inspector-section"><p className="section-label">已收到的用户反馈</p><p>{String(meta.ownerFeedback)}</p></section> : null}
    <section className="matrix-inspector-section"><label className="field"><span>核对反馈</span>
      <textarea rows={5} value={draft} disabled={pending} placeholder="记录分类、拆分、标签或原文定位的问题…"
        onChange={event => { setDraft(event.target.value); onSaveState(event.target.value ? "未保存" : ""); }} />
    </label><div className="matrix-inspector-actions">
      <button type="button" className="button primary" disabled={!state || pending || !draft.trim()} onClick={() => void save()}>保存反馈</button>
      <button type="button" className="button secondary" disabled={!state || pending} onClick={() => void save("confirm")}>确认本条</button>
      {review.lastEventId ? <button type="button" className="text-button" disabled={pending || Boolean(draft)} onClick={() => void save("undo")}>撤销最近操作</button> : null}
    </div></section>
    {error && <p role="alert">{error} <button type="button" className="text-button" onClick={() => void load()}>重新载入反馈状态</button></p>}
    <p role="status" aria-live="polite">{pending ? "正在保存…" : draft ? "有未保存反馈" : message ||
      (review.status === "reviewed" || meta.status === "reviewed" ? "本条已核对" :
       review.status === "needs-review" || meta.status === "needs-review" ? "反馈已保存，待进一步处理" : meta.ownerFeedback ? "已记录会话反馈，可继续补充" : "可在此记录核对结果")}</p>
    {asArray<JsonRecord>(review.notes).map(note => <p key={String(note.eventId)}>{String(note.text)}</p>)}
  </div>;
}
