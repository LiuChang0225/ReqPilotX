import { useCallback, useEffect, useRef, useState } from "react";
import type { SourceRequirementKindV1, SourceNumberFieldsV1 } from "../../shared/api/contracts";
import { request } from "../../shared/api/client";
import { Icon } from "../../shared/ui/Icon";
import { projectKnowledgeFileUrl, useAnalysisKnowledgeSelection } from "../knowledge/projectKnowledge";
import { useWorkbenchHost } from "../workspace/WorkbenchHostContext";
import { EngineerOpinion, ENGINEER_DECISION_LABELS, type EngineerChoice, type EngineerResponseView } from "./EngineerOpinion";

type Carry = { operation?: string; from_baseline_sha256?: string; retained_parent_ids?: string[];
  stale_parent_ids?: string[]; retained_unit_ids?: string[]; stale_unit_ids?: string[] };
type InputReceipt = { operation?: string; baseline_sha256?: string; origin_input_receipt?: InputReceipt };
type CarryNote = { text: string; origin?: string };
type Citation = { fact_id: string; quote: string };
type Decision = { decision: string; reason: string; clarification_question: string; evidence: Citation[] };
type Child = SourceNumberFieldsV1 & { node_id?: string; singular_requirement_id?: string; requirement_display_id: string; text: string;
  source_excerpts?: { text: string; page?: number }[]; requirement_kinds?: SourceRequirementKindV1[] };
type Job = { job_id: string; status: string; message?: string; progress?: { message?: string }; error?: string };
export type SourceItemDeliveryState = {
  baseline_sha256: string; baseline_revision: number; source_sha256: string;
  source_item_count: number; requirement_count: number; non_requirement_count: number;
  analysis_unit_count: number; assessed_unit_count: number; pending_unit_count: number;
  unassessed_unit_count?: number; awaiting_evidence_unit_count?: number; clarification_unit_count?: number;
  decomposition_job_id: string | null; analysis_job_id: string | null; notices: string[];
  active_jobs: Job[];
  analysis_policy?: { status: string; reason: string } | null;
  engineer_responses?: EngineerResponseView | null;
  business_basis_sha256?: string;
  decomposition: { records: Record<string, Child[]>;
    dispositions?: Record<string, { action: "KEEP" | "UNDO" | "SPLIT"; reason: string; baseline_sha256?: string }>;
    review_scope?: { authorized_count: number; decided_count: number; remaining_count: number; complete: boolean };
    carry_forward?: Carry;
    identity_registry?: Record<string, Record<string, string>> } | null;
  assessments: { records: Record<string, Decision>; carry_forward?: Carry; unit_input_receipts?: Record<string, InputReceipt>; evidence_catalog: Record<string, {
    document_name?: string; document_id: string; source_sha256: string;
    source_ref?: { location?: { page_number?: number } };
  }> } | null;
};

function agentRoot(projectId: string, documentId: string) {
  return `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure`;
}

