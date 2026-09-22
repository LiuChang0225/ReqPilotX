import type { SourceRequirementOutlineRecordV1 } from "../../shared/api/contracts";
import type { SourceItemDeliveryState } from "./SourceItemReasoning";

export type AnalysisQueue = "ALL" | "ACTION" | "EVIDENCE" | "CLARIFY" | "DEVIATION" | "HUMAN";
export const ANALYSIS_QUEUES: Array<[AnalysisQueue, string]> = [["ALL", "全部分析单元"], ["ACTION", "待处理"], ["EVIDENCE", "待内部证据"], ["CLARIFY", "待澄清"], ["DEVIATION", "有偏离"], ["HUMAN", "工程师意见"]];
export type AnalysisUnitView = { id: string; sourceId: string; displayId: string; decision: string; human: boolean; stale: boolean; searchText: string; kinds: string[] };
export function analysisUnits(records: Iterable<SourceRequirementOutlineRecordV1>, state: SourceItemDeliveryState | null): AnalysisUnitView[] {
  if (!state) return [];
  return Array.from(records).filter(record => record.requirementness === "REQUIREMENT").flatMap(record => {
    const children = state.decomposition?.records[record.source_record_id] || [];
    const units = children.length ? children.map((child, index) => ({ id: child.singular_requirement_id || child.node_id || `${record.source_record_id}:unbound:${index}`, displayId: child.requirement_display_id, text: child.text, sourceNumber: child.source_number, kinds: child.requirement_kinds || record.requirement_kinds }))
      : [{ id: record.source_record_id, displayId: record.requirement_display_id || record.source_display_id, text: record.display_label, sourceNumber: record.source_number, kinds: record.requirement_kinds }];
    return units.map(unit => {
      const manual = state.engineer_responses?.records[unit.id];
      const human = manual?.state === "CURRENT" && Boolean(manual.decision);
      const decision = human ? manual!.decision : state.assessments?.records[unit.id]?.decision || "UNKNOWN";
      return { ...unit, searchText: [unit.id, unit.displayId, unit.text, unit.sourceNumber, record.source_number, record.source_display_id, record.requirement_display_id, ...(record.ancestor_path || [])].join(" ").toLocaleLowerCase(), sourceId: record.source_record_id, decision, human, stale: manual?.state === "STALE" || Boolean(state.assessments?.carry_forward?.stale_unit_ids?.includes(unit.id) && !state.assessments?.records[unit.id]) };
    });
  });
}
export function inAnalysisQueue(unit: AnalysisUnitView, queue: AnalysisQueue) {
  if (queue === "ALL") return true;
  if (queue === "ACTION") return unit.stale || ["UNKNOWN", "PENDING_EVIDENCE", "NEEDS_CLARIFICATION"].includes(unit.decision);
  if (queue === "HUMAN") return unit.human;
  if (queue === "EVIDENCE") return unit.decision === "PENDING_EVIDENCE";
  if (queue === "CLARIFY") return unit.decision === "NEEDS_CLARIFICATION";
  return ["PARTIALLY_COMPLIANT", "NON_COMPLIANT"].includes(unit.decision);
}

export function matchesAnalysisSearch(unit: AnalysisUnitView, query: string) {
  return query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean).every(part => unit.searchText.includes(part));
}
