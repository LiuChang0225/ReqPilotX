import { useEffect, useState } from "react";
import { request } from "../../shared/api/client";
import type { SourceRequirementBaselineOutlineV1, SourceItemLedgerRowV1 } from "../../shared/api/contracts";

export type AnalysisFocus = "TECHNICAL_PARAMETERS" | "VERIFICATION";
export const FOCUS_LABELS: Record<AnalysisFocus, string> = { TECHNICAL_PARAMETERS: "技术参数", VERIFICATION: "试验验证" };
export type SourceItemReviewRecord = {
  source_item_id: string; source_display_id: string; requirement_display_id: string | null;
  search_text: string; topics: string[]; focus_areas: AnalysisFocus[]; focus_reason: string;
  needs_attention: boolean; attention_reasons: string[];
  changed_from_previous: boolean | null; change_summary: string;
  previous_judgment: { requirementness: string; requirement_kinds: string[]; non_requirement_role: string | null; decision_summary: string } | null;
  derived_count: number; has_visual_interpretation: boolean; feedback_count: number; pending_feedback_count: number;
};
export type SourceItemReviewContext = {
  source_sha256: string; baseline_sha256: string; baseline_revision: number;
  records: Record<string, SourceItemReviewRecord>;
  history_status: "AVAILABLE" | "UNKNOWN";
  focus_annotation_status: "BOUND" | "PARTIAL" | "UNAVAILABLE";
  capabilities?: { text_scope_revision: ReviewCapability; table_classification_review: ReviewCapability; table_scope_revision: ReviewCapability };
};
export type ReviewCapability = { supported: boolean; available: boolean; endpoint?: string; limitation?: string; unavailable_reason?: string };

export function useSourceItemReviewContext(projectId: string, documentId: string, baselineSha: string, refreshToken = 0) {
  const [result, setResult] = useState<{ key: string; value: SourceItemReviewContext | null; error: string }>({ key: "", value: null, error: "" });
  const key = `${projectId}:${documentId}:${baselineSha}`;
  useEffect(() => {
    if (!baselineSha) return;
    const controller = new AbortController();
    void request<SourceItemReviewContext>(`/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/delivery/review-context?expected_baseline_sha256=${encodeURIComponent(baselineSha)}`, { signal: controller.signal })
      .then(value => {
        if (controller.signal.aborted) return;
        if (value.baseline_sha256 !== baselineSha) throw new Error("审阅索引版本已变化，请同步清单。");
        setResult({ key, value, error: "" });
      }).catch(error => { if (!controller.signal.aborted) setResult({ key, value: null, error: error.message || "审阅索引暂不可用" }); });
    return () => controller.abort();
  }, [projectId, documentId, baselineSha, key, refreshToken]);
  return result.key === key ? result : { key, value: null, error: "" };
}

export type ReviewFilter = "ALL" | "REQUIREMENTS" | "FOCUS" | "PARAMETERS" | "VERIFICATION" | "ATTENTION" | "CHANGED" | "DERIVED" | "IMAGES";
export type ReviewFilters = { query: string; view: ReviewFilter; category: string; topic: string };

export type RelatedReviewItem = { search_text: string; requirement_kinds: readonly string[] };

export function filterSourceItemLedger(outline: SourceRequirementBaselineOutlineV1 | null,
  context: SourceItemReviewContext | null, filters: ReviewFilters, relatedItems: ReadonlyMap<string, readonly RelatedReviewItem[]> = new Map()) {
  const matched = new Set<string>(), visible = new Set<string>(), ancestors = new Set<string>();
  if (!outline) return { matched, visible, ancestors, active: false };
  const active = Boolean(filters.query.trim() || filters.view !== "ALL" || filters.category || filters.topic);
  const queries = filters.query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const rows = new Map(outline.ledger.rows.map(row => [row.row_id, row]));
  const sourceRows = new Map(outline.ledger.rows.filter(row => row.source_item_id).map(row => [row.source_item_id!, row]));
  for (const record of outline.records) {
    const info = context?.records[record.source_record_id];
    const text = [record.source_display_id, record.requirement_display_id, record.source_number, ...(record.source_numbers || []).map(number => typeof number === "string" ? number : number.value), record.display_label, record.label,
      ...record.ancestor_path, info?.search_text || ""].filter(Boolean).join(" ").toLocaleLowerCase();
    const matches = (value: string, kinds: readonly string[]) => queries.every(query => value.includes(query)) && (!filters.category || kinds.includes(filters.category));
    const shared = [record.source_display_id, record.requirement_display_id, record.source_number, ...record.ancestor_path].filter(Boolean).join(" ");
    if (!matches(text, record.requirement_kinds) && !(relatedItems.get(record.source_record_id) || []).some(child =>
      matches(`${shared} ${child.search_text}`.toLocaleLowerCase(), child.requirement_kinds))) continue;
    if (filters.topic && !(info?.topics || []).includes(filters.topic)) continue;
    if (filters.view === "REQUIREMENTS" && record.requirementness !== "REQUIREMENT") continue;
    if (filters.view === "FOCUS" && !info?.focus_areas.length) continue;
    if (filters.view === "PARAMETERS" && !info?.focus_areas.includes("TECHNICAL_PARAMETERS")) continue;
    if (filters.view === "VERIFICATION" && !info?.focus_areas.includes("VERIFICATION")) continue;
    if (filters.view === "ATTENTION" && !info?.needs_attention && !info?.pending_feedback_count) continue;
    if (filters.view === "CHANGED" && info?.changed_from_previous !== true) continue;
    if (filters.view === "DERIVED" && !info?.derived_count && !relatedItems.get(record.source_record_id)?.length) continue;
    if (filters.view === "IMAGES" && !info?.has_visual_interpretation) continue;
    matched.add(record.source_record_id);
    let row = sourceRows.get(record.source_record_id);
    if (row) visible.add(row.row_id);
    while (row?.parent_row_id) {
      ancestors.add(row.parent_row_id); visible.add(row.parent_row_id); row = rows.get(row.parent_row_id);
    }
  }
  if (!active) for (const row of outline.ledger.rows) visible.add(row.row_id);
  return { matched, visible, ancestors, active };
}

export function sourceReviewCounts(rows: readonly SourceItemLedgerRowV1[], context: SourceItemReviewContext | null) {
  const byId = new Map(rows.map(row => [row.row_id, row]));
  const counts = new Map<string, { focus: number; attention: number; changed: number }>();
  const visit = (id: string): { focus: number; attention: number; changed: number } => {
    if (counts.has(id)) return counts.get(id)!;
    const row = byId.get(id); const info = row?.source_item_id ? context?.records[row.source_item_id] : null;
    const count = { focus: info?.focus_areas.length ? 1 : 0, attention: info?.needs_attention || info?.pending_feedback_count ? 1 : 0, changed: info?.changed_from_previous === true ? 1 : 0 };
    if (row) for (const child of row.child_row_ids) { const nested = visit(child); count.focus += nested.focus; count.attention += nested.attention; count.changed += nested.changed; }
    counts.set(id, count); return count;
  };
  for (const row of rows) visit(row.row_id);
  return counts;
}