export function useSourceItemReasoning(projectId: string, documentId: string, baselineSha: string, initialState?: SourceItemDeliveryState | null) {
  const host = useWorkbenchHost();
  const root = agentRoot(projectId, documentId);
  const key = `${root}:${baselineSha}`;
  const knowledgeSelection = useAnalysisKnowledgeSelection(key);
  const [entry, setEntry] = useState<{ key: string; value: SourceItemDeliveryState } | null>(() => initialState?.baseline_sha256 === baselineSha ? { key, value: initialState } : null);
  const state = entry?.key === key ? entry.value : null;
  const setState = useCallback((value: SourceItemDeliveryState | null) => setEntry(value ? { key, value } : null), [key]);
  const [error, setError] = useState("");
  const [pending, setPending] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [refresh, setRefresh] = useState(0);
  const currentKey = useRef(key);
  const manualRead = useRef<Promise<void> | null>(null);
  currentKey.current = key;
  useEffect(() => { setState(initialState?.baseline_sha256 === baselineSha ? initialState : null); setJob(null); setError(""); setPending(""); }, [key, initialState, baselineSha, setState]);
  useEffect(() => {
    if (!baselineSha) return;
    if (initialState?.baseline_sha256 === baselineSha && refresh === 0) return;
    const controller = new AbortController();
    void request<SourceItemDeliveryState>(`${root}/delivery/state`, { signal: controller.signal })
      .then(value => {
        if (controller.signal.aborted) return;
        if (value.baseline_sha256 !== baselineSha) throw new Error("原文工作版已更新，请同步后继续。");
        setState(value);
        if (value.active_jobs?.length) setJob(value.active_jobs[0]);
      }).catch(cause => { if (!controller.signal.aborted) setError(String(cause.message || cause)); });
    return () => controller.abort();
  }, [root, baselineSha, refresh, initialState, setState]);
  useEffect(() => {
    if (!job || !["PENDING", "RUNNING"].includes(job.status)) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void request<Job>(`${root}/requirement-baseline/agent-runs/${encodeURIComponent(job.job_id)}`,
        { signal: controller.signal }).then(next => {
        if (controller.signal.aborted) return;
        setJob(next);
        if (!["PENDING", "RUNNING"].includes(next.status)) {
          setRefresh(value => value + 1);
          if (next.status !== "SUCCEEDED") setError(next.progress?.message || "本次任务未完成，已有结果仍保留。可重试当前操作。");
        }
      }).catch(cause => {
        if (!controller.signal.aborted) setError(`读取任务进度失败：${cause.message}。请刷新状态。`);
      });
    }, 3000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [job, root]);

  const run = useCallback(async (action: "decomposition" | "analysis", sourceItemId?: string) => {
    if (!state || pending || ["PENDING", "RUNNING"].includes(job?.status || "")) return;
    const startedKey = key;
    setPending(action); setError("");
    const payload = {
      command_id: `${action}:${crypto.randomUUID()}`, expected_source_sha256: state.source_sha256,
      expected_baseline_sha256: state.baseline_sha256, operation_namespace: "working",
      publication_mode: "WORKING_PROPOSAL", visual_evidence: true,
      ...(action === "decomposition" ? { explicit_trigger: true, source_item_ids: [sourceItemId] }
        : { decomposition_job_id: state.decomposition_job_id,
            ...(knowledgeSelection.ids !== null ? { knowledge_document_ids: knowledgeSelection.ids } : {}),
            ...(sourceItemId ? { selected_unit_ids: state.decomposition?.records[sourceItemId]?.length
              ? state.decomposition.records[sourceItemId].map(child => child.singular_requirement_id || child.node_id)
              : [sourceItemId] } : {}) }),
    };
    try {
      const result = await request<Job>(`${root}/requirement-baseline/${action}/start`,
        { method: "POST", body: JSON.stringify(payload) });
      if (currentKey.current === startedKey) setJob(result);
    } catch (cause) {
      if (currentKey.current === startedKey) setError(cause instanceof Error ? cause.message : "任务未能启动，请重试。");
    } finally { if (currentKey.current === startedKey) setPending(""); }
  }, [state, pending, job, key, root, knowledgeSelection.ids]);

  const saveEngineerResponse = useCallback(async (unitId: string, choice: EngineerChoice) => {
    const view = state?.engineer_responses, unit = view?.units[unitId];
    if (!state || !view?.available || !unit) throw new Error("当前单元不能记录人工意见，请同步原文版本。");
    const startedKey = key;
    const updated = await request<EngineerResponseView>(`${root}/delivery/engineer-responses`, { method: "POST", body: JSON.stringify({
      command_id: `engineer:${crypto.randomUUID()}`, expected_source_sha256: state.source_sha256,
      expected_baseline_sha256: state.baseline_sha256, expected_response_revision: view.response_revision,
      decisions: [{ unit_id: unitId, expected_unit_context_sha256: unit.unit_context_sha256, ...choice }],
    }) });
    if (currentKey.current === startedKey) {
      setRefresh(value => value + 1);
      if (updated.business_basis_sha256 && state.business_basis_sha256 && updated.business_basis_sha256 !== state.business_basis_sha256) {
        throw new Error("工作意见已保存，依据或建议随后更新，请同步后核对。");
      }
      setEntry(previous => previous?.key === startedKey ? { ...previous, value: { ...previous.value, engineer_responses: updated } } : previous);
    }
  }, [state, key, root]);

  const loadEngineerResponses = useCallback(async () => {
    if (manualRead.current) return manualRead.current;
    if (!state) throw new Error("请先读取当前工作版。");
    const startedKey = key;
    const promise = request<EngineerResponseView>(`${root}/delivery/engineer-responses`).then(updated => {
      if (currentKey.current !== startedKey) return;
      if (updated.business_basis_sha256 && state.business_basis_sha256 && updated.business_basis_sha256 !== state.business_basis_sha256) {
        setRefresh(value => value + 1);
        throw new Error("原文、派生或建议已更新，请同步后重新读取意见入口。");
      }
      setEntry(previous => previous?.key === startedKey ? { ...previous, value: { ...previous.value, engineer_responses: updated } } : previous);
      if (Object.keys(updated.records).length) setRefresh(value => value + 1);
    }).finally(() => { if (manualRead.current === promise) manualRead.current = null; });
    manualRead.current = promise; return promise;
  }, [state, key, root]);

  const exportMatrix = async () => {
    if (!state || pending) return;
    const startedKey = key;
    setPending("export"); setError("");
    try {
      const response = await fetch(`${root}/delivery/export?expected_baseline_sha256=${encodeURIComponent(state.baseline_sha256)}&expected_response_revision=${state.engineer_responses?.response_revision || 0}${state.business_basis_sha256 ? `&expected_business_basis_sha256=${encodeURIComponent(state.business_basis_sha256)}` : ""}`);
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(typeof data.detail === "string" ? data.detail : "导出未完成，请重试。");
      }
      const blob = await response.blob();
      if (currentKey.current !== startedKey) return;
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a"); link.href = objectUrl;
      const filename = response.headers.get("content-disposition")?.split("filename*=UTF-8''")[1];
      link.download = filename ? decodeURIComponent(filename) : `Compliance-Matrix-v${state.baseline_revision}-DRAFT.xlsx`;
      link.click(); setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (cause) {
      if (currentKey.current === startedKey) setError(cause instanceof Error ? cause.message : "导出未完成。");
    } finally { if (currentKey.current === startedKey) setPending(""); }
  };
  return { scopeKey: key, state, error, pending, job, run, exportMatrix, saveEngineerResponse, loadEngineerResponses, knowledgeSelection,
    knowledgeFileUrl: (documentId: string, sha: string) => { const url = projectKnowledgeFileUrl(projectId, documentId, sha); return url ? host.resourceUrl(url) : ""; },
    refresh: () => { setError(""); setRefresh(value => value + 1); },
    busy: Boolean(pending || ["PENDING", "RUNNING"].includes(job?.status || "")) };
}

