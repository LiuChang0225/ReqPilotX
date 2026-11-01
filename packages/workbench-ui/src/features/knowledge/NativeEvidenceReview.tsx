import { useEffect, useRef, useState } from "react";
import { Dialog } from "../../shared/ui/Dialog";
import { useWorkbenchHost } from "../workspace/WorkbenchHostContext";
import { ProjectKnowledgePanel } from "./ProjectKnowledgePanel";
import { projectKnowledgeFileUrl, type ProjectKnowledgeCatalog } from "./projectKnowledge";
import { openKbEvidenceApi, type EvidenceGap, type EvidenceScope, type EvidenceSession, type OriginalEvidence, type PreparedEvidenceReview } from "./openKbEvidenceApi";

type Props = {
  scope: EvidenceScope; unitIds: string[]; unitLabel: string; knowledgeIds: string[] | null;
  onKnowledgeChange: (ids: string[] | null) => void; durable?: boolean; onClose: () => void; onPrepared: () => void;
};
type Originals = { documentId: string; fileName: string; evidence: OriginalEvidence[]; gaps: EvidenceGap[] };
const errorText = (cause: unknown) => cause instanceof Error ? cause.message : "证据入口暂不可用，请重试。";

/** Source-only bridge into the existing canonical reasoning and human-response ledger. */
export function NativeEvidenceReview({ scope, unitIds, unitLabel, knowledgeIds, onKnowledgeChange, durable, onClose, onPrepared }: Props) {
  const host = useWorkbenchHost();
  const [catalog, setCatalog] = useState<ProjectKnowledgeCatalog | null>(null);
  const [session, setSession] = useState<EvidenceSession | null>(null), [originals, setOriginals] = useState<Originals[]>([]);
  const [selected, setSelected] = useState<string[]>([]), [pending, setPending] = useState("");
  const [error, setError] = useState(""), [prepared, setPrepared] = useState<PreparedEvidenceReview | null>(null);
  const [readErrors, setReadErrors] = useState<string[]>([]);
  const active = useRef<AbortController | null>(null), mounted = useRef(true);
  const scopeKey = JSON.stringify([scope.projectId, scope.documentId, scope.sourceSha256, scope.baselineSha256, unitIds,
    knowledgeIds, catalog?.knowledge_base_version]);
  const currentKey = useRef(scopeKey); currentKey.current = scopeKey;
  useEffect(() => {
    setSession(null); setOriginals([]); setSelected([]); setError(""); setReadErrors([]); setPrepared(null); setPending("");
    return () => { active.current?.abort(); active.current = null; };
  }, [scopeKey]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; active.current?.abort(); }; }, []);
  const requestedIds = knowledgeIds ?? catalog?.documents.filter(item => item.eligible).map(item => item.id) ?? [];
  const requestedDocuments = catalog?.documents.filter(item => requestedIds.includes(item.id) && item.eligible) ?? [];
  const missing = requestedDocuments.length !== requestedIds.length;
  const readOriginals = async () => {
    if (!catalog || missing || active.current || host.readOnly || host.nativeEvidence !== true) return;
    const startedKey = scopeKey, controller = new AbortController(); active.current = controller;
    setPending("read"); setError(""); setReadErrors([]); setSession(null); setOriginals([]); setSelected([]); setPrepared(null);
    try {
      const status = await openKbEvidenceApi.status(scope.projectId, controller.signal);
      if (!status.available) throw new Error("来源证据入口当前不可用，请稍后重试。");
      if (status.knowledge_base_version !== catalog.knowledge_base_version) throw new Error("项目资料已更新，请先同步资料目录再重新读取原文。");
      const packet = await openKbEvidenceApi.createSession(scope, status, knowledgeIds, unitIds, controller.signal);
      const results = await Promise.allSettled(requestedDocuments.map(async document => ({ documentId: document.id, fileName: document.file_name,
        ...await openKbEvidenceApi.originals(scope, packet, document.id, document.source_sha256, controller.signal) })));
      if (controller.signal.aborted || currentKey.current !== startedKey || !mounted.current) return;
      const contents = results.flatMap(result => result.status === "fulfilled" ? [result.value] : []);
      setReadErrors(results.flatMap((result, index) => result.status === "rejected" ? [`${requestedDocuments[index].file_name}：${errorText(result.reason)}`] : []));
      const evidenceIds = contents.flatMap(item => item.evidence.map(part => part.evidence_id));
      if (new Set(evidenceIds).size !== evidenceIds.length) throw new Error("不同资料返回了重复证据身份，请重新同步。");
      setSession(packet); setOriginals(contents);
    } catch (cause) { if (!controller.signal.aborted && currentKey.current === startedKey && mounted.current) setError(errorText(cause)); }
    finally { if (active.current === controller) active.current = null; if (currentKey.current === startedKey && mounted.current) setPending(""); }
  };
  const prepare = async () => {
    if (!session || prepared || readErrors.length || active.current || host.readOnly || host.nativeEvidence !== true) return;
    const startedKey = scopeKey, controller = new AbortController(); active.current = controller;
    setPending("prepare"); setError("");
    try {
      const status = await openKbEvidenceApi.status(scope.projectId, controller.signal);
      if (!status.available || status.generation !== session.generation || status.catalog_sha256 !== session.directory_catalog_sha256
        || status.knowledge_base_version !== session.knowledge_base_version) throw new Error("项目资料版本已变化，请重新读取原文后准备草稿。");
      const result = await openKbEvidenceApi.prepareReview(scope, session, selected, controller.signal);
      if (controller.signal.aborted || currentKey.current !== startedKey || !mounted.current) return;
      setPrepared(result); onPrepared();
    } catch (cause) {
      if (!controller.signal.aborted && currentKey.current === startedKey && mounted.current) {
        setError(`${errorText(cause)} 请先同步工作账本；已有草稿可能已保存。重新读取证据包后再操作。`);
        setSession(null); onPrepared();
      }
    } finally { if (active.current === controller) active.current = null; if (currentKey.current === startedKey && mounted.current) setPending(""); }
  };
  const close = () => { if (pending === "prepare") onPrepared(); active.current?.abort(); onClose(); };
  const gaps = [...session?.catalog_gaps || [], ...originals.flatMap(item => item.gaps)];
  return <Dialog open title="准备内部证据评审草稿" onClose={close} className="native-evidence-review-dialog">
    <header className="native-evidence-review-header"><div><h2>准备内部证据评审草稿</h2><p>{unitLabel} · {unitIds.length} 个当前分析单元</p></div><button type="button" className="button ghost compact" onClick={close}>关闭证据评审</button></header>
    <p>仅核对登记原文并保存“待内部证据”工作草稿。语义比较未执行（NOT_PERFORMED），OpenKB 实时检索未运行（NOT_RUN）；人工接受未声明（NOT_CLAIMED）。正式工作意见仍由现有工程师意见入口确认。</p>
    <ProjectKnowledgePanel projectId={scope.projectId} onCatalogChange={setCatalog} selection={{ ids: knowledgeIds, onChange: onKnowledgeChange, durable, disabled: Boolean(pending || prepared) }} />
    {missing && <p role="alert">部分所选资料已失效，请重新选择或同步项目目录。</p>}
    <div className="native-evidence-actions"><button type="button" className="button secondary compact" disabled={!catalog || missing || Boolean(pending || prepared)} onClick={() => void readOriginals()}>{pending === "read" ? "正在读取原文…" : session ? "重新读取证据包" : "读取所选资料原文"}</button></div>
    {error && <p role="alert">{error}</p>}
    {session && <section className="native-evidence-originals" aria-label="证据原文选择">
      <p role="status">已读取来源证据包 · {originals.length} 份资料 · {originals.reduce((sum, item) => sum + item.evidence.length, 0)} 段原文。请逐段选择供人工核对的原文；选择原文不等于符合性证明。</p>
      {!requestedDocuments.length && <p>本次未选择项目资料；草稿仅保留证据待补充状态。</p>}
      {readErrors.length > 0 && <div role="alert"><strong>部分原文未读取，暂不能准备草稿。请重试读取，或重新选择资料后继续。</strong><ul>{readErrors.map(message => <li key={message}>{message}</li>)}</ul></div>}
      {originals.map(document => <article key={document.documentId}><h3>{document.fileName}</h3>
        {!document.evidence.length && <p>此资料当前没有可引用文字。请结合登记原件核对，不能据此判断不存在证据。</p>}
        {document.evidence.map(item => <div className="native-evidence-original" key={item.evidence_id}>
          <label><input type="checkbox" checked={selected.includes(item.evidence_id)} disabled={Boolean(pending || prepared) || selected.length >= 32 && !selected.includes(item.evidence_id)} onChange={event => setSelected(ids => event.target.checked ? [...ids, item.evidence_id] : ids.filter(id => id !== item.evidence_id))} />选择原文 {item.evidence_id}</label>
          <p className="native-evidence-quote">{item.text}</p><small>来源权威：{item.authority || "UNKNOWN"} · 证据用途：{item.claim_role || "UNKNOWN"} · 适用性：{item.applicability || "UNKNOWN"}</small>
          <details><summary>核对原始引用与定位</summary><p className="native-evidence-locator">{JSON.stringify(item.source_ref)}</p><p>原文范围：{item.reference.quote_start}–{item.reference.quote_end} · 来源版本 {item.reference.source_sha256}</p></details>
          <a target="_blank" rel="noreferrer" href={host.resourceUrl(projectKnowledgeFileUrl(scope.projectId, document.documentId, item.reference.source_sha256))}>打开此证据登记原件</a>
        </div>)}
      </article>)}
      {gaps.length > 0 && <aside aria-label="证据缺口"><strong>来源与内容缺口</strong>{gaps.map((gap, index) => <p key={`${gap.code}:${index}`}>{gap.message || gap.code || "存在尚待核对的内容缺口"}</p>)}</aside>}
      <p>已选 {selected.length} / 32 段原文。未选原文也只能形成待补充草稿，不会自动生成符合建议或人工意见。</p>
      <button type="button" className="button secondary" disabled={Boolean(pending || prepared || readErrors.length)} onClick={() => void prepare()}>{pending === "prepare" ? "正在保存评审草稿…" : prepared ? "评审草稿已保存" : "准备待证据评审草稿"}</button>
    </section>}
    {prepared && <p role="status">已为本次选定单元保存待内部证据工作草稿（PENDING_EVIDENCE）· 当前草稿中未决分析单元 {prepared.pending_unit_count} 项。未执行语义比较、模型或实时检索，未声明人工接受；关闭后在现有工程师意见入口核对。</p>}
    {pending && <p className="muted">可关闭此窗口停止等待；如果正在保存，请同步工作账本确认服务端结果后再重试。</p>}
  </Dialog>;
}
