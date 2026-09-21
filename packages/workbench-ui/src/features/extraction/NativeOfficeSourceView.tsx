import { useEffect, useMemo, useRef, useState } from "react";
import { request } from "../../shared/api/client";
import type { NativeStructureTablePreviewV1 } from "../../shared/api/contracts";
import { tableDisplayTitle } from "../restoration/tableDisplay";
import "./NativeOfficeSourceView.css";

export interface NativeOfficeLocator {
  format: "docx" | "xlsx";
  body_element_index?: number;
  paragraph_index?: number | null;
  table_index?: number | null;
  row_index?: number | null;
  column_index?: number | null;
  object_index?: number | null;
  sheet?: string;
  cell_range?: string;
}

export function officeLocation(value: unknown): NativeOfficeLocator | null {
  if (!value || typeof value !== "object") return null;
  const locator = value as NativeOfficeLocator;
  return locator.format === "docx" || locator.format === "xlsx" ? locator : null;
}

export function officeLocationLabel(value: unknown): string {
  const loc = officeLocation(value);
  if (!loc) return "原件位置";
  if (loc.format === "xlsx") return `${loc.sheet || "工作表"}!${loc.cell_range || ""}`;
  const label = loc.table_index != null ? `表 ${loc.table_index + 1}`
    : loc.paragraph_index != null ? `段落 ${loc.paragraph_index + 1}`
      : `正文对象 ${(loc.body_element_index ?? 0) + 1}`;
  const cell = loc.row_index != null && loc.column_index != null ? ` · R${loc.row_index + 1}C${loc.column_index + 1}` : "";
  return label + cell + (loc.object_index != null ? ` · 图 ${loc.object_index + 1}` : "");
}

interface OfficeCell {
  cell_id: string;
  row: number;
  column: number;
  row_span: number;
  column_span: number;
  raw_text?: string;
  text?: string;
  formula?: string | null;
  display_value?: string | null;
  native_locator?: unknown;
}
interface OfficeTable { row_count: number; column_count: number; cells: OfficeCell[] }
interface OfficeNode {
  source_node_id: string;
  parent_source_node_id: string | null;
  kind: string;
  source_sequence: number;
  native_locator: NativeOfficeLocator;
  text: string;
  table: OfficeTable | null;
  image: { url: string; content_sha256: string } | null;
}
export interface NativeOfficeSourcePayload {
  schema_version: "reqpilot-native-source-view/v1";
  source_sha256: string;
  source_format: "docx" | "xlsx";
  source_file_name: string;
  authority: "ORIGINAL_SOURCE_NATIVE_CAPTURE";
  status: string;
  nodes: OfficeNode[];
  diagnostics: unknown[];
}

export function NativeOfficeGrid({ table, label, focusedCells = [] }: {
  table: OfficeTable; label: string; focusedCells?: readonly string[];
}) {
  const [limit, setLimit] = useState(100);
  useEffect(() => setLimit(100), [table]);
  const rows = useMemo(() => {
    const result = new Map<number, OfficeCell[]>();
    for (const cell of table.cells) result.set(cell.row, [...(result.get(cell.row) || []), cell]);
    return result;
  }, [table]);
  return <div className="native-office-grid">
    <div className="native-office-grid-scroll" tabIndex={0} role="region" aria-label={`${label} · 可横向滚动`}>
      <table><caption>{label}</caption><tbody>
        {Array.from({ length: Math.min(table.row_count, limit) }, (_, row) => <tr key={row}>
          {(rows.get(row) || []).slice().sort((a, b) => a.column - b.column).map(cell => <td key={cell.cell_id}
            rowSpan={cell.row_span} colSpan={cell.column_span}
            className={focusedCells.includes(cell.cell_id) ? "is-focused" : undefined}
            title={officeLocationLabel(cell.native_locator)}>
            <span>{cell.raw_text ?? cell.text ?? ""}</span>
            {cell.formula && <small>公式：{cell.formula}</small>}
          </td>)}
        </tr>)}
      </tbody></table>
    </div>
    {limit < table.row_count && <button type="button" className="button secondary compact"
      onClick={() => setLimit(value => value + 100)}>继续显示原表（已显示 {limit} / {table.row_count} 行）</button>}
  </div>;
}

export function NativeOfficePreviewTable({ preview, focusedCells = [] }: {
  preview: NativeStructureTablePreviewV1; focusedCells?: readonly string[];
}) {
  return <section className="native-office-tables" aria-label="当前原文条目的表格内容">
    {preview.fragments.map(fragment => <NativeOfficeGrid key={fragment.fragment_id}
      table={fragment} focusedCells={focusedCells}
      label={`${tableDisplayTitle(preview)} · ${officeLocationLabel(fragment.native_locator)}`} />)}
  </section>;
}

