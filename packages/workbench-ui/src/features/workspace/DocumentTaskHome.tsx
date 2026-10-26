import { useEffect, useMemo, useRef, useState } from "react";
import { request } from "../../shared/api/client";
import { Icon } from "../../shared/ui/Icon";
import { useSaasNativeAgentAvailability } from "../extraction/saasAgentAvailability";
import { useDocumentAgent } from "./DocumentAgentContext";
import { useWorkspace } from "./WorkspaceContext";
import { useApplicationHost } from "../../host/ApplicationHost";
import { useWorkbenchHost } from "./WorkbenchHostContext";
import { agentTaskLabel, agentProgressLabel, isAgentTaskActive } from "./documentWorkSummary";
import "./document-intake.css";

type Evidence = { raw_text?: string; native_locator?: { page_number?: number | null } };
type CaptureNode = Evidence & { source_node_id: string; table_payload?: { cells?: (Evidence & { cell_id: string })[] } | null };
type Candidate = {
  structure_node_id: string; kind: string; source_order: number; explicit_label?: string | null;
  source_refs: { source_id: string }[];
  semantic_source_spans?: { source_ref: { source_id: string }; start_offset: number; end_offset: number }[];
};
type FastCapture = {
  schema_version: string; project_id: string; document_id: string; source_sha256: string;
  capture_status: string; elapsed_seconds: number; cache_reused: boolean;
  capture_counts: Record<string, number>;
  page_facts: { page: number; native_text_characters: number }[];
  gaps: { code: string; message: string; pages?: number[] }[];
  source_ir: { nodes: CaptureNode[] };
  structure_proposal: { nodes: Candidate[] } | null;
};
const KIND_LABELS: Record<string, string> = { document: "文档", section: "章节", clause: "条款", content: "正文", table: "表格", table_row: "表格行", list_group: "列表", group: "内容组", continuation: "续接内容" };
const AGENT_GOAL = "阅读当前客户文档，整理可回溯的原文结构、原文条目与分类。保留完整BOM、试验及跨页条件，自查并修订问题，保存可核对的同版本成果与Matrix草稿。没有内部依据时跳过符合性判断，保留未决项。";

function candidateRows(capture: FastCapture | null, fileName: string) {
  if (!capture) return [];
  const evidence = new Map<string, Evidence>();
  for (const node of capture.source_ir.nodes) {
    evidence.set(node.source_node_id, node);
    for (const cell of node.table_payload?.cells || []) evidence.set(cell.cell_id, cell);
  }
  return [...(capture.structure_proposal?.nodes || [])].sort((a, b) => a.source_order - b.source_order).map(node => {
    const refs = node.source_refs.map(ref => evidence.get(ref.source_id)).filter((item): item is Evidence => Boolean(item));
    const spans = node.semantic_source_spans || [];
    const text = (spans.length ? spans.map(span => {
      const raw = evidence.get(span.source_ref.source_id)?.raw_text || "";
      return Array.from(raw).slice(span.start_offset, span.end_offset).join("");
    }) : refs.map(item => item.raw_text || "")).filter(Boolean).join("\n");
    const pages = [...new Set(refs.map(item => item.native_locator?.page_number).filter((page): page is number => typeof page === "number" && page > 0))];
    return { id: node.structure_node_id, kind: KIND_LABELS[node.kind] || "内容", label: node.explicit_label || "", text,
      title: text.replace(/\s+/g, " ").trim() || (node.kind === "document" ? fileName : "此候选没有可显示的文字，请核对原件"), pages };
  });
}

export function DocumentTaskHome(props: { fileName: string; sourceFileUrl: string }) {
  const { route, selectedDocument } = useWorkspace();
  const sourceSha256 = String(selectedDocument?.source_sha256 || "");
  return <DocumentIntake key={`${route.projectId}:${route.documentId}:${sourceSha256}`}
    {...props} projectId={route.projectId} documentId={route.documentId} sourceSha256={sourceSha256} agentSelected={route.entry === "agent"} />;
}

