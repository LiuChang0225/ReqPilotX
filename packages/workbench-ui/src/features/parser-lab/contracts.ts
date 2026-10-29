/** Read projections of source-ir/v1; the server validates the canonical model. */
export type BBox = [number, number, number, number];
export interface Locator { page_number: number; bbox?: BBox | null; block_index?: number | null; table_index?: number | null }
export interface Cell { cell_id: string; row: number; column: number; row_span: number; column_span: number; raw_text: string; native_locator: Locator }
export interface SourceNode {
  source_node_id: string; kind: string; source_order: number; raw_text: string; native_locator: Locator;
  asset_ref?: string | null;
  pdf_text_lines?: { start_offset: number; end_offset: number; bbox: BBox }[];
  table_payload?: { row_count: number; column_count: number; cells: Cell[] } | null;
}
export interface SourceIR {
  nodes: SourceNode[]; assets: { asset_id: string; storage_key: string }[];
  visual_text_snapshots?: { page_number: number; observations: { observation_id: string; observation_index: number; bbox: BBox; raw_text: string; confidence?: number | null }[] }[];
}
export interface ParserInfo { id: string; name: string; version: string | null; availability: string; reason?: string }
export interface Artifact { id: string; path: string; media_type: string; byte_count: number; sha256: string }
export interface Run {
  run_id: string; filename: string; source_sha256: string; parser: string; parser_version: string | null;
  adapter_id: string | null; adapter_version: string | null; status: string; started_at: string; elapsed_seconds?: number; config?: Record<string, unknown>;
  pages: { page_number: number; bbox: BBox; rotation: number; cropbox: BBox; render_artifact: string }[];
  diagnostics: { code: string; message: string }[]; artifacts: Artifact[]; source_ir?: SourceIR | null;
}
export interface Selection { id: string; page: number; bbox?: BBox | null; ownerId?: string }
export const artifactUrl = (run: Run, id: string) => `/api/parser-lab/runs/${run.run_id}/artifacts/${encodeURIComponent(id)}`;
export function locations(run: Run): (Selection & { label: string })[] {
  const native = (run.source_ir?.nodes || []).map(n => ({ id: n.source_node_id, page: n.native_locator.page_number,
    bbox: n.native_locator.bbox, label: `${n.source_order + 1} · ${n.kind}` }));
  const cells = (run.source_ir?.nodes || []).flatMap(n => (n.table_payload?.cells || []).map(c => ({
    id: c.cell_id, page: c.native_locator.page_number, bbox: c.native_locator.bbox, label: `单元格 ${c.row+1},${c.column+1}` })));
  const visual = (run.source_ir?.visual_text_snapshots || []).flatMap(s => s.observations.map(o => ({
    id: o.observation_id, page: s.page_number, bbox: o.bbox, label: `OCR ${o.observation_index+1}` })));
  return [...native, ...cells, ...visual];
}