export type SourceItemReasoningController = ReturnType<typeof useSourceItemReasoning>;

export function SourceItemDeliveryActions({ workflow, compact = false, exportOnly = false }: { workflow: SourceItemReasoningController; compact?: boolean; exportOnly?: boolean }) {
  return <div className="source-item-delivery-actions">
    {!exportOnly && (compact ? <details className="source-review-more-actions"><summary>分析操作</summary><div>
      <p>使用已提供的资料分析；缺少证据的内容保持待补充。</p>
      <button className="button secondary compact" disabled={!workflow.state || workflow.busy} onClick={() => void workflow.run("analysis")}>分析全部需求</button>
    </div></details> : <button className="button secondary compact" disabled={!workflow.state || workflow.busy}
      onClick={() => void workflow.run("analysis")}>分析全部需求</button>)}
    <button className="button secondary compact" disabled={!workflow.state || workflow.pending === "export"}
      onClick={() => void workflow.exportMatrix()}><Icon name="download" size={14} />
      {workflow.pending === "export" ? "正在导出…" : "导出 Matrix 草稿"}</button>
  </div>;
}

export function SourceItemReasoningStatus({ workflow, showSummary = true }: { workflow: SourceItemReasoningController; showSummary?: boolean }) {
  const { state, job } = workflow;
  return <div className="source-item-reasoning-status">
    {state && showSummary && <span>全局：{state.analysis_unit_count} 个分析单元 · 已分析 {state.assessed_unit_count}
      {state.unassessed_unit_count !== undefined ? <> · 待分析 {state.unassessed_unit_count} · 待内部证据 {state.awaiting_evidence_unit_count || 0} · 待澄清 {state.clarification_unit_count || 0}</> : <> · 未关闭 {state.pending_unit_count}</>}</span>}
    {job && ["PENDING", "RUNNING"].includes(job.status) && <span role="status">{job.progress?.message || job.message || "Agent 正在处理，已有结果可继续查看。"}</span>}
    {state?.notices.map(notice => <span key={notice}>{notice}</span>)}
    {workflow.error && <span role="alert">{workflow.error} <button type="button" className="button ghost compact" onClick={workflow.refresh}>刷新状态</button></span>}
  </div>;
}

