import { useEffect, useState } from "react";
import { ApiError, request } from "../../shared/api/client";
import type { AgentLoopTask } from "../extraction/SourceItemAgentLoop";
import type { SourceItemDeliveryState } from "../extraction/SourceItemReasoning";

export const isAgentTaskActive = (task: AgentLoopTask | null | undefined) => Boolean(task && ["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(task.job_status));
export function agentTaskLabel(task: AgentLoopTask | null | undefined): string {
  if (!task?.task_id) return "尚未发起任务";
  if (task.job_status === "CANCEL_REQUESTED") return "正在暂停";
  if (task.job_status === "FAILED") return "本轮未完成";
  if (task.job_status === "CANCELLED" || task.runtime_status === "INTERRUPTED") return "已暂停";
  if (task.map_confirmation_required) return "待核对结构";
  if (task.job_status === "PENDING") return "已排队";
  if (task.job_status === "RUNNING") return "处理中";
  if (task.result?.application_saved) return "工作版已保存";
  if (task.result?.status === "NEEDS_CONTINUATION") return "等待继续";
  return task.job_status === "SUCCEEDED" ? "本轮已结束" : "状态待同步";
}
export function agentProgressLabel(message?: string): string | undefined {
  return message?.replace(/^Agent 正在调用 (reqpilot_[a-z]+)$/, (_text, tool: string) => ({
    reqpilot_context: "正在读取当前任务", reqpilot_skill: "正在核对处理方法", reqpilot_read: "正在查阅原文与成果",
    reqpilot_candidate: "正在整理修订建议", reqpilot_run: "正在检查与整理成果", reqpilot_publish: "正在保存工作版",
  }[tool] || "正在处理当前任务"));
}
export type DocumentWorkSummary = {
  sourceSha256: string;
  delivery: SourceItemDeliveryState | null;
  task: AgentLoopTask | null;
  pluginDraft?: { session_id: string; source_sha256: string; revision_id: string | null; revision_number: number; workbench_path: string; map_confirmation_required?: boolean } | null;
  deliveryError: string;
  taskError: string;
};
export function documentDeliveryLabel(summary: DocumentWorkSummary | null): string {
  if (!summary) return "正在读取成果…";
  if (summary.delivery) return `原文工作版 v${summary.delivery.baseline_revision}`;
  if (summary.task?.stages?.some(stage => stage.stage === "ITEMS")) return "条目阶段草稿";
  if (summary.task?.stages?.some(stage => stage.stage === "MAP")) return "结构阶段草稿";
  if (summary.pluginDraft) return summary.pluginDraft.revision_number ? `插件草稿 v${summary.pluginDraft.revision_number}` : "插件已连接";
  return summary.deliveryError ? "成果状态待同步" : "尚无原文工作版";
}
export function documentWorkCheckpoint(summary: DocumentWorkSummary | null): "list" | "structure" {
  return summary?.delivery ? "list" : "structure";
}
export async function readDocumentWorkSummary(projectId: string, documentId: string, sourceSha256: string, signal: AbortSignal): Promise<DocumentWorkSummary | null> {
  const root = `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/delivery`;
      const [delivery, latest, plugin] = await Promise.allSettled([
        request<SourceItemDeliveryState>(`${root}/state`, { signal: signal }),
        request<AgentLoopTask | null>(`${root}/loop/latest`, { signal: signal }),
        request<DocumentWorkSummary["pluginDraft"]>(`${root}/plugin-workbench/latest`, { signal: signal }),
      ]);
      if (signal.aborted) return null;
      const result: DocumentWorkSummary = { sourceSha256, delivery: null, task: null, deliveryError: "", taskError: "" };
      if (plugin.status === "fulfilled" && plugin.value?.source_sha256 === sourceSha256) result.pluginDraft = plugin.value;
      if (delivery.status === "fulfilled") {
        if (delivery.value?.source_sha256 === sourceSha256 && delivery.value.baseline_sha256) result.delivery = delivery.value;
        else result.deliveryError = "成果与当前来源不一致";
      } else if (!(delivery.reason instanceof ApiError && (delivery.reason.status === 404 || delivery.reason.status === 409 && String(delivery.reason.message).includes("原文条目尚未生成")))) {
        result.deliveryError = "暂时无法读取工作版";
      }
      if (latest.status === "fulfilled" && latest.value?.task_id) {
        if (!latest.value.source_sha256 || latest.value.source_sha256 === sourceSha256) result.task = latest.value;
        else result.taskError = "任务属于其他来源版本";
      } else if (latest.status === "rejected" && !(latest.reason instanceof ApiError && latest.reason.status === 404)) {
        result.taskError = "任务状态暂未同步";
      }
  return result;
}
export function useDocumentWorkSummary(projectId: string, documentId: string, sourceSha256: string, revision = 0) {
  const identity = `${projectId}:${documentId}:${sourceSha256}`;
  const [state, setState] = useState<{ identity: string; value: DocumentWorkSummary } | null>(null);
  useEffect(() => {
    if (!projectId || !documentId || !sourceSha256) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const root = `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/delivery`;
    const read = async () => {
      const result = await readDocumentWorkSummary(projectId, documentId, sourceSha256, abort.signal);
      if (!result || abort.signal.aborted) return;
      setState({ identity, value: result });
      timer = setTimeout(() => void read(), isAgentTaskActive(result.task) ? 5000 : 30000);
    };
    void read();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [identity, projectId, documentId, sourceSha256, revision]);
  return state?.identity === identity ? state.value : null;
}
