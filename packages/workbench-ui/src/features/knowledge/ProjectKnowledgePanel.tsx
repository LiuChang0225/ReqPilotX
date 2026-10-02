import { useEffect, useState } from "react";
import { projectKnowledgeFileUrl, useProjectKnowledgeCatalog } from "./projectKnowledge";
import type { ProjectKnowledgeCatalog } from "./projectKnowledge";
import { useWorkbenchHost } from "../workspace/WorkbenchHostContext";
import { NativeEvidenceRegistration } from "./NativeEvidenceRegistration";

type Selection = { ids: string[] | null; onChange: (ids: string[] | null) => void; disabled?: boolean; durable?: boolean };
const label = (value?: string) => ({ UNKNOWN: "未标记", INTERNAL_CONTROLLED: "内部受控", CUSTOMER_APPROVED: "客户认可", REGULATORY_AUTHORITY: "监管机构", CERTIFICATION_BODY: "认证机构", TEST_LAB: "试验机构", NORMATIVE_REQUIREMENT: "规范要求", CERTIFICATION: "认证记录", COMPLIANCE_RECORD: "符合性记录", RELEASED_DESIGN_EVIDENCE: "已发布设计依据", REFERENCE_CONTEXT: "参考资料", TEST_RESULT: "试验结果", VALIDATION_RESULT: "验证结果", DESIGN_DECLARATION: "设计声明", APPLICABLE: "适用", ACTIVE: "当前资料", SUPERSEDED: "已被替代", INACTIVE: "未启用" } as Record<string, string>)[value || ""] || value || "未标记";

export function ProjectKnowledgePanel({ projectId, focusedDocumentId, onFocusDocument, selection, onCatalogChange }: { projectId: string; focusedDocumentId?: string; onFocusDocument?: (id: string) => void; selection?: Selection; onCatalogChange?: (catalog: ProjectKnowledgeCatalog | null) => void }) {
  const host = useWorkbenchHost();
  const { catalog, error, loading, refresh } = useProjectKnowledgeCatalog(projectId);
  useEffect(() => { onCatalogChange?.(catalog); }, [catalog, onCatalogChange]);
  const [selected, setSelected] = useState(""), [limit, setLimit] = useState(20);
  useEffect(() => { setSelected(""); setLimit(20); }, [projectId]);
  const focusId = focusedDocumentId || selected;
  const document = focusId ? catalog?.documents.find(item => item.id === focusId) : catalog?.documents[0];
  const facts = document ? Object.values(catalog?.evidence_catalog || {}).filter(fact => fact.document_id === document.id) : [];
  const ids = selection?.ids ?? catalog?.documents.filter(item => item.eligible).map(item => item.id) ?? [];
  const missingSelection = catalog && selection?.ids?.some(id => !catalog.documents.some(item => item.id === id && item.eligible));
  return <section className="project-knowledge-panel" aria-label={selection ? "本次分析依据" : "项目依据目录"}>
    <header><div><strong>{selection ? "下次分析使用的项目资料" : "已登记的项目资料"}</strong><p>{selection ? "运行分析时使用所选范围；已保存建议的依据见各条引用。" : "文件可读取与可证明符合性分别判断，权威、用途及适用性沿用登记信息。"}</p></div><button type="button" className="button ghost compact" onClick={refresh} disabled={loading}>同步资料</button></header>
    {host.nativeEvidence === true && !host.readOnly && <NativeEvidenceRegistration key={projectId} projectId={projectId} onRegistered={refresh} disabled={selection?.disabled} />}
    {!projectId && <p>先选择项目，再查看该项目的依据。</p>}
    {loading && <p role="status">正在读取项目资料与来源引用…</p>}
    {error && <p role="alert">{error}</p>}
    {catalog && <>
      {selection && <div className="knowledge-selection-tools"><span>{selection.ids === null ? "默认使用项目内可读取资料" : `已选择 ${selection.ids.length} 份资料`}</span><button type="button" className="button ghost compact" disabled={selection.disabled} onClick={() => selection.onChange(null)}>使用项目默认范围</button><button type="button" className="button ghost compact" disabled={selection.disabled} onClick={() => selection.onChange([])}>本次不使用项目资料</button></div>}
      {selection?.durable === false && <p role="status">本次资料选择仅在当前页面保留。</p>}
      {missingSelection && <p role="alert">部分已选资料当前不可读取，请重新核对本次范围。</p>}
      {!catalog.documents.length ? <p className="knowledge-empty">项目尚未登记内部资料。{host.nativeEvidence === true && !host.readOnly ? "可在上方登记证据原件；" : "可在“项目管理 → 内部知识”添加；"}参考案例不会自动成为本项目证据。</p> : <div className="project-knowledge-layout">
        <div className="knowledge-document-list">{catalog.documents.map(item => <div key={item.id} className={document?.id === item.id ? "is-selected" : ""}>
          {selection && <input type="checkbox" aria-label={`用于本次分析：${item.file_name}`} disabled={!item.eligible || selection.disabled} checked={ids.includes(item.id)} onChange={event => selection.onChange(event.target.checked ? [...ids, item.id] : ids.filter(id => id !== item.id))} />}
          <button type="button" aria-pressed={document?.id === item.id} onClick={() => { setSelected(item.id); setLimit(20); onFocusDocument?.(item.id); }}><strong>{item.file_name}</strong><small>{item.doc_version ? `版本 ${item.doc_version} · ` : ""}{item.eligible ? "可读取" : "当前不可读取"}</small></button>
        </div>)}</div>
        {!document && focusId && <p role="alert">指定资料不在当前项目目录中。请从左侧选择要查看的资料。</p>}
        {document && <article className="knowledge-document-detail"><header><div><h3>{document.file_name}</h3><p>{label(document.authority)} · {label(document.claim_role)} · 适用性：{label(document.applicability)}</p></div>{projectKnowledgeFileUrl(projectId, document.id, document.source_sha256) && <a className="button secondary compact" target="_blank" rel="noreferrer" href={projectKnowledgeFileUrl(projectId, document.id, document.source_sha256)}>打开登记原件</a>}</header>
          {!document.eligible && <p>当前资料不进入有效事实目录：{label(document.status)}。</p>}
          <p className="muted">已捕获 {facts.length} 段原文；原件打开时校验登记版本。</p>
          {facts.slice(0, limit).map((fact, index) => <details className="knowledge-fact" key={fact.fact_id}><summary>{fact.source_ref?.location?.page_number ? `P${fact.source_ref.location.page_number} · ` : ""}原文内容 {index + 1}</summary><p>{fact.text}</p>{fact.table ? <small>表格的行列关系请在登记原件中核对。</small> : null}</details>)}
          {facts.length > limit && <button type="button" className="button ghost compact" onClick={() => setLimit(value => value + 20)}>继续显示原文（剩余 {facts.length - limit} 段）</button>}
          {catalog.gaps?.filter(gap => gap.document_id === document.id).map((gap, index) => <p className="muted" key={`${gap.code}:${index}`}>{gap.message || (gap.code === "VISUAL_EVIDENCE_REQUIRES_READING" ? "部分图片需要结合原件阅读。" : "部分内容尚无可引用文字，请结合原件核对。")}</p>)}
        </article>}
      </div>}
    </>}
  </section>;
}
