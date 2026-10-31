import { useEffect, useMemo, useRef, useState } from "react";
import { request } from "../../shared/api/client";
import { Icon } from "../../shared/ui/Icon";
import { useSaasNativeAgentAvailability } from "../extraction/saasAgentAvailability";
import { useDocumentAgent } from "./DocumentAgentContext";
import { useWorkspace } from "./WorkspaceContext";
import { useApplicationHost } from "../../host/ApplicationHost";
import { useWorkbenchHost } from "./WorkbenchHostContext";
import { agentTaskLabel, agentProgressLabel, isAgentTaskActive } from "./documentWorkSummary";
import { sourceHeadingLabel } from "../extraction/sourceHeadingLabel";
import { useUiCopy, type UiTranslate } from "../../host/HostLocaleContext";
import { useHostPdfViewerUrl } from "../pdf/pdfViewerUrl";
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
const KIND_LABELS: Record<string, [string, string]> = {
  document: ["document.kind.document", "文档"], section: ["document.kind.section", "章节"], clause: ["document.kind.clause", "条款"],
  content: ["document.kind.content", "正文"], table: ["document.kind.table", "表格"], table_row: ["document.kind.tableRow", "表格行"],
  list_group: ["document.kind.listGroup", "列表"], group: ["document.kind.group", "内容组"], continuation: ["document.kind.continuation", "续接内容"],
};
const agentGoal = (l: UiTranslate) => l("document.defaultGoal", "阅读当前客户文档，整理可回溯的原文结构、原文条目与分类。保留完整BOM、试验及跨页条件，自查并修订问题，保存可核对的同版本成果与Matrix草稿。没有内部依据时跳过符合性判断，保留未决项。");

