import type { SourceNumberFieldsV1, SourceScopeSummaryV1 } from "../../shared/api/contracts";

/** Only display the shared source-number projection; never parse IDs or labels into source numbers. */
export function originalNumber(value: SourceNumberFieldsV1 | null | undefined): string {
  if (!value) return "未提供";
  if (value.source_number?.trim()) return value.source_number;
  const numbers = value.source_numbers?.map(item => typeof item === "string" ? item : item.value).filter(Boolean) || [];
  if (numbers.length) return numbers.join("、");
  return value.source_number !== undefined || value.source_numbers !== undefined ? "无独立原编号" : "未提供";
}
export function scopeCountLabel(summary: SourceScopeSummaryV1 | undefined, direct = 0) {
  if (summary) return `原文 ${summary.subtree_source_item_count} · 需求 ${summary.subtree_requirement_count}`;
  return direct ? `直属原文 ${direct}` : "";
}
export function scopeCountDetail(summary: SourceScopeSummaryV1 | undefined) {
  return summary ? `当前已保存子树：原文 ${summary.subtree_source_item_count}，其中需求 ${summary.subtree_requirement_count}、非需求 ${summary.subtree_non_requirement_count}；本层直属正文 ${summary.direct_source_item_count}；派生 ${summary.derived_count}；分析单元 ${summary.analysis_unit_count}。计数不随折叠变化，已保存数量不等于整章应有总量。` : "仅统计本层直接拥有的原文，不是整章总量。";
}
