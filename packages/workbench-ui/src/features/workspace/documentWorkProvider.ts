import type { SourceStage } from "../extraction/SourceDocumentStageResults";

export type WorkAction = "save_feedback" | "confirm_map" | "export_draft" | "start" | "steer" | "interrupt" | "resume" | "open_external_task";
export type WorkCapability = { supported: boolean; enabled: boolean; reason?: string; reason_code?: string; effect?: string };
export type PluginFeedback = { feedback_id: string; text: string; expected_revision_id: string; target_label?: string;
  created_at?: string; node_id?: string | null; source_item_ids?: string[]; status?: string; delivery_status?: string };
export type PluginVersion = { revision_id: string; number: number; stages: SourceStage[]; created_at?: string };
export type PluginState = {
  project_id?: string; document_id?: string; session_id: string; source_sha256: string; revision_id: string | null; revision_number: number;
  stages: SourceStage[]; versions: PluginVersion[]; map_confirmation_required: boolean; confirmed_map_sha256?: string;
  feedback: PluginFeedback[]; feedback_resolutions?: Record<string, { revision_id: string; reason: string; authority?: string }>;
  goal?: string | null; updated_at?: string; last_synced_at?: string;
  execution?: { state?: string; status?: string; reason?: string }; execution_state?: string;
  capabilities?: Partial<Record<WorkAction, WorkCapability>>;
  feedback_receipts?: Array<{ feedback_id: string; state?: string; status?: string; retrieval_state?: string; retrieved_at?: string | null }>;
  analysis_policy?: { status: string; reason: string } | null;
};
export function pluginCapability(state: PluginState | null, action: WorkAction, current = true): WorkCapability {
  const capability = state?.capabilities?.[action];
  if (!capability) return { supported: false, enabled: false, reason: "当前连接尚未提供此操作能力。" };
  if (!current && (action === "save_feedback" || action === "confirm_map")) return { ...capability, enabled: false, reason: "正在查看历史版本；保留草稿，切换最新成果后再提交。" };
  return { ...capability, supported: capability.supported === true, enabled: capability.supported === true && capability.enabled === true };
}
export function externalExecutionLabel(state: PluginState | null) {
  const value = state?.execution?.state || state?.execution?.status || state?.execution_state;
  return ({ RUNNING: "外部任务报告处理中", COMPLETED: "外部任务报告本轮结束", SUCCEEDED: "外部任务报告本轮结束", FAILED: "外部任务报告未完成", INTERRUPTED: "外部任务报告已暂停", CANCELLED: "外部任务报告已暂停" } as Record<string,string>)[value || ""] || "外部运行状态未报告";
}
export function pluginFeedbackLabel(state: PluginState, feedback: PluginFeedback, viewedRevision: string | null) {
  const number = state.versions.find(v => v.revision_id === viewedRevision)?.number || 0;
  const change = state.feedback_resolutions?.[feedback.feedback_id];
  if (change && (state.versions.find(v => v.revision_id === change.revision_id)?.number || Infinity) <= number) return "插件已提交修订 · 待核对";
  const receipt = state.feedback_receipts?.find(value => value.feedback_id === feedback.feedback_id);
  if (receipt?.retrieved_at && ["TAKEN", "RECEIVED", "FETCHED", "RETRIEVED"].includes(receipt.retrieval_state || "")) return "外部任务已报告取回";
  return "意见已保存 · 待外部任务取回";
}
