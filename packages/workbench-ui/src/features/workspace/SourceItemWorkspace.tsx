import { useEffect, useState, type ReactNode } from "react";
import { ApiError, request } from "../../shared/api/client";
import { ErrorBlock, LoadingBlock } from "../../shared/ui/Feedback";
import { NativeStructureReferenceDock } from "../extraction/NativeStructureReferenceDock";
import { SourceItemDeliveryActions, SourceItemReasoningStatus, useSourceItemReasoning, type SourceItemDeliveryState } from "../extraction/SourceItemReasoning";
import { useWorkspace } from "./WorkspaceContext";
import { DeliveryVersions } from "./DeliveryVersions";
import { useDocumentAgent } from "./DocumentAgentContext";
import { useUiCopy } from "../../host/HostLocaleContext";

function WorkingExport({ state, projectId, documentId, onReview, onRefresh }: {
  state: SourceItemDeliveryState; projectId: string; documentId: string; onReview: () => void; onRefresh: () => void;
}) {
  const l = useUiCopy();
  const workflow = useSourceItemReasoning(projectId, documentId, state.baseline_sha256, state);
  const derived = Object.values(state.decomposition?.records || {}).reduce((sum, children) => sum + children.length, 0);
  return <section className="source-working-export" aria-label={l("sourceWorkspace.exportAria", "当前工作版导出")}>
    <header><div><span>{l("sourceWorkspace.sharedLedger", "与原文审阅共用同一账本")}</span><h1>{l("sourceWorkspace.exportTitle", "导出当前工作版")}</h1></div><button type="button" className="button secondary" onClick={onRefresh}>{l("sourceWorkspace.syncVersion", "同步工作版")}</button></header>
    <section className="working-export-readiness" aria-label={l("sourceWorkspace.readinessAria", "交付就绪情况")}><strong>{l("sourceWorkspace.downloadable", "可下载当前评审草稿")}</strong><p>{l("sourceWorkspace.pendingAnalysis", "当前仍有 {count} 个分析单元待完成响应。", { count: state.pending_unit_count })}{state.awaiting_evidence_unit_count !== undefined ? l("sourceWorkspace.pendingBreakdown", "其中 {evidence} 项待内部证据，{clarification} 项待澄清。", { evidence: state.awaiting_evidence_unit_count, clarification: state.clarification_unit_count || 0 }) : ""}</p><span>{l("sourceWorkspace.downloadKeeps", "下载会保留这些未决状态，正式响应由工程师确认。")}</span></section>
    <dl><div><dt>{l("sourceWorkspace.baseline", "原文工作版")}</dt><dd>v{state.baseline_revision}</dd></div>
      <div><dt>{l("sourceWorkspace.sourceItems", "完整原文")}</dt><dd>{l(state.source_item_count === 1 ? "sourceWorkspace.itemCountOne" : "sourceWorkspace.itemCount", "{count} 项", { count: state.source_item_count })}</dd></div><div><dt>{l("sourceWorkspace.requirements", "原文需求")}</dt><dd>{l(state.requirement_count === 1 ? "sourceWorkspace.itemCountOne" : "sourceWorkspace.itemCount", "{count} 项", { count: state.requirement_count })}</dd></div>
      <div><dt>{l("sourceWorkspace.nonRequirements", "非需求内容")}</dt><dd>{l(state.non_requirement_count === 1 ? "sourceWorkspace.itemCountOne" : "sourceWorkspace.itemCount", "{count} 项", { count: state.non_requirement_count })}</dd></div><div><dt>{l("sourceWorkspace.derived", "派生需求")}</dt><dd>{l(derived === 1 ? "sourceWorkspace.itemCountOne" : "sourceWorkspace.itemCount", "{count} 项", { count: derived })}</dd></div>
      <div><dt>{l("sourceWorkspace.analysisUnits", "实际分析单元")}</dt><dd>{l(state.analysis_unit_count === 1 ? "sourceWorkspace.itemCountOne" : "sourceWorkspace.itemCount", "{count} 项", { count: state.analysis_unit_count })}</dd></div></dl>
    <p>{l("sourceWorkspace.exportIncludes", "导出包含完整原文层级、SRC/REQ、派生关系、原表与原图，以及当前已保存的判断和图片释读。未完成的响应保持待分析或待补充。")}</p>
    <div className="source-working-export-actions"><SourceItemDeliveryActions workflow={workflow} exportOnly />
      <button type="button" className="button secondary" onClick={onReview}>{l("sourceWorkspace.backToReview", "回到原文审阅")}</button></div>
    <SourceItemReasoningStatus workflow={workflow} />
    <DeliveryVersions projectId={projectId} documentId={documentId} sourceSha256={state.source_sha256} baselineRevision={state.baseline_revision} />
  </section>;
}