export function nativeOfficeObjectUrl(projectId: string, documentId: string, sourceSha: string,
  sourceHandle: string, contentSha: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}`
    + `/source-capture/native-objects/${encodeURIComponent(sourceHandle)}`
    + `?source_sha256=${encodeURIComponent(sourceSha)}&content_sha256=${encodeURIComponent(contentSha)}`;
}

export function NativeOfficeSourceView({ projectId, documentId, sourceSha256, sourceFileUrl,
  focusIds = [], imageOnlyId }: {
  projectId: string; documentId: string; sourceSha256: string; sourceFileUrl?: string;
  focusIds?: readonly string[]; imageOnlyId?: string;
}) {
  const key = `${projectId}/${documentId}/${sourceSha256}`;
  const [loaded, setLoaded] = useState<{ key: string; value: NativeOfficeSourcePayload } | null>(null);
  const [failure, setFailure] = useState<{ key: string; message: string } | null>(null);
  const [retry, setRetry] = useState(0);
  const [sheetChoice, setSheet] = useState<{ key: string; sheet: string } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const value = loaded?.key === key ? loaded.value : null;
  const error = failure?.key === key ? failure.message : "";
  const focusKey = focusIds.join("\u001f");
  useEffect(() => {
    const controller = new AbortController();
    setFailure(null);
    void request<NativeOfficeSourcePayload>(`/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}`
      + `/source-capture/native-view?source_sha256=${encodeURIComponent(sourceSha256)}`, { signal: controller.signal })
      .then(result => {
        if (controller.signal.aborted) return;
        if (result.source_sha256 !== sourceSha256 || result.authority !== "ORIGINAL_SOURCE_NATIVE_CAPTURE"
          || !["docx", "xlsx"].includes(result.source_format)) throw new Error("原件版本已更新，请同步后重新查看。");
        setLoaded({ key, value: result });
      }).catch(caught => {
        if (!controller.signal.aborted) setFailure({ key, message: caught instanceof Error ? caught.message : "原件暂时无法读取。" });
      });
    return () => controller.abort();
  }, [projectId, documentId, sourceSha256, key, retry]);
  const nodes = useMemo(() => {
    if (!value) return [];
    const tables = new Set(value.nodes.filter(node => node.table).map(node => node.source_node_id));
    // Cell paragraphs are already present in the original merged grid; embedded
    // images and nested tables remain visible as their own captured objects.
    return value.nodes.filter(node => node.table || node.image || !tables.has(node.parent_source_node_id || ""))
      .slice().sort((a, b) => a.source_sequence - b.source_sequence);
  }, [value]);
  const sheets = Array.from(new Set(nodes.map(node => node.native_locator.sheet).filter((sheet): sheet is string => Boolean(sheet))));
  const focusedNode = nodes.find(node => focusIds.includes(node.source_node_id));
  const chosenSheet = sheetChoice?.key === `${key}/${focusKey}` ? sheetChoice.sheet : focusedNode?.native_locator.sheet || sheets[0] || "";
  const visible = imageOnlyId ? nodes.filter(node => node.image && node.source_node_id === imageOnlyId)
    : nodes.filter(node => !chosenSheet || node.native_locator.sheet === chosenSheet);
  useEffect(() => {
    rootRef.current?.querySelector<HTMLElement>('[data-source-focused="true"]')?.scrollIntoView?.({ block: "nearest" });
  }, [value, key, focusKey, chosenSheet]);
  if (error) return <div role="alert" className="native-office-state">{error}
    <button type="button" onClick={() => setRetry(n => n + 1)}>重新读取原件</button></div>;
  if (!value) return <p role="status" className="native-office-state">正在读取原件内容…</p>;
  return <div ref={rootRef} className="native-office-source" aria-label={imageOnlyId ? "Office 原图" : "Office 原件对照"}>
    {!imageOnlyId && <header className="native-office-header"><div><strong>{value.source_format === "docx" ? "Word 原文" : "Excel 原文"}</strong>
      <small>{value.source_file_name} · 原生内容</small></div>
      {sourceFileUrl && <a href={sourceFileUrl} download className="button ghost compact">下载原件</a>}
    </header>}
    {!imageOnlyId && sheets.length > 1 && <nav className="native-office-sheets" aria-label="原件工作表">
      {sheets.map(sheet => <button type="button" key={sheet} aria-pressed={chosenSheet === sheet}
        onClick={() => setSheet({ key: `${key}/${focusKey}`, sheet })}>{sheet}</button>)}
    </nav>}
    <div className="native-office-content">{visible.map(node => <article key={node.source_node_id}
      data-source-focused={focusIds.includes(node.source_node_id) || undefined}>
      <small className="native-office-location">{officeLocationLabel(node.native_locator)}</small>
      {node.table ? <NativeOfficeGrid table={node.table} label={officeLocationLabel(node.native_locator)} />
        : node.image ? <figure><img src={node.image.url} alt={`原图 · ${officeLocationLabel(node.native_locator)}`} loading="lazy" />
          <figcaption>原件嵌入图片</figcaption></figure>
          : node.kind === "heading" ? <h4>{node.text}</h4> : <p>{node.text}</p>}
    </article>)}</div>
    {visible.length === 0 && <p role="status">当前范围没有可显示的原生对象。</p>}
    {value.status !== "COMPLETE" && <p role="status">部分原件内容未能读取，请结合下载的原件核查。</p>}
  </div>;
}
