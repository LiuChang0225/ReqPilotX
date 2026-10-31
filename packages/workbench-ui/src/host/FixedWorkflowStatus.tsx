import type { ApplicationWorkflowDescriptor, ApplicationWorkflowState } from "./ApplicationHost";

const statusLabels: Record<string, string> = {
  PENDING: "尚未保存", IN_PROGRESS: "处理中", SAVED: "已保存", SKIPPED: "按规则跳过",
  WAITING: "等待输入", BLOCKED: "等待前置阶段",
};
const reasonLabels: Record<string, string> = {
  SOURCE_MAP_REQUIRED: "需要先保存文档结构", ITEMS_NOT_SAVED: "尚未保存原文条目",
  MAP_CONFIRMATION_REQUIRED: "等待确认这一版结构", SCOPED_DERIVATION_REMAINING: "明确授权的派生范围尚未处理完",
  NO_INTERNAL_EVIDENCE: "未提供内部依据", INTERNAL_EVIDENCE_NOT_WIRED: "当前版本尚未接入内部依据",
  INTERNAL_EVIDENCE_ANALYSIS_REQUIRED: "内部依据分析尚未保存", WORKFLOW_EVIDENCE_CONFLICT: "工作流声明与依据状态不一致",
  SELF_CHECK_NOT_SAVED: "尚无已保存自查", SELF_CHECK_BASIS_CHANGED: "自查依据已变化，需要重新核对",
  DELIVERABLE_NOT_SAVED: "尚未保存交付成果", SELF_CHECK_REQUIRED: "需要先完成自查",
  INCOMPLETE_OPERATION: "存在未完成程序操作", EVIDENCE_ANALYSIS_SKIPPED: "依据分析按声明跳过",
  EVIDENCE_BINDING_UNAVAILABLE: "冻结的内部依据当前不可用", EVIDENCE_BINDING_CHANGED: "冻结的内部依据版本已变化",
};
const evidenceLabels: Record<string, string> = {
  TASK_CONTEXT: "任务与来源", DOCUMENT_MAP: "文档结构", SOURCE_ITEMS: "原文条目",
  DECOMPOSITION: "明确派生", REVIEW_HEAD: "自查工作版", DELIVERY: "已保存交付",
  WORKFLOW_BRANCH: "声明分支",
};

export function FixedWorkflowSummary({ workflow }: { workflow?: ApplicationWorkflowDescriptor | null }) {
  if (!workflow) return <p className="agent-inline-notice" role="status">当前宿主尚未提供固定流程描述；不会用聊天目标替代阶段检查。</p>;
  return <section className="fixed-workflow-intake" aria-label="固定业务流程">
    <header><strong>{workflow.label || "客户需求整理固定流程"}</strong><span>版本 {workflow.version}</span></header>
    <dl>
      <div><dt>必需资料</dt><dd>{workflow.required_inputs?.map(item => item.label).join("、") || "客户需求文档"}</dd></div>
      <div><dt>可选内部依据</dt><dd>{workflow.optional_inputs?.length
        ? workflow.optional_inputs.map(item => `${item.label}：${item.available ? "可选择" : item.reason || "当前不可用"}`).join("；")
        : "宿主未提供项目参考或团队知识库。"}</dd></div>
      <div><dt>预期成果</dt><dd>{workflow.expected_outputs?.join("；") || "结构、原文条目、分类、自查与可核对成果。"}</dd></div>
    </dl>
    <ol className="fixed-workflow-stage-list">{workflow.stages.map(stage => <li key={stage.id}>{stage.label}</li>)}</ol>
  </section>;
}

export function FixedWorkflowStatus({ state, unconfirmed }: { state?: ApplicationWorkflowState | null;
  unconfirmed?: { reason: string; message?: string } | null }) {
  if (!state) return <p className="agent-inline-notice" role="status">{unconfirmed
    ? `固定流程状态未确认：${unconfirmed.message || unconfirmed.reason}。已保存成果仍保留，请重新读取任务后再核对。`
    : "固定流程状态尚未保存；任务尚未开始或成果尚未同步。"}</p>;
  if (state.verification === "UNCONFIRMED") return <section className="fixed-workflow-status" aria-label="固定流程成果">
    <header><strong>业务成果阶段</strong><span>状态未确认</span></header>
    <p role="status">固定流程状态未确认：{(state as ApplicationWorkflowState & { unconfirmed?: { reason?: string } }).unconfirmed?.reason || "派生状态与保存工件不一致"}。已保存成果仍保留，请重新读取任务后再核对。</p>
  </section>;
  return <section className="fixed-workflow-status" aria-label="固定流程成果">
    <header><strong>业务成果阶段</strong><span>流程 v{state.workflow.version}</span></header>
    <ol>{state.stages.map(stage => <li key={stage.id} data-status={stage.status}>
      <span>{stage.label}</span><strong>{statusLabels[stage.status] || stage.status}</strong>
      {stage.reason && <small>{reasonLabels[stage.reason] || stage.reason}</small>}
      {stage.evidence?.length ? <small>{stage.evidence.map(item => evidenceLabels[item.kind] || item.kind).join("、")}</small> : null}
    </li>)}</ol>
    {(state as ApplicationWorkflowState & { knowledge_evidence?: { mode?: string; status?: string; message?: string } }).knowledge_evidence && <p className={((state as ApplicationWorkflowState & { knowledge_evidence?: { status?: string } }).knowledge_evidence?.status || "UNCHECKED") === "CURRENT" ? "" : "agent-inline-notice"} role="status">
      内部依据：{(state as ApplicationWorkflowState & { knowledge_evidence?: { mode?: string; status?: string; message?: string } }).knowledge_evidence?.mode || "NONE"} · {((state as ApplicationWorkflowState & { knowledge_evidence?: { status?: string } }).knowledge_evidence?.status || "UNCHECKED") === "CURRENT" ? "任务冻结版本可读取" : (state as ApplicationWorkflowState & { knowledge_evidence?: { message?: string } }).knowledge_evidence?.message || "任务冻结版本未确认"}。已保存分析建议仍按当时引用保留。
    </p>}
    <p role="status">{state.ready_to_review ? "已保存成果满足当前可核对条件；人工接受仍未声称。" : `下一步：${state.next_stage ? state.stages.find(stage => stage.id === state.next_stage)?.label || state.next_stage : "继续处理未决项"}`}</p>
    {state.open_items?.length ? <p className="agent-inline-notice">未决项：{state.open_items.map(item => reasonLabels[item] || item).join("；")}</p> : null}
  </section>;
}