/** Native working ledgers and explicit legacy confirmed sets keep distinct routes. */
export function SourceItemWorkspace({ task, fallback }: { task: "analysis" | "export"; fallback: ReactNode }) {
  const l = useUiCopy();
  const { route, updateRoute, selectedDocument } = useWorkspace();
  const agent = useDocumentAgent();
  const [reload, setReload] = useState(0), [page, setPage] = useState(1);
  const [result, setResult] = useState<{ key: string; state: SourceItemDeliveryState | null; error: string; absent: boolean } | null>(null);
  const publicationJob = agent?.task?.result?.application_saved ? agent.task.job_id : "";
  useEffect(() => { if (publicationJob) setReload(value => value + 1); }, [publicationJob]);
  const sourceSha = String(selectedDocument?.source_sha256 || "");
  const key = `${route.projectId}:${route.documentId}:${sourceSha}`;
  const enabled = Boolean(route.projectId && route.documentId && sourceSha && !route.requirementSetId);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setResult(null);
    void request<SourceItemDeliveryState>(`/api/projects/${encodeURIComponent(route.projectId)}/sor-documents/${encodeURIComponent(route.documentId)}/agent-structure/delivery/state`, { signal: controller.signal })
      .then(state => { if (!controller.signal.aborted) {
        if (state.source_sha256 !== sourceSha) throw new Error(l("sourceWorkspace.ledgerMismatch", "工作账本与当前来源版本不一致，请同步文档。"));
        setResult({ key, state, error: "", absent: false });
      } }).catch(error => { if (!controller.signal.aborted) setResult({ key, state: null, error: error.message || l("sourceWorkspace.ledgerUnavailable", "工作账本暂不可读取"), absent: error instanceof ApiError && (error.status === 404 || error.status === 409 && String(error.message).includes("原文条目尚未生成")) }); });
    return () => controller.abort();
  }, [enabled, key, reload, route.projectId, route.documentId, sourceSha]);
  if (!enabled) return <>{fallback}</>;
  if (!result || result.key !== key) return <LoadingBlock label={l("sourceWorkspace.loadingLedger", "正在读取与需求提取一致的工作账本")} />;
  if (result.absent) return <section className="document-ledger-empty" aria-label={l("sourceWorkspace.noLedgerAria", "尚无原文工作版")}>
    <h2>{l("sourceWorkspace.organizeFirst", "先整理原文条目")}</h2>
    <p>{task === "analysis" ? l("sourceWorkspace.noAnalysisLedger", "当前文档还没有可分析的原文工作版。") : l("sourceWorkspace.noExportLedger", "当前文档还没有可导出的原文工作版。")}{l("sourceWorkspace.organizeNote", "前往需求提取整理原文；已保存的阶段成果可在 Agent 协作中核对。")}</p>
    <div><button type="button" className="button primary" onClick={() => updateRoute({ task:"extraction",mode:"organize",checkpoint:"structure" })}>{l("sourceWorkspace.goToExtraction", "前往需求提取")}</button>
      {agent && <button type="button" className="button secondary" onClick={() => agent.show()}>{l("sourceWorkspace.viewAgent", "查看 Agent 协作")}</button>}
      <button type="button" className="button ghost" onClick={() => setReload(value => value + 1)}>{l("sourceWorkspace.syncSaved", "同步已有成果")}</button></div>
  </section>;
  if (result.error || !result.state) return <ErrorBlock error={new Error(result.error)} onRetry={() => setReload(value => value+1)} />;
  const onReview = () => updateRoute({ task: "extraction", mode: "organize", checkpoint: "list" });
  if (task === "export") return <WorkingExport key={result.state.baseline_sha256} state={result.state}
    projectId={route.projectId} documentId={route.documentId} onReview={onReview} onRefresh={() => setReload(value => value+1)} />;
  return <section className="source-native-workspace" aria-label={l("sourceWorkspace.analysisAria", "当前工作版分析")}>
    <header className="source-native-workspace-header"><div><strong>{l("sourceWorkspace.ledgerAnalysis", "工作账本分析")}</strong><span>{l("sourceWorkspace.basedOnVersion", "沿用原文工作版 v{revision} · 原文、派生与证据共同查看", { revision: result.state.baseline_revision })}</span></div>
      <button type="button" className="button secondary compact" onClick={onReview}>{l("sourceWorkspace.reviewSource", "原文审阅")}</button></header>
    <NativeStructureReferenceDock projectId={route.projectId} documentId={route.documentId} sourceSha256={sourceSha}
      onChangePurpose={onReview}
      sourceFileUrl={`/api/projects/${encodeURIComponent(route.projectId)}/sor-documents/${encodeURIComponent(route.documentId)}/file`}
      activePage={page} onPageChange={setPage} showSourcePreview mode="SOURCE_REQUIREMENTS" purpose="analysis" initialReasoningState={result.state} />
  </section>;
}
