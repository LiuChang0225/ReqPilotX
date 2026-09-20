import { useEffect, useState, type ReactNode } from "react";
import { ApiError, request } from "../../shared/api/client";
import { ErrorBlock, LoadingBlock } from "../../shared/ui/Feedback";
import { NativeStructureReferenceDock } from "../extraction/NativeStructureReferenceDock";
import { SourceItemDeliveryActions, SourceItemReasoningStatus, useSourceItemReasoning, type SourceItemDeliveryState } from "../extraction/SourceItemReasoning";
import { useWorkspace } from "./WorkspaceContext";
import { DeliveryVersions } from "./DeliveryVersions";
import { useDocumentAgent } from "./DocumentAgentContext";

function WorkingExport({ state, projectId, documentId, onReview, onRefresh }: {
  state: SourceItemDeliveryState; projectId: string; documentId: string; onReview: () => void; onRefresh: () => void;
}) {
  const workflow = useSourceItemReasoning(projectId, documentId, state.baseline_sha256, state);
  const derived = Object.values(state.decomposition?.records || {}).reduce((sum, children) => sum + children.length, 0);
  return <section className="source-working-export" aria-label="当前工作版导出">
    <header><div><span>与原文审阅共用同一账本</span><h1>导出当前工作版</h1></div><button type="button" className="button secondary" onClick={onRefresh}>同步工作版</button></header>
    <section className="working-export-readiness" aria-label="交付就绪情况"><strong>可下载当前评审草稿</strong><p>当前仍有 {state.pending_unit_count} 个分析单元待完成响应。{state.awaiting_evidence_unit_count !== undefined ? `其中 ${state.awaiting_evidence_unit_count} 项待内部证据，${state.clarification_unit_count || 0} 项待澄清。` : ""}</p><span>下载会保留这些未决状态，正式响应由工程师确认。</span></section>
    <dl><div><dt>原文工作版</dt><dd>v{state.baseline_revision}</dd></div>
      <div><dt>完整原文</dt><dd>{state.source_item_count} 项</dd></div><div><dt>原文需求</dt><dd>{state.requirement_count} 项</dd></div>
      <div><dt>非需求内容</dt><dd>{state.non_requirement_count} 项</dd></div><div><dt>派生需求</dt><dd>{derived} 项</dd></div>
      <div><dt>实际分析单元</dt><dd>{state.analysis_unit_count} 项</dd></div></dl>
    <p>导出包含完整原文层级、SRC/REQ、派生关系、原表与原图，以及当前已保存的判断和图片释读。未完成的响应保持待分析或待补充。</p>
    <div className="source-working-export-actions"><SourceItemDeliveryActions workflow={workflow} exportOnly />
      <button type="button" className="button secondary" onClick={onReview}>回到原文审阅</button></div>
    <SourceItemReasoningStatus workflow={workflow} />
    <DeliveryVersions projectId={projectId} documentId={documentId} sourceSha256={state.source_sha256} baselineRevision={state.baseline_revision} />
  </section>;
}

/** Native working ledgers and explicit legacy confirmed sets keep distinct routes. */
export function SourceItemWorkspace({ task, fallback }: { task: "analysis" | "export"; fallback: ReactNode }) {
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
        if (state.source_sha256 !== sourceSha) throw new Error("工作账本与当前来源版本不一致，请同步文档。");
        setResult({ key, state, error: "", absent: false });
      } }).catch(error => { if (!controller.signal.aborted) setResult({ key, state: null, error: error.message || "工作账本暂不可读取", absent: error instanceof ApiError && (error.status === 404 || error.status === 409 && String(error.message).includes("原文条目尚未生成")) }); });
    return () => controller.abort();
  }, [enabled, key, reload, route.projectId, route.documentId, sourceSha]);
  if (!enabled) return <>{fallback}</>;
  if (!result || result.key !== key) return <LoadingBlock label="正在读取与需求提取一致的工作账本" />;
  if (result.absent) return <section className="document-ledger-empty" aria-label="尚无原文工作版">
    <h2>先整理原文条目</h2>
    <p>{task === "analysis" ? "当前文档还没有可分析的原文工作版。" : "当前文档还没有可导出的原文工作版。"}前往需求提取整理原文；已保存的阶段成果可在 Agent 协作中核对。</p>
    <div><button type="button" className="button primary" onClick={() => updateRoute({ task:"extraction",mode:"organize",checkpoint:"structure" })}>前往需求提取</button>
      {agent && <button type="button" className="button secondary" onClick={() => agent.show()}>查看 Agent 协作</button>}
      <button type="button" className="button ghost" onClick={() => setReload(value => value + 1)}>同步已有成果</button></div>
  </section>;
  if (result.error || !result.state) return <ErrorBlock error={new Error(result.error)} onRetry={() => setReload(value => value+1)} />;
  const onReview = () => updateRoute({ task: "extraction", mode: "organize", checkpoint: "list" });
  if (task === "export") return <WorkingExport key={result.state.baseline_sha256} state={result.state}
    projectId={route.projectId} documentId={route.documentId} onReview={onReview} onRefresh={() => setReload(value => value+1)} />;
  return <section className="source-native-workspace" aria-label="当前工作版分析">
    <header className="source-native-workspace-header"><div><strong>工作账本分析</strong><span>沿用原文工作版 v{result.state.baseline_revision} · 原文、派生与证据共同查看</span></div>
      <button type="button" className="button secondary compact" onClick={onReview}>原文审阅</button></header>
    <NativeStructureReferenceDock projectId={route.projectId} documentId={route.documentId} sourceSha256={sourceSha}
      onChangePurpose={onReview}
      sourceFileUrl={`/api/projects/${encodeURIComponent(route.projectId)}/sor-documents/${encodeURIComponent(route.documentId)}/file`}
      activePage={page} onPageChange={setPage} showSourcePreview mode="SOURCE_REQUIREMENTS" purpose="analysis" initialReasoningState={result.state} />
  </section>;
}