function candidateRows(capture: FastCapture | null, fileName: string, l: UiTranslate) {
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
    const kind = KIND_LABELS[node.kind];
    return { id: node.structure_node_id, kind: kind ? l(kind[0], kind[1]) : l("document.kind.content", "内容"), label: node.explicit_label || "", text,
      title: text.replace(/\s+/g, " ").trim() || (node.kind === "document" ? fileName : l("document.candidateNoText", "此候选没有可显示的文字，请核对原件")), pages };
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
  const l = useUiCopy();
  const pdfViewerUrl = useHostPdfViewerUrl();
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
  const rows = useMemo(() => candidateRows(capture, fileName, l), [capture, fileName, l]);
  const needle = query.trim().toLocaleLowerCase();
  const filtered = useMemo(() => needle ? rows.filter(row => `${row.label} ${row.title}`.toLocaleLowerCase().includes(needle)) : rows, [rows, needle]);
  const description = active ? agentProgressLabel(task?.progress?.message, l) || l("document.activeDescription", "正在处理当前文档，已保存的阶段成果可随时核对。")
    : task?.job_status === "FAILED" ? l("document.failedDescription", "本轮任务未完成。原件与已保存的成果仍保留，可查看原因后继续。")
    : task?.job_status === "CANCELLED" || task?.runtime_status === "INTERRUPTED" ? l("document.pausedDescription", "任务已暂停，已有处理记录和成果仍保留。")
    : task?.map_confirmation_required ? l("document.mapDraftDescription", "结构草案已保存，可查看任务并核对结构。")
    : l("document.existingTaskDescription", "已有任务和处理记录可继续查看，也可补充修改意见。");
  const agentReady = Boolean(agent && agent.collaborationAvailable !== false && (applicationHost?.taskStartAvailable || availability.ready));
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
        || value.project_id !== projectId || value.document_id !== documentId) throw new Error(l("document.captureMismatch", "读取结果与当前原件不一致，请重新读取。"));
      setCapture(value); setQuery(""); setLimit(50);
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : l("document.captureFailed", "原文读取未完成，请重试。"));
    } finally {
      if (!controller.signal.aborted) { inFlight.current = null; setPending(false); }
    }
  };
  const isPdf = fileName.toLowerCase().endsWith(".pdf");
  const textCharacters = capture?.page_facts.reduce((total, page) => total + page.native_text_characters, 0) || 0;
  return <section className="document-intake" aria-label={l("document.intakeAria", "文档整理起始页")}>
    <div className="document-intake-work">
      <header className="document-intake-heading"><h2>{l("document.intakeTitle", "从原文开始整理")}</h2><p>{l("document.intakeSubtitle", "先查看文档内容和结构候选，再选择需要的整理方式。")}</p></header>
      <section className="document-intake-quick" aria-label={l("document.quickReadAria", "快速读取")}>
        <div className="document-intake-action"><button type="button" className="button primary" disabled={pending || !sourceSha256 || !projectId || !documentId || Boolean(applicationHost && !applicationHost.captureAvailable)} onClick={() => void fast()}>
          <Icon name={pending ? "refresh" : "document"} size={16} className={pending ? "is-spinning" : ""} />
          {pending ? l("document.readingSource", "正在读取原文…") : error ? l("document.retryRead", "重试读取原文") : capture ? l("document.rereadSource", "重新读取原文") : l("document.quickReadSource", "快速读取原文")}
        </button><span>{l("document.captureNoModel", "程序读取 · 不调用模型")}</span></div>
        {!sourceSha256 && <p className="document-intake-note">{l("document.waitingSource", "正在等待原件信息，准备完成后即可读取。")}</p>}
        {applicationHost && !applicationHost.captureAvailable && <p className="document-intake-note">{l("document.hostCaptureUnavailable", "当前宿主尚未接入程序读取，原件与已保存成果仍可查看。")}</p>}
        {!capture && <p className="document-intake-note">{l("document.captureHint", "读取文字、表格和图像，查看程序生成的结构候选。")}</p>}
        {pending && <p role="status" className="document-intake-note">{l("document.capturePending", "正在读取当前原件，大文档可能需要稍等。")}</p>}
        {error && <p role="alert" className="document-intake-error">{error}</p>}
      </section>
      {capture && <section className="document-intake-results" aria-label={l("document.captureResultsAria", "程序读取结果")}>
        <header><h3>{l("document.programCandidates", "程序候选")}</h3><span>{capture.capture_status === "FAILED" ? l("document.captureFailedStatus", "读取未完成") : capture.capture_status === "PARTIAL" ? l("document.capturePartial", "部分读取") : l("document.captureComplete", "读取完成")}{capture.cache_reused ? l("document.captureReused", " · 复用已存原文") : ""}</span></header>
        <p className="document-intake-note">{l("document.captureReviewNote", "以下内容供核对，尚未生成正式 SRC 或完成需求分类。")}</p>
        <dl className="document-intake-facts">
          <div><dt>{l("document.pages", "页数")}</dt><dd>{capture.page_facts.length || l("common.unknown", "未知")}</dd></div>
          <div><dt>{l("document.text", "文字")}</dt><dd>{l("document.characters", "{count} 字符", { count: textCharacters.toLocaleString() })}</dd></div>
          <div><dt>{l("document.tables", "表格")}</dt><dd>{capture.capture_counts.table || 0}</dd></div>
          <div><dt>{l("document.images", "图像")}</dt><dd>{(capture.capture_counts.image || 0) + (capture.capture_counts.figure || 0)}</dd></div>
          <div><dt>{l("document.structureCandidates", "结构候选")}</dt><dd>{rows.length}</dd></div>
        </dl>
        {capture.gaps.length > 0 && <details className="document-intake-gaps" open><summary>{l("document.sourceIssues", "需核对的来源问题 · {count}", { count: capture.gaps.length })}</summary><ul>{capture.gaps.map((gap, index) => <li key={`${gap.code}:${index}`}>
          {gap.pages?.length ? <strong>{l("document.pagePrefix", "第 {pages} 页：", { pages: gap.pages.join(l("document.pageSeparator", "、")) })}</strong> : null}{gap.message}
        </li>)}</ul></details>}
        {rows.length > 0 ? <>
          <label className="document-intake-search"><Icon name="search" size={15} /><span className="sr-only">{l("document.searchCandidates", "搜索结构候选")}</span><input aria-label={l("document.searchCandidates", "搜索结构候选")} type="search" placeholder={l("document.searchCandidateHint", "查找编号或原文")} value={query} onChange={event => { setQuery(event.target.value); setLimit(50); }} /></label>
          <p className="document-intake-count">{l("document.candidateCount", "显示 {shown} / {total} 个候选 · 展开查看原文", { shown: Math.min(limit, filtered.length), total: filtered.length })}</p>
          <ol className="document-intake-list" aria-label={l("document.candidateList", "结构候选清单")}>{filtered.slice(0, limit).map(row => <li key={row.id}>
            <details><summary><span className="document-intake-kind">{row.kind}</span><span>{sourceHeadingLabel(row.label, row.title)}</span></summary>
              <p className="document-intake-source-text">{row.text || row.title}</p>
              {isPdf && row.pages.length > 0 && <div className="document-intake-page-links">{row.pages.map(page => <button type="button" key={page} className="button ghost compact" onClick={() => setPreviewPage(page)}>{l("document.viewSourcePage", "查看原件第 {page} 页", { page })}<Icon name="arrow" size={13} /></button>)}</div>}
            </details>
          </li>)}</ol>
          {!filtered.length && <p className="document-intake-note">{l("document.noCandidateMatch", "没有匹配的候选，试试其他编号或文字。")}</p>}
          {filtered.length > limit && <button type="button" className="button secondary compact" onClick={() => setLimit(value => value + 50)}>{l("document.showMoreCandidates", "再显示 50 个候选")}</button>}
        </> : <p className="document-intake-note">{l("document.noCandidates", "本次没有生成结构候选。可先核对原件与来源问题，再选择 Agent 整理。")}</p>}
      </section>}
      {applicationHost ? <section className="document-intake-fixed" aria-label={l("document.fixedWorkflowAria", "固定业务流程说明")}>
        <header><Icon name="spark" size={16} /><h3>{applicationHost.workflow?.label || l("document.fixedWorkflowFallback", "客户需求整理固定流程")}</h3>{applicationHost.workflow ? <span>v{applicationHost.workflow.version}</span> : null}</header>
        <dl>
          <div><dt>{l("document.requiredInputs", "必需资料")}</dt><dd>{l("document.customerRequirementDocument", "客户需求文档：{name}", { name: fileName })}</dd></div>
          <div><dt>{l("document.optionalEvidence", "可选内部依据")}</dt><dd>{applicationHost.workflow?.optional_inputs?.length
            ? applicationHost.workflow.optional_inputs.map(item => `${item.label}${l("document.labelSeparator", "：")}${item.available ? l("document.optionalAvailable", "可选择") : item.reason || l("document.optionalUnavailable", "当前不可用")}`).join(l("common.listSeparator", "；"))
            : l("document.noOptionalEvidence", "当前版本尚未接入项目参考或团队知识库；不会据此虚构符合性结论。")}</dd></div>
          <div><dt>{l("document.expectedOutputs", "预期成果")}</dt><dd>{applicationHost.workflow?.expected_outputs?.join(l("common.listSeparator", "；")) || l("document.expectedOutputsFallback", "文档结构、原文条目与分类、自查和可核对成果；无内部依据时明确标记符合性分析未执行。")}</dd></div>
        </dl>
        <p>{l("document.fixedWorkflowNote", "点击开始一次即可，无需编写启动提示词；补充说明是可选输入，不能取消阶段和成果检查。")}</p>
      </section> : null}
      <details className="document-intake-agent" open={agentSelected || Boolean(task?.task_id)}>
        <summary><Icon name="spark" size={16} /><span>{task?.task_id ? l("document.existingAgentTask", "已有 Agent 任务") : applicationHost ? l("document.organizeWithFixedWorkflow", "按固定业务流程整理") : l("document.needSemantic", "需要语义判断？使用 Agent 整理")}</span><Icon name="chevron-down" size={15} /></summary>
        {task?.task_id ? <><h3>{agentTaskLabel(task, l)}</h3><p role="status">{description}</p></> : <p>{applicationHost ? l("document.pairFixedWorkflowNote", "由 PAIR 执行固定流程，实际阶段以已保存成果为准；尚未开始。") : l("document.agentOrganizeNote", "结合原文理解条目边界、需求类别及关联条件，并自查修订。你可以先核对任务目标。")}</p>}
        {task?.task_id || agentReady ? <button type="button" className="button secondary" onClick={() => agent?.show(task?.task_id ? undefined : applicationHost ? undefined : agentGoal(l), "document")}>
          <Icon name="spark" size={15} />{task?.task_id ? l("document.viewTaskAndComments", "查看任务与补充意见") : applicationHost ? l("document.openFixedWorkflowTask", "打开固定流程任务") : l("document.useAgentOrganize", "使用 Agent 整理")}
        </button> : <div className="document-intake-agent-unavailable"><p>{applicationHost?.taskSetup ? l("document.pairTaskNotReady", "PAIR 任务启动尚未就绪：请检查能力包与默认模型设置。") : availability.message || l("document.agentUnavailable", "Agent 当前不可用，配置完成后可继续。")}</p>{applicationHost ? null : <a className="button secondary" href="/workspace?task=projects&settings=agent"><Icon name="settings" size={15} />{l("home.configureAgent", "配置 Agent")}</a>}</div>}
      </details>
    </div>
    <section className="document-intake-preview" aria-label={l("document.sourcePreviewAria", "原件预览")}><header><Icon name="document" size={15} /><strong title={fileName}>{fileName}</strong><a href={presentation.resourceUrl(sourceFileUrl)} target="_blank" rel="noreferrer"><Icon name="external" size={14} />{l("library.openOriginal", "打开原文件")}</a></header>
      {isPdf ? <iframe title={l("document.taskSourcePreview", "任务原文件预览")} src={pdfViewerUrl(sourceFileUrl, previewPage, true)} /> : <p>{l("document.openSourceInNewWindow", "可在新窗口打开原文件查看。")}</p>}
    </section>
  </section>;
}