export function decisionLabel(value: string) {
  return ({ COMPLIANT: "符合建议", PARTIALLY_COMPLIANT: "部分符合建议", NON_COMPLIANT: "不符合建议",
    PENDING_EVIDENCE: "待内部证据", NEEDS_CLARIFICATION: "待澄清", NOT_APPLICABLE: "不适用建议", UNKNOWN: "待分析" })[value] || value;
}

function originalReceipt(receipt: InputReceipt): InputReceipt {
  let origin = receipt;
  while (origin.origin_input_receipt) origin = origin.origin_input_receipt;
  return origin;
}

function CarryNotes({ notes }: { notes: CarryNote[] }) {
  if (!notes.length) return null;
  return <aside aria-label="版本继承与待重评">{notes.map((note, index) => <div key={`${note.text}:${index}`}>
    <p>{note.text}</p>
    {note.origin && <details><summary>查看来源基线</summary><p>{note.origin}</p></details>}
  </div>)}</aside>;
}

export function SourceDispositionNote({ state, sourceItemId }: { state: SourceItemDeliveryState | null; sourceItemId: string }) {
  const disposition = state?.decomposition?.dispositions?.[sourceItemId];
  const scope = state?.decomposition?.review_scope;
  if (!disposition && !scope) return null;
  return <details className="source-disposition-note"><summary>拆分处置记录</summary>
    <p>{disposition ? `${disposition.baseline_sha256 && disposition.baseline_sha256 !== state?.baseline_sha256 ? "前版记录，需核对 · " : ""}${{KEEP:"保留整体",UNDO:"撤销派生",SPLIT:"形成派生"}[disposition.action]}：${disposition.reason || "尚未提供理由"}` : "本版未记录此条款的处置理由。"}</p>
    {scope && <p>本轮已处置 {scope.decided_count} / {scope.authorized_count} 个授权父项 · 待处理 {scope.remaining_count}{scope.complete ? " · 授权范围已完成处置" : " · 授权范围尚未完成"}</p>}
  </details>;
}

