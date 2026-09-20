import { useEffect, useRef, useState } from "react";
import { request } from "../../shared/api/client";
import { Icon } from "../../shared/ui/Icon";
import { sourcePageLabel } from "./sourcePageLabel";
import { retainedCommand, settleCommand } from "./agentTaskTransport";

export type SourceStage = { stage: "MAP" | "ITEMS"; artifact_sha256: string; count: number };
export type StageRow = { node_id?: string; parent_id?: string | null; source_record_id?: string; title?: string; label?: string; kind?: string;
  source_display_id?: string; requirement_display_id?: string; requirementness?: string; requirement_kinds?: string[];
  ancestor_path?: string[]; pages?: number[]; source_form?: string; logical_table_scope_id?: string; source_content?: { text: string; page?: number }[]; text_bindings?: { text: string; page?: number }[]; source_text?: string };
type Preview = { artifact_sha256: string; source_sha256: string; rows: StageRow[]; total: number; next_offset: number | null };
function rowLabel(row: StageRow) {
  const label = row.source_display_id || row.label || "";
  const title = row.title || row.label || "原文内容";
  return label === title ? title : `${label} ${title}`.trim();
}

export function SourceDocumentStageResults({ root, taskId, sourceSha256, stages, confirmationRequired, onConfirmed, initialStage, workspace = false, sourceFileUrl, revisionId, onSelectionChange, confirmationDescription, confirmationLabel }: {
  root: string; taskId: string; sourceSha256: string; stages: SourceStage[]; confirmationRequired: boolean;
  onConfirmed: (value: unknown) => void;
  initialStage?: "MAP" | "ITEMS"; workspace?: boolean; sourceFileUrl?: string;
  revisionId?: string; onSelectionChange?: (row: StageRow | null) => void;
  confirmationDescription?: string; confirmationLabel?: string;
}) {
  const [selected, setSelected] = useState<SourceStage | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [sourcePage, setSourcePage] = useState(1);
  const [selectedRow, setSelectedRow] = useState(0);
  const key = `${root}:${taskId}:${sourceSha256}:${revisionId || ""}:${stages.map(s => s.artifact_sha256).join(":")}`;
  const identity = useRef(key); identity.current = key;
  const selectionIdentity = `${key}:${selected?.stage || ""}:${selected?.artifact_sha256 || ""}`;
  const selectedIdentity = useRef(selectionIdentity); selectedIdentity.current = selectionIdentity;
  useEffect(() => { setSelected(stages.find(stage => stage.stage === initialStage) || null); setPreview(null); setSelectedRow(0); setError(""); setPending(false); }, [key, initialStage]);
  useEffect(() => { onSelectionChange?.(preview?.rows[selectedRow] || null); }, [preview, selectedRow, onSelectionChange]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setPending(true); setError(""); setPreview(null);
    void request<Preview>(`${root}/${encodeURIComponent(taskId)}/stages/${selected.stage}${revisionId ? `?revision=${revisionId}` : ""}`, { signal: controller.signal })
      .then(value => {
        if (controller.signal.aborted) return;
        if (value.artifact_sha256 !== selected.artifact_sha256 || value.source_sha256 !== sourceSha256) throw new Error("阶段已更新，请重新同步后打开。");
        setPreview(value);
      }).catch(cause => { if (!controller.signal.aborted) setError(cause.message); })
      .finally(() => { if (!controller.signal.aborted) setPending(false); });
    return () => controller.abort();
  }, [root, taskId, selected, sourceSha256, revisionId]);
  const more = async () => {
    if (!selected || preview?.next_offset == null) return;
    const current = selectionIdentity;
    setPending(true);
    try {
      const next = await request<Preview>(`${root}/${encodeURIComponent(taskId)}/stages/${selected.stage}?offset=${preview.next_offset}${revisionId ? `&revision=${revisionId}` : ""}`);
      if (selectedIdentity.current !== current) return;
      if (next.artifact_sha256 !== preview.artifact_sha256 || next.source_sha256 !== sourceSha256) throw new Error("阶段已更新，请重新打开。");
      setPreview({ ...next, rows: [...preview.rows, ...next.rows] });
    } catch (cause) { if (selectedIdentity.current === current) setError(cause instanceof Error ? cause.message : "暂时无法读取。"); }
    finally { if (selectedIdentity.current === current) setPending(false); }
  };
  const confirm = async () => {
    if (!selected || selected.stage !== "MAP" || !preview) return;
    const current = key;
    setPending(true); setError("");
    const storageKey = `reqpilotx:confirm-map:${root}:${taskId}`;
    const signature = `${sourceSha256}:${preview.artifact_sha256}`;
    const commandId = retainedCommand(storageKey, signature);
    try {
      const value = await request(`${root}/${encodeURIComponent(taskId)}/confirm-map`, { method: "POST", body: JSON.stringify({
        command_id: commandId, expected_source_sha256: sourceSha256, expected_map_sha256: preview.artifact_sha256,
        ...(revisionId ? { expected_revision_id: revisionId } : {}),
      }) });
      settleCommand(storageKey, commandId);
      if (identity.current === current) onConfirmed(value);
    } catch (cause) { settleCommand(storageKey, commandId, cause); if (identity.current === current) setError(cause instanceof Error ? cause.message : "确认未保存。"); }
    finally { if (identity.current === current) setPending(false); }
  };
  const stageRowsById = new Map((preview?.rows || []).map(row => [row.node_id, row]));
  const depthFor = (row: StageRow) => {
    if (row.ancestor_path?.length) return row.ancestor_path.length;
    let parent = row.parent_id, depth = 0;
    const seen = new Set<string>();
    while (parent && !seen.has(parent)) { seen.add(parent); depth += 1; parent = stageRowsById.get(parent)?.parent_id; }
    return depth;
  };
  if (!stages.length) return null;
  return <section className="agent-stage-results" aria-label="阶段成果">
    <strong>已可核对的成果</strong>
    <div className="agent-stage-result-list">{stages.map(stage => <button key={stage.stage} type="button" className="agent-stage-result"
      aria-pressed={selected?.stage === stage.stage} onClick={() => setSelected(stage)}>
      <Icon name={stage.stage === "MAP" ? "tree" : "requirement"} size={17} />
      <span>{stage.stage === "MAP" ? "查看文档结构" : "查看条目与分类"}<small>已保存 · 可核对</small></span>
      <em>{stage.count} 项</em><Icon name="chevron" size={14} /></button>)}</div>
    {confirmationRequired && <p role="status">文档结构已形成，确认后继续提取条目；可先补充结构修改意见。</p>}
    {error && <p role="alert">{error}</p>}
    {workspace && preview ? <>
      {confirmationRequired && selected?.stage === "MAP" && <section className="stage-approval-card" aria-label="确认文档结构">
        <Icon name="requirement" size={19} /><div><strong>这一版结构是否可以继续？</strong><p>{confirmationDescription || "核对下面的结构与原页。确认后继续提取原文条目；有问题可在 Agent 协作中补充修改意见。"}</p></div>
        <button type="button" className="button primary" disabled={pending} onClick={() => void confirm()}>{confirmationLabel || "确认此结构并继续"}</button>
      </section>}
      <p className="stage-scope-note">阶段草稿 · 已显示 {preview.rows.length} / {preview.total} 项 · 内容可核对，不代表全文或人工响应已完成</p>
      <div className="stage-review-layout">
        <div className={`stage-records${selected?.stage === "ITEMS" ? " is-items" : ""}`} aria-label={selected?.stage === "MAP" ? "阶段文档结构" : "阶段原文条目"}>
          <table><thead><tr><th>原文结构与内容</th>{selected?.stage === "ITEMS" && <th>判定 / 类别</th>}<th>来源</th></tr></thead><tbody>{preview.rows.map((row, index) => {
            const pages = row.pages?.length ? row.pages : [...new Set([...(row.source_content || []), ...(row.text_bindings || [])].map(c => c.page).filter((page): page is number => Boolean(page)))];
            return <tr key={row.source_record_id || row.node_id || index} className={selectedRow === index ? "is-selected" : ""}><td>
              <button type="button" style={{ paddingLeft: `${10 + Math.min(depthFor(row), 6) * 12}px` }} onClick={() => { setSelectedRow(index); if (pages[0]) setSourcePage(pages[0]); }}>
                <Icon name={row.kind?.includes("TABLE") ? "matrix" : "document"} size={14} /><span>{rowLabel(row)}</span>
              </button></td>{selected?.stage === "ITEMS" && <td className="stage-item-classification"><strong>{row.requirement_display_id || row.source_display_id}</strong><span>{row.requirementness === "REQUIREMENT" ? "需求" : row.requirementness === "NON_REQUIREMENT" ? "非需求" : "待判定"}</span><small>{row.requirement_kinds?.map(kind => ({ FUNCTIONAL: "功能", QUALITY: "质量", CONSTRAINT: "约束", PROJECT: "项目", PROCESS: "过程" }[kind] || kind)).join(" / ")}</small></td>}<td>{sourcePageLabel(pages)}</td></tr>;
          })}</tbody></table>
          {preview.next_offset !== null && <button type="button" className="button ghost compact" disabled={pending} onClick={() => void more()}>显示更多</button>}
        </div>
        <div className="stage-source-detail">
          <div className="stage-original-copy"><strong>{preview.rows[selectedRow]?.title || preview.rows[selectedRow]?.label || "原文内容"}</strong>
            <p>{Boolean(preview.rows[selectedRow]?.logical_table_scope_id || preview.rows[selectedRow]?.source_form?.startsWith("TABLE")) ? "表格请对照原页核对表头、共享单元格与完整内容。" : preview.rows[selectedRow]?.source_text || [...(preview.rows[selectedRow]?.source_content || []), ...(preview.rows[selectedRow]?.text_bindings || [])].map(c => c.text).join("\n")}</p>
          </div>
          {sourceFileUrl && <iframe title="阶段原文 PDF 对照" src={`/pdf-viewer?embedded=1&fit=page&pdf=${encodeURIComponent(sourceFileUrl)}&page=${sourcePage}`} />}
        </div>
      </div>
    </> : preview && <div aria-label={selected?.stage === "MAP" ? "阶段文档结构" : "阶段原文条目"}>
      <p>草稿 · 已显示 {preview.rows.length} / {preview.total} 项</p>
      {preview.rows.map((row, index) => <details key={row.source_record_id || row.node_id || index}
        style={{ marginInlineStart: `${Math.min(depthFor(row), 6) * 12}px` }}>
        <summary>{rowLabel(row)}
          {row.requirement_display_id ? ` · ${row.requirement_display_id}` : ""}</summary>
        <p>{row.ancestor_path?.join(" / ")}{row.pages?.length ? ` · P${row.pages.join("、")}` : ""}</p>
        {row.requirementness && <p>{row.requirementness === "REQUIREMENT" ? "需求" : "非需求"} · {row.requirement_kinds?.map(kind => ({ FUNCTIONAL: "功能", QUALITY: "质量", CONSTRAINT: "约束", PROJECT: "项目", PROCESS: "过程" }[kind] || kind)).join(" / ")}</p>}
        <div style={{ whiteSpace: "pre-wrap" }}>{row.source_text || [...(row.source_content || []), ...(row.text_bindings || [])].map(c => c.text).join("\n")}</div>
      </details>)}
      {preview.next_offset !== null && <button type="button" className="button ghost compact" disabled={pending} onClick={() => void more()}>显示更多</button>}
      {selected?.stage === "MAP" && confirmationRequired && <button type="button" className="button primary compact" disabled={pending} onClick={() => void confirm()}>确认此结构并继续</button>}
    </div>}
    {pending && <p role="status">正在同步阶段成果…</p>}
  </section>;
}