function DocumentIntake({ fileName, sourceFileUrl, projectId, documentId, sourceSha256, agentSelected }: {
  fileName: string; sourceFileUrl: string; projectId: string; documentId: string; sourceSha256: string; agentSelected: boolean;
}) {
  const agent = useDocumentAgent();
  const applicationHost = useApplicationHost();
  const presentation = useWorkbenchHost();
  const availability = useSaasNativeAgentAvailability();
  const task = agent?.task;
  const active = isAgentTaskActive(task || null);
  const [capture, setCapture] = useState<FastCapture | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(50);
  const [previewPage, setPreviewPage] = useState(1);
  const inFlight = useRef<AbortController | null>(null);
  useEffect(() => () => { inFlight.current?.abort(); }, []);
  const rows = useMemo(() => candidateRows(capture, fileName), [capture, fileName]);
  const needle = query.trim().toLocaleLowerCase();
  const filtered = useMemo(() => needle ? rows.filter(row => `${row.label} ${row.title}`.toLocaleLowerCase().includes(needle)) : rows, [rows, needle]);
  const description = active ? agentProgressLabel(task?.progress?.message) || "正在处理当前文档，已保存的阶段成果可随时核对。"
    : task?.job_status === "FAILED" ? "本轮任务未完成。原件与已保存的成果仍保留，可查看原因后继续。"
    : task?.job_status === "CANCELLED" || task?.runtime_status === "INTERRUPTED" ? "任务已暂停，已有处理记录和成果仍保留。"
    : task?.map_confirmation_required ? "结构草案已保存，可查看任务并核对结构。"
    : "已有任务和处理记录可继续查看，也可补充修改意见。";
  const agentReady = Boolean(agent && agent.collaborationAvailable !== false && availability.ready);
  const fast = async () => {
    if (inFlight.current || !projectId || !documentId || !sourceSha256) return;
    const controller = new AbortController();
    inFlight.current = controller;
    setPending(true); setError("");
    try {
      const value = await request<FastCapture>(`/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/source-capture/fast`, {
        method: "POST", body: JSON.stringify({ expected_source_sha256: sourceSha256 }), signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (value.schema_version !== "project-fast-source-capture/v1" || value.source_sha256 !== sourceSha256
        || value.project_id !== projectId || value.document_id !== documentId) throw new Error("读取结果与当前原件不一致，请重新读取。");
      setCapture(value); setQuery(""); setLimit(50);
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "原文读取未完成，请重试。");
    } finally {
      if (!controller.signal.aborted) { inFlight.current = null; setPending(false); }
    }
  };
  const isPdf = fileName.toLowerCase().endsWith(".pdf");
  const textCharacters = capture?.page_facts.reduce((total, page) => total + page.native_text_characters, 0) || 0;
  return <section className="document-intake" aria-label="文档整理起始页">
    <div className="document-intake-work">
      <header className="document-intake-heading"><h2>从原文开始整理</h2><p>先查看文档内容和结构候选，再选择需要的整理方式。</p></header>
      <section className="document-intake-quick" aria-label="快速读取">
        <div className="document-intake-action"><button type="button" className="button primary" disabled={pending || !sourceSha256 || !projectId || !documentId || Boolean(applicationHost && !applicationHost.captureAvailable)} onClick={() => void fast()}>
          <Icon name={pending ? "refresh" : "document"} size={16} className={pending ? "is-spinning" : ""} />
          {pending ? "正在读取原文…" : error ? "重试读取原文" : capture ? "重新读取原文" : "快速读取原文"}
        </button><span>程序读取 · 不调用模型</span></div>
        {!sourceSha256 && <p className="document-intake-note">正在等待原件信息，准备完成后即可读取。</p>}
        {applicationHost && !applicationHost.captureAvailable && <p className="document-intake-note">当前宿主尚未接入程序读取，原件与已保存成果仍可查看。</p>}
        {!capture && <p className="document-intake-note">读取文字、表格和图像，查看程序生成的结构候选。</p>}
        {pending && <p role="status" className="document-intake-note">正在读取当前原件，大文档可能需要稍等。</p>}
        {error && <p role="alert" className="document-intake-error">{error}</p>}
      </section>
      {capture && <section className="document-intake-results" aria-label="程序读取结果">
        <header><h3>程序候选</h3><span>{capture.capture_status === "FAILED" ? "读取未完成" : capture.capture_status === "PARTIAL" ? "部分读取" : "读取完成"}{capture.cache_reused ? " · 复用已存原文" : ""}</span></header>
        <p className="document-intake-note">以下内容供核对，尚未生成正式 SRC 或完成需求分类。</p>
        <dl className="document-intake-facts">
          <div><dt>页数</dt><dd>{capture.page_facts.length || "未知"}</dd></div>
          <div><dt>文字</dt><dd>{textCharacters.toLocaleString()} 字符</dd></div>
          <div><dt>表格</dt><dd>{capture.capture_counts.table || 0}</dd></div>
          <div><dt>图像</dt><dd>{(capture.capture_counts.image || 0) + (capture.capture_counts.figure || 0)}</dd></div>
          <div><dt>结构候选</dt><dd>{rows.length}</dd></div>
        </dl>
        {capture.gaps.length > 0 && <details className="document-intake-gaps" open><summary>需核对的来源问题 · {capture.gaps.length}</summary><ul>{capture.gaps.map((gap, index) => <li key={`${gap.code}:${index}`}>
          {gap.pages?.length ? <strong>第 {gap.pages.join("、")} 页：</strong> : null}{gap.message}
        </li>)}</ul></details>}
        {rows.length > 0 ? <>
          <label className="document-intake-search"><Icon name="search" size={15} /><span className="sr-only">搜索结构候选</span><input aria-label="搜索结构候选" type="search" placeholder="查找编号或原文" value={query} onChange={event => { setQuery(event.target.value); setLimit(50); }} /></label>
          <p className="document-intake-count">显示 {Math.min(limit, filtered.length)} / {filtered.length} 个候选 · 展开查看原文</p>
          <ol className="document-intake-list" aria-label="结构候选清单">{filtered.slice(0, limit).map(row => <li key={row.id}>
            <details><summary><span className="document-intake-kind">{row.kind}</span><span>{row.label && !row.title.startsWith(row.label) ? `${row.label} ` : ""}{row.title}</span></summary>
              <p className="document-intake-source-text">{row.text || row.title}</p>
              {isPdf && row.pages.length > 0 && <div className="document-intake-page-links">{row.pages.map(page => <button type="button" key={page} className="button ghost compact" onClick={() => setPreviewPage(page)}>查看原件第 {page} 页<Icon name="arrow" size={13} /></button>)}</div>}
            </details>
          </li>)}</ol>
          {!filtered.length && <p className="document-intake-note">没有匹配的候选，试试其他编号或文字。</p>}
          {filtered.length > limit && <button type="button" className="button secondary compact" onClick={() => setLimit(value => value + 50)}>再显示 50 个候选</button>}
        </> : <p className="document-intake-note">本次没有生成结构候选。可先核对原件与来源问题，再选择 Agent 整理。</p>}
      </section>}
      <details className="document-intake-agent" open={agentSelected || Boolean(task?.task_id)}>
        <summary><Icon name="spark" size={16} /><span>{task?.task_id ? "已有 Agent 任务" : "需要语义判断？使用 Agent 整理"}</span><Icon name="chevron-down" size={15} /></summary>
        {task?.task_id ? <><h3>{agentTaskLabel(task)}</h3><p role="status">{description}</p></> : <p>结合原文理解条目边界、需求类别及关联条件，并自查修订。你可以先核对任务目标。</p>}
        {task?.task_id || agentReady ? <button type="button" className="button secondary" onClick={() => agent?.show(task?.task_id ? undefined : AGENT_GOAL, "document")}>
          <Icon name="spark" size={15} />{task?.task_id ? "查看任务与补充意见" : "使用 Agent 整理"}
        </button> : <div className="document-intake-agent-unavailable"><p>{availability.message || "Agent 当前不可用，配置完成后可继续。"}</p><a className="button secondary" href="/workspace?task=projects&settings=agent"><Icon name="settings" size={15} />配置 Agent</a></div>}
      </details>
    </div>
    <section className="document-intake-preview" aria-label="原件预览"><header><Icon name="document" size={15} /><strong title={fileName}>{fileName}</strong><a href={presentation.resourceUrl(sourceFileUrl)} target="_blank" rel="noreferrer"><Icon name="external" size={14} />打开原文件</a></header>
      {isPdf ? <iframe title="任务原文件预览" src={presentation.pdfViewerUrl(sourceFileUrl, previewPage, true)} /> : <p>可在新窗口打开原文件查看。</p>}
    </section>
  </section>;
}