export function SourceItemReasoningDetail({ workflow, sourceItemId, requirementId, onPageChange, reviewMode = false, matchingUnitIds, selectedUnitId, onSelectUnit, showUnitPicker = true, showUnitText = true, readOnly = false, engineerReadOnly = readOnly, purpose }: {
  workflow: SourceItemReasoningController; sourceItemId: string; requirementId: string;
  onPageChange: (page: number) => void;
  readOnly?: boolean; engineerReadOnly?: boolean; reviewMode?: boolean; showUnitPicker?: boolean; showUnitText?: boolean; matchingUnitIds?: string[]; selectedUnitId?: string; onSelectUnit?: (unitId: string) => void;
  purpose?: "review" | "analysis";
}) {
  const state = workflow.state;
  const [selectedUnit, setSelectedUnit] = useState("");
  const children = state?.decomposition?.records[sourceItemId] || [];
  const splitCarry = state?.decomposition?.carry_forward;
  const splitStale = splitCarry?.operation === "SOURCE_VALIDATED_REBIND" && Boolean(splitCarry.stale_parent_ids?.includes(sourceItemId));
  const parentNotes: CarryNote[] = [];
  if (splitStale) parentNotes.push({ text: "前版派生未通过本版来源与分类一致性核验，需检查拆分；旧派生保留在历史版本。", origin: splitCarry?.from_baseline_sha256 });
  else if (children.length && splitCarry?.operation === "SOURCE_VALIDATED_REBIND" && splitCarry.retained_parent_ids?.includes(sourceItemId)) {
    parentNotes.push({ text: "沿用前版派生；本轮未重新拆分。", origin: splitCarry.from_baseline_sha256 });
  }
  const assessmentCarry = state?.assessments?.carry_forward;
  const staleUnits = assessmentCarry?.operation === "SOURCE_VALIDATED_REBIND" ? assessmentCarry.stale_unit_ids || [] : [];
  const historicalChildren = state?.decomposition?.identity_registry?.[sourceItemId] || {};
  for (const uid of staleUnits) {
    if (historicalChildren[uid] && !children.some(child => (child.singular_requirement_id || child.node_id) === uid) && !state?.assessments?.records[uid]) {
      parentNotes.push({ text: `${historicalChildren[uid]}：前版分析未通过本版输入一致性核验，需重新分析；旧建议保留在历史版本。`, origin: assessmentCarry?.from_baseline_sha256 });
    }
  }
  const units = children.length ? children.map(child => ({ ...child, unitId: child.singular_requirement_id || child.node_id || "" }))
    : [{ unitId: sourceItemId, requirement_display_id: requirementId, text: "", source_excerpts: [] }];
  const queueUnits = !reviewMode && matchingUnitIds?.length ? units.filter(unit => matchingUnitIds.includes(unit.unitId)) : units;
  const selection = selectedUnitId ?? selectedUnit;
  const activeUnit = queueUnits.some(unit => unit.unitId === selection) ? selection : queueUnits[0]?.unitId;
  return <section className={`source-item-reasoning-detail${reviewMode ? "" : " is-response-analysis"}`} aria-label={reviewMode ? "本条拆分结果" : "分析单元与响应建议"}>
    <header><strong>{reviewMode ? children.length ? `已有派生 · ${children.length} 项` : "本条拆分" : "分析单元与响应建议"}</strong><div className="source-item-delivery-actions">
      {purpose !== "analysis" && <button className="button secondary compact" disabled={readOnly || !state || workflow.busy}
        onClick={() => void workflow.run("decomposition", sourceItemId)}>{!showUnitPicker && children.length ? "检查父条款拆分" : "检查本条拆分"}</button>}
      {purpose !== "review" && <button className="button secondary compact" disabled={readOnly || !state || workflow.busy}
        onClick={() => void workflow.run("analysis", sourceItemId)}>{!showUnitPicker && children.length ? "分析父条款派生项" : "分析本条"}</button>}
    </div></header>
    <CarryNotes notes={parentNotes} />
    <SourceDispositionNote state={state} sourceItemId={sourceItemId} />
    {children.length > 0 ? showUnitPicker && <p className="muted">保留完整父项与共同条件；以下是已形成的拆分结果。</p> : reviewMode && !splitStale ? <p className="muted">当前保留为一个完整原文条目，未另行派生。</p> : null}
    {!reviewMode && showUnitPicker && units.length > 1 && <><p className="muted">本条 {units.length} 个分析单元 · 当前队列匹配 {queueUnits.length} 个</p><div className="analysis-unit-tabs" role="group" aria-label="选择分析单元">{queueUnits.map(unit => <button type="button" key={unit.unitId} aria-pressed={unit.unitId === activeUnit} onClick={() => { setSelectedUnit(unit.unitId); onSelectUnit?.(unit.unitId); }}>{unit.requirement_display_id}</button>)}</div></>}
    {units.filter(unit => reviewMode && showUnitPicker || unit.unitId === activeUnit).map(unit => {
      const decision = state?.assessments?.records[unit.unitId];
      const engineer = state?.engineer_responses?.records[unit.unitId];
      const receipt = state?.assessments?.unit_input_receipts?.[unit.unitId];
      const notes: CarryNote[] = [];
      if (decision && receipt?.operation === "SOURCE_VALIDATED_REBIND") {
        notes.push({ text: "沿用前版分析建议；本轮未重新分析。", origin: originalReceipt(receipt).baseline_sha256 });
      } else if (!decision && staleUnits.includes(unit.unitId)) {
        notes.push({ text: "前版分析未通过本版输入一致性核验，需重新分析；旧建议保留在历史版本。", origin: assessmentCarry?.from_baseline_sha256 });
      }
      return <article key={unit.unitId}>
        <strong>{unit.requirement_display_id}{engineer?.state === "CURRENT" && engineer.decision
          ? ` · 工程师工作意见：${ENGINEER_DECISION_LABELS[engineer.decision]}`
          : decision ? ` · ${decisionLabel(decision.decision)}` : state?.analysis_policy?.status === "SKIPPED" ? " · 符合性分析未执行" : reviewMode ? "" : " · 待分析"}</strong>
        <CarryNotes notes={notes} />
        {showUnitText && unit.text && <p>{unit.text}</p>}
        {unit.source_excerpts?.length ? <details><summary>派生依据</summary>{unit.source_excerpts.map((part, index) =>
          <p key={index}>{part.text}{part.page ? <button type="button" onClick={() => onPageChange(part.page!)}>P{part.page}</button> : null}</p>)}</details> : null}
        {!reviewMode && purpose !== "review" && <EngineerOpinion unitId={unit.unitId} view={state?.engineer_responses} onSave={workflow.saveEngineerResponse} onLoad={workflow.loadEngineerResponses} readOnly={engineerReadOnly}
          defaultExpanded draftScope={workflow.scopeKey} suggestion={decision} />}
        {decision && <div className="analysis-suggestion"><strong>{engineer?.state === "CURRENT" ? "AI分析建议（保留）" : "Agent 分析建议"}</strong><details className="analysis-rationale" open={reviewMode}><summary>判断依据</summary><p>{decision.reason}</p></details>{decision.clarification_question && <p><strong>待补充：</strong>{decision.clarification_question}</p>}
          {decision.evidence.map((citation, index) => {
            const fact = state?.assessments?.evidence_catalog[citation.fact_id];
            const sourceUrl = fact && workflow.knowledgeFileUrl?.(fact.document_id, fact.source_sha256);
            return <blockquote key={`${citation.fact_id}:${index}`}><p>{citation.quote}</p><cite>
              {sourceUrl ? <a href={`${sourceUrl}${fact?.source_ref?.location?.page_number ? `#page=${fact.source_ref.location.page_number}` : ""}`} target="_blank" rel="noreferrer">{fact?.document_name || "打开引用原件"}{fact?.source_ref?.location?.page_number ? ` · P${fact.source_ref.location.page_number}` : ""}</a> : <>{fact?.document_name || fact?.document_id || "内部资料"}{fact?.source_ref?.location?.page_number ? ` · P${fact.source_ref.location.page_number}` : ""}</>}
            </cite></blockquote>;
          })}</div>}
      </article>;
    })}
  </section>;
}
