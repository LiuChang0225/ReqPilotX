import { useEffect, useRef, useState } from "react";
import { request } from "../../shared/api/client";
import { retainedCommand, settleCommand } from "../extraction/agentTaskTransport";

type Readiness = { can_lock: boolean; blockers: string[]; analysis_target_count: number; effective_conclusion_count: number; ai_default_count: number; human_count: number };
type Preview = { source_sha256: string; delivery_sha256: string; business_basis_sha256: string; baseline_revision: number; response_revision: number; readiness: Readiness };
type DeliveryVersion = { revision: number; mode: "DRAFT" | "FORMAL"; created_at: string; baseline_revision: number; source_sha256: string; response_revision: number; delivery_sha256: string; readiness: Readiness };

type DeliveryVersionsProps = {
  projectId: string; documentId: string; sourceSha256: string; baselineRevision: number;
};
export function DeliveryVersions(props: DeliveryVersionsProps) {
  return <ScopedDeliveryVersions key={`${props.projectId}:${props.documentId}:${props.sourceSha256}:${props.baselineRevision}`} {...props} />;
}
function ScopedDeliveryVersions({ projectId, documentId, sourceSha256, baselineRevision }: DeliveryVersionsProps) {
  const root = `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/delivery/versions`;
  const scope = `${root}:${sourceSha256}:${baselineRevision}`, identity = useRef(scope); identity.current = scope;
  const [preview, setPreview] = useState<Preview | null>(null), [versions, setVersions] = useState<DeliveryVersion[]>([]);
  const [accepted, setAccepted] = useState(false), [busy, setBusy] = useState(false), [loading, setLoading] = useState(true);
  const [historyKnown, setHistoryKnown] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setPreview(null); setAccepted(false); setLoading(true); setError(""); setNotice(""); setBusy(false);
    void Promise.all([request<Preview>(`${root}/preview`, { signal: controller.signal }), request<{versions: DeliveryVersion[]}>(root, { signal: controller.signal })])
      .then(([current, history]) => {
        if (controller.signal.aborted) return;
        if (current.source_sha256 !== sourceSha256 || current.baseline_revision !== baselineRevision) throw new Error("工作版已更新，请同步工作版后核对交付。");
        setPreview(current); setVersions(history.versions); setHistoryKnown(true);
      }).catch(cause => { if (!controller.signal.aborted) setError(cause.message || "交付版本暂不可读取，请重试。"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [root, scope, sourceSha256, baselineRevision, reload]);
  const save = async (mode: "DRAFT" | "FORMAL") => {
    if (!preview || busy) return;
    const sentScope = scope;
    const body = { expected_source_sha256: preview.source_sha256, expected_delivery_sha256: preview.delivery_sha256,
      expected_business_basis_sha256: preview.business_basis_sha256, mode, accept_snapshot: mode === "FORMAL" && accepted };
    const storage = `reqpilotx:delivery-command:${scope}`, command_id = retainedCommand(storage, JSON.stringify(body));
    setBusy(true); setError(""); setNotice("");
    try {
      const saved = await request<DeliveryVersion>(root, { method: "POST", body: JSON.stringify({ ...body, command_id }) });
      settleCommand(storage, command_id);
      if (identity.current !== sentScope) return;
      setVersions(old => [saved, ...old.filter(version => version.revision !== saved.revision)].sort((a,b) => b.revision-a.revision));
      setAccepted(false); setNotice(`交付 v${saved.revision} 已保存，快照与 Excel 可在下方重新下载。`);
    } catch (cause) { settleCommand(storage, command_id, cause); if (identity.current === sentScope) setError(cause instanceof Error ? cause.message : "保存未完成，请重试。"); }
    finally { if (identity.current === sentScope) setBusy(false); }
  };
  return <section className="delivery-versions" aria-label="交付版本">
    <header><div><h2>保存交付版本</h2><p>保存完整账本与 Excel，后续修订保留这次交付。</p></div><button className="button ghost compact" disabled={busy || loading} onClick={() => setReload(value => value+1)}>同步交付状态</button></header>
    {loading && <p role="status">正在核对来源、响应与历史版本…</p>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {preview && <div className="delivery-freeze">
      <div><strong>原文 v{preview.baseline_revision} · 响应 v{preview.response_revision}</strong>
        <p>{preview.readiness.effective_conclusion_count} / {preview.readiness.analysis_target_count} 个单元已有有效响应（工程师 {preview.readiness.human_count} 项，Agent 建议 {preview.readiness.ai_default_count} 项）</p>
        {preview.readiness.blockers.length > 0 && <ul>{preview.readiness.blockers.map(blocker => <li key={blocker}>{blocker}</li>)}</ul>}
        <button className="button secondary" disabled={busy} onClick={() => void save("DRAFT")}>{busy ? "正在保存…" : "保存草稿版本"}</button>
      </div>
      <div className="delivery-lock">
        <strong>锁定正式交付版</strong><p>确认这份响应快照后保存为 FORMAL；当前工作版可继续修订。</p>
        <label><input type="checkbox" checked={accepted} disabled={busy || !preview.readiness.can_lock} onChange={event => setAccepted(event.target.checked)} />我已核对本版响应，确认锁定此交付快照</label>
        <button className="button primary" disabled={busy || !accepted || !preview.readiness.can_lock} onClick={() => void save("FORMAL")}>确认并保存正式版</button>
      </div>
    </div>}
    <div className="delivery-history-heading"><h2>交付历史</h2><span>{historyKnown ? `${versions.length} 个版本` : loading ? "正在读取…" : "暂不可读取"}</span></div>
    {!loading && historyKnown && versions.length === 0 && <p className="muted">还没有保存的交付版本。保存草稿后，可随时下载同一份文件。</p>}
    {versions.length > 0 && <div className="delivery-history-scroll"><table className="delivery-history-table"><thead><tr><th>交付版</th><th>绑定工作版</th><th>响应覆盖</th><th>保存时间</th><th>文件</th></tr></thead>
      <tbody>{versions.map(version => <tr key={version.revision}><td><strong>v{version.revision}</strong><span className="delivery-mode">{version.mode === "FORMAL" ? "正式版 · 已锁定" : "评审草稿"}</span></td>
        <td>原文 v{version.baseline_revision} · 响应 v{version.response_revision}{version.source_sha256 !== sourceSha256 && <small>前一来源文件</small>}</td>
        <td>{version.readiness.effective_conclusion_count} / {version.readiness.analysis_target_count}</td>
        <td><time dateTime={version.created_at}>{new Date(version.created_at).toLocaleString("zh-CN", { month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit" })}</time></td>
        <td><a className="button ghost compact" href={`${root}/${version.revision}/export`}>下载 Excel</a></td></tr>)}</tbody></table></div>}
  </section>;
}
