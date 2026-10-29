/** Ephemeral display projection. Never changes SourceIR or writes parser artifacts. */
import type { BBox, Run, Selection, SourceNode } from "./contracts";

export type Layer = "native" | "rules";
export type DisplayKind = "body" | "heading" | "table" | "image" | "header" | "footer" | "watermark" | "unknown";
export const KINDS: Record<DisplayKind, { label: string; color: string }> = {
  body: { label: "正文", color: "#2855b4" }, heading: { label: "标题", color: "#793ebc" },
  table: { label: "表格", color: "#007f75" }, image: { label: "图片", color: "#a05e00" },
  header: { label: "页眉", color: "#586570" }, footer: { label: "页脚", color: "#9b3f37" },
  watermark: { label: "疑似水印", color: "#ae3268" }, unknown: { label: "未分类", color: "#8b6a33" },
};
export interface DisplayElement extends Selection {
  text: string; kind: DisplayKind; order: number; sourceOrder: number; node?: SourceNode;
  origin: string; reasons: string[]; noise: boolean; granularity: string;
  canvasOmitted?: string;
}
const isNoise = (kind: DisplayKind) => ["header", "footer", "watermark"].includes(kind);
function textKey(text: string) {
  const value = text.normalize("NFKC").trim().replace(/\s+/g, " ").toLowerCase();
  const pageCounter = /^(?:(?:page|第)\s*)?\d+\s*(?:(?:\/|of)\s*\d+|页)?$/.test(value) ||
    /^第\s*\d+\s*页\s*[,，/·]?\s*共\s*\d+\s*页$/.test(value);
  return pageCounter ? "<page-counter>" : value;
}
export function presentation(run: Run, layer: Layer): DisplayElement[] {
  const elements: DisplayElement[] = (run.source_ir?.nodes || []).map(node => {
    const nativeSemantic = run.parser === "docling";
    const semantic: Record<string, DisplayKind> = { heading: "heading", header: "header", footer: "footer", watermark: "watermark", paragraph: "body", list_item: "body", note: "body" };
    const kind = node.table_payload ? "table" : node.asset_ref ? "image" : nativeSemantic ? semantic[node.kind] || "unknown" : "unknown";
    return { id: node.source_node_id, page: node.native_locator.page_number, bbox: node.native_locator.bbox,
      text: node.raw_text, node, kind, order: node.source_order+1, sourceOrder: node.source_order+1,
      origin: nativeSemantic ? "Docling 原生标签 → SourceIR" : "解析器事实 → SourceIR 映射",
      reasons: [kind === "unknown" ? `原始映射 kind=${node.kind}；当前解析器不提供可靠的文档语义类别。` : "使用实际表格结构、图片资产或原生标签。",
        "SourceIR 顺序由 adapter 保存；不等于原生语义阅读顺序。"],
      noise: isNoise(kind), granularity: node.table_payload ? "表格／原生单元格框" : node.asset_ref ? "图片区域" : node.pdf_text_lines?.length ? "原生行框；选择绑定源块（非字级）" : "段落／块框（不是字级定位）" };
  });
  for (const snapshot of run.source_ir?.visual_text_snapshots || []) for (const word of snapshot.observations) {
    elements.push({ id: word.observation_id, page: snapshot.page_number, bbox: word.bbox, text: word.raw_text,
      kind: "unknown", order: elements.length+1, sourceOrder: elements.length+1, origin: "OCR 独立视觉观察",
      reasons: ["仅有文字与观察坐标；未原生判定正文、标题或噪声。"], noise: false, granularity: "OCR 观察框" });
  }
  if (layer === "native") return elements;
  const heights = elements.filter(e => e.text && e.bbox && !e.text.includes("\n")).map(e => e.bbox![3]-e.bbox![1]).sort((a,b) => a-b);
  const medianHeight = heights[Math.floor(heights.length/2)] || 12;
  function band(e: DisplayElement) {
    const page = run.pages.find(p => p.page_number === e.page);
    if (!page || !e.bbox) return "none";
    const y = ((e.bbox[1]+e.bbox[3])/2-page.bbox[1])/(page.bbox[3]-page.bbox[1]);
    return y <= .1 ? "top" : y >= .9 ? "bottom" : "middle";
  }
  const repeated = new Map<string, Set<number>>();
  for (const e of elements) if (e.text.trim() && e.text.length <= 160) {
    const key = `${band(e)}:${textKey(e.text)}`;
    if (!repeated.has(key)) repeated.set(key, new Set());
    repeated.get(key)!.add(e.page);
  }
  const result = elements.map(e => {
    if (["table", "image"].includes(e.kind)) return { ...e, reasons: [...e.reasons] };
    const page = run.pages.find(p => p.page_number === e.page);
    const b = band(e), count = repeated.get(`${b}:${textKey(e.text)}`)?.size || 0;
    let kind: DisplayKind = "unknown", rule = "pl-text-display/v1", reason = "无可靠文本或定位，保留未分类。";
    if (e.text.trim()) { kind = "body"; reason = "其余可读文字归入正文展示；不作业务语义结论。"; }
    if (count >= 2 && count >= run.pages.length*.5 && ["top", "bottom"].includes(b)) {
      kind = b === "top" ? "header" : "footer"; rule = "pl-repeat-margin/v1";
      reason = `同一文本／页码模板重复于 ${count}/${run.pages.length} 页且位于${b === "top" ? "顶部" : "底部"} 10% 区域，仅为候选。`;
    } else if (e.node && ["header", "footer", "watermark"].includes(e.node.kind)) {
      kind = e.node.kind as DisplayKind; rule = "source-ir-adapter-tag/v1";
      reason = `既有 adapter 标记 kind=${e.node.kind}，作为候选展示；不冒称解析器原生分类。`;
    } else if (count >= 2 && b === "middle" && page && e.bbox && e.text.length <= 40 &&
      e.bbox[2]-e.bbox[0] >= (page.bbox[2]-page.bbox[0])*.35 && e.bbox[3]-e.bbox[1] >= (page.bbox[3]-page.bbox[1])*.1) {
      kind = "watermark"; rule = "pl-large-repeated-centre/v1"; reason = "跨页重复的大幅中央短文本，可能为水印；未确定，不自动删除。";
    } else if (e.node?.kind === "heading" || (e.bbox && e.text.length <= 80 && !e.text.includes("\n") && e.bbox[3]-e.bbox[1] >= medianHeight*1.35)) {
      kind = "heading"; rule = "pl-heading-candidate/v1"; reason = "既有标题映射或短单行框高于中位行高 1.35 倍，仅为标题候选。";
    }
    return { ...e, kind, noise: isNoise(kind), origin: `二次规则 ${rule}`, reasons: [reason] };
  });
  const tables = result.filter(e => e.node?.table_payload && e.bbox);
  for (const element of result) if (element.text.trim() && element.bbox && element.kind !== "table") {
    const duplicate = tables.find(table => table.page === element.page && table.bbox![0] <= element.bbox![0] &&
      table.bbox![1] <= element.bbox![1] && table.bbox![2] >= element.bbox![2] && table.bbox![3] >= element.bbox![3] &&
      textKey(table.node!.table_payload!.cells.map(c => c.raw_text).join(" ")).includes(textKey(element.text)));
    if (duplicate) {
      element.canvasOmitted = "pl-table-text-overlap/v1";
      element.reasons.push(`pl-table-text-overlap/v1：文字位于表格 ${duplicate.id} 内且与单元格文字匹配；仅在画布折叠重复块，列表与 raw 保留。`);
    }
  }
  result.sort((a,b) => a.page-b.page || (a.bbox?.[1] ?? Infinity)-(b.bbox?.[1] ?? Infinity) ||
    (a.bbox?.[0] ?? Infinity)-(b.bbox?.[0] ?? Infinity) || a.sourceOrder-b.sourceOrder);
  return result.map((e,i) => ({ ...e, order: i+1, reasons: [...e.reasons,
    "pl-geometric-order/v1：按页、从上到下、从左到右的展示顺序候选；不保证多栏语义阅读顺序。"] }));
}
export function elementLabel(e: DisplayElement, layer: Layer) {
  return KINDS[e.kind].label + (layer === "rules" && !["table", "image", "unknown"].includes(e.kind) ? "候选" : "");
}
export function boxStyle(bbox: BBox, page: BBox) {
  return { left: `${(bbox[0]-page[0])/(page[2]-page[0])*100}%`, top: `${(bbox[1]-page[1])/(page[3]-page[1])*100}%`,
    width: `${(bbox[2]-bbox[0])/(page[2]-page[0])*100}%`, height: `${(bbox[3]-bbox[1])/(page[3]-page[1])*100}%` };
}
export function fittedFont(text: string, bbox: BBox) {
  const lines = text.split("\n");
  const widthUnits = Math.max(1, ...lines.map(line => Array.from(line).reduce((sum,c) => sum + (/[\u2e80-\uffff]/.test(c) ? 1 : .56),0)));
  return Math.max(3, Math.min(12, (bbox[3]-bbox[1]) / Math.max(1,lines.length) * .82, (bbox[2]-bbox[0])/widthUnits));
}
