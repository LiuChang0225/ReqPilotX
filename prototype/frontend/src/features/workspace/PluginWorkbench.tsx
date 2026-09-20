/** Producer-specific content only. DocumentWorkSurface owns the single shell and collaboration lane. */
import { useEffect, useState } from "react";
import { NativeStructureReferenceDock } from "../extraction/NativeStructureReferenceDock";
import { RequirementTreeStageRail } from "../extraction/RequirementTreeStageRail";
import { useWorkspace } from "./WorkspaceContext";
import type { PluginWorkbenchProvider } from "./usePluginWorkbench";
import type { WorkspaceContextValue } from "./WorkspaceContext";
import { useWorkbenchHost } from "./WorkbenchHostContext";
export type { PluginState } from "./documentWorkProvider";

type PluginWorkbenchProps = {
  provider: PluginWorkbenchProvider; projectId: string; documentId: string; sourceSha256: string; documentName: string;
};
export function PluginWorkbench(props: PluginWorkbenchProps) {
  const { route, updateRoute } = useWorkspace();
  return <PluginWorkbenchContent {...props} route={route} updateRoute={updateRoute} />;
}

/** The same business view mounted by the standalone shell or a host panel. */
export function PluginWorkbenchContent({ provider, projectId, documentId, sourceSha256, documentName, route, updateRoute }: PluginWorkbenchProps & Pick<WorkspaceContextValue, "route" | "updateRoute">) {
  const host = useWorkbenchHost();
  const [page, setPage] = useState(1), [error, setError] = useState("");
  const { reader, state, version, current } = provider;
  useEffect(() => { setError(""); }, [provider.pinned, projectId, documentId]);
  const hasItems = Boolean(reader?.outline);
  const itemsMode = hasItems && (route.checkpoint === "list" || route.task === "analysis");
  const sourceUrl = host.resourceUrl(`/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/file`);
  const confirm = async () => { setError(""); try { await provider.confirmMap(); } catch (cause) { setError(cause instanceof Error ? cause.message : "确认未保存，请重试。"); } };
  if (route.task === "export" && !reader) return <section className="source-working-export" aria-label="当前核对版交付"><h1>交付当前核对版</h1><p role="status">{state && !state.revision_id ? "当前任务只有来源，尚无可导出的阶段成果。" : provider.error ? "当前版本暂不可读取，请重新同步或选择已保存版本。" : "正在读取当前核对版的交付内容…"}</p><button type="button" className="button secondary" onClick={() => updateRoute({ task: "extraction", mode: "organize", checkpoint: "structure" })}>返回当前工作</button></section>;
  if (route.task === "export") return <section className="source-working-export" aria-label="当前核对版交付">
    <header><div><span>{documentName}</span><h1>交付当前核对版</h1></div><span>v{version?.number || "—"}{current ? " · 最新" : " · 历史版本"}</span></header>
    <section className="working-export-readiness"><strong>{hasItems ? "可核对的评审草稿" : "条目成果尚未保存"}</strong><p>{hasItems ? "导出包含当前核对版全部已保存内容，来源、父子关系与分类保持一致。" : "当前只有结构成果，可先核对结构并向原外部任务反馈。"}</p>
      {reader?.analysis_policy?.status === "SKIPPED" && <p>符合性分析未执行：{reader.analysis_policy.reason === "USER_SKIPPED" ? "本次选择跳过" : "未提供内部依据"}。</p>}
    </section>
    {reader?.outline && <dl><div><dt>原文条目</dt><dd>{reader.outline.summary.source_item_count}</dd></div><div><dt>原文需求</dt><dd>{reader.outline.summary.requirement_count}</dd></div><div><dt>保留的非需求</dt><dd>{reader.outline.summary.non_requirement_count}</dd></div><div><dt>实际分析单元</dt><dd>{reader.reasoning?.analysis_unit_count ?? "未提供"}</dd></div></dl>}
    <div className="source-working-export-actions">{hasItems && provider.capability("export_draft").enabled ? <a className="button primary" href={host.resourceUrl(provider.exportUrl)}>导出当前核对版 Excel</a> : <p>{provider.capability("export_draft").reason || "当前版本暂不可导出。"}</p>}
      <button className="button secondary" onClick={() => updateRoute({ task:"extraction", mode:"organize", checkpoint:hasItems?"list":"structure" })}>回到原文核对</button></div>
    <p className="muted">这是外部插件提交的工作草稿。当前连接尚未接入完整人工响应和正式交付锁定能力。</p>
    <section className="delivery-versions"><h2>已保存的成果版本</h2><div className="delivery-history-scroll"><table className="delivery-history-table"><thead><tr><th>版本</th><th>已保存成果</th><th>保存时间</th><th>查看</th></tr></thead><tbody>{state?.versions.map(item => <tr key={item.revision_id}><td>v{item.number}</td><td>{item.stages.map(s => `${s.stage === "MAP" ? "结构" : "原文条目"} ${s.count} 项`).join(" · ")}</td><td>{item.created_at ? new Date(item.created_at).toLocaleString("zh-CN") : "未提供"}</td><td><button className="button ghost compact" disabled={item.revision_id === provider.pinned} onClick={() => provider.selectRevision(item.revision_id)}>核对此版</button></td></tr>)}</tbody></table></div></section>
  </section>;
  return <section className="requirement-tree-page" aria-label={route.task === "analysis" ? "当前核对版分析" : "当前核对版原文"}>
    {!host.readOnly && <header className="requirement-tree-commandbar"><div className="requirement-tree-title"><span>{route.task === "analysis" ? "响应与证据" : "需求提取"}</span><strong title={documentName}>{documentName}</strong></div>
      {route.task === "extraction" && <RequirementTreeStageRail value={itemsMode ? "list" : "structure"} reviewAvailable={false} workingLedger itemsAvailable={hasItems} onChange={checkpoint => updateRoute({ task:"extraction",mode:"organize",checkpoint })}/>}
    </header>}
    {error && <p role="alert">{error}</p>}
    {!host.readOnly && current && state?.map_confirmation_required && route.task === "extraction" && !itemsMode && <section className="stage-approval-card" aria-label="确认文档结构"><div><strong>核对这一版结构后继续</strong><p>确认会保存到当前版本；由原外部任务取回后继续整理条目。</p></div><button className="button primary compact" disabled={!reader || provider.pending || !provider.capability("confirm_map").enabled} onClick={() => void confirm()}>确认此结构</button></section>}
    {route.task === "analysis" && <p className="agent-policy-note">{reader?.analysis_policy?.status === "SKIPPED" ? "符合性分析未执行：未提供内部依据或本次选择跳过。" : "当前显示外部已保存成果。"} {host.readOnly ? "当前面板只读，可核对来源与已保存建议。" : "当前连接尚未接入分析和人工响应的完整传输，可核对来源并保存修改意见。"}</p>}
    {state && !state.revision_id && <div className="source-only-work"><p>当前任务已绑定这份原件，尚未保存结构或条目。可先阅读原件，后续成果会保留在这个任务中。</p>{host.readOnly ? <a className="button secondary" href={sourceUrl} target="_blank" rel="noreferrer">阅读原件</a> : <button type="button" className="button secondary" onClick={() => updateRoute({ task: "reading" })}>阅读原件</button>}</div>}
    {!reader ? state && !state.revision_id ? null : <p role="status">{provider.error ? "当前核对版暂不可读取，请重新同步或选择已保存版本。" : !state ? "正在同步外部成果…" : provider.pinned ? "正在读取当前核对版…" : "等待外部任务保存首个结构成果…"}</p> : route.task === "analysis" && !hasItems ? <p>当前只有结构草稿；条目形成后可查看响应。</p> : <NativeStructureReferenceDock
      key={`${reader.revision_id}`} projectId={projectId} documentId={documentId} sourceSha256={sourceSha256} sourceReader={reader}
      onChangePurpose={purpose => updateRoute({ task: purpose === "analysis" ? "analysis" : "extraction", mode: "organize", checkpoint: "list" })}
      sourceFileUrl={sourceUrl} showSourcePreview activePage={page} onPageChange={setPage} mode={itemsMode ? "SOURCE_REQUIREMENTS" : "STRUCTURE"} purpose={route.task === "analysis" ? "analysis" : "review"}/> }
  </section>;
}
