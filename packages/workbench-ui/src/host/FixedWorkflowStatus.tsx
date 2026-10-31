import { useId, useRef, useState } from "react";
import type { ApplicationWorkflowDescriptor, ApplicationWorkflowState } from "./ApplicationHost";
import { useUiCopy } from "./HostLocaleContext";

const statusLabels: Record<string, string> = {
  PENDING: "尚未保存", IN_PROGRESS: "处理中", SAVED: "已保存", SKIPPED: "按规则跳过",
  WAITING: "等待输入", BLOCKED: "等待前置阶段",
};
const reasonLabels: Record<string, string> = {
  SOURCE_MAP_REQUIRED: "需要先保存文档结构", ITEMS_NOT_SAVED: "尚未保存原文条目",
  ITEMS_REQUIRED: "需要先保存原文条目", MAP_CONFIRMATION_REQUIRED: "等待确认这一版结构",
  SCOPED_DERIVATION_REMAINING: "明确授权的派生范围尚未处理完", UNPROCESSED_SOURCE_SCOPE: "仍有原文范围尚未处理",
  NO_INTERNAL_EVIDENCE: "未提供内部依据", INTERNAL_EVIDENCE_NOT_WIRED: "当前版本尚未接入内部依据",
  INTERNAL_EVIDENCE_ANALYSIS_REQUIRED: "内部依据分析尚未保存", INTERNAL_EVIDENCE_ANALYSIS_PARTIAL: "部分分析已保存，仍有必需单元未完成",
  WORKFLOW_EVIDENCE_CONFLICT: "工作流声明与依据状态不一致",
  SELF_CHECK_NOT_SAVED: "尚无已保存自查", SELF_CHECK_NOT_RUN: "尚未运行程序自查",
  SELF_CHECK_BASIS_CHANGED: "自查依据已变化，需要重新核对", SELF_CHECK_FAILED: "程序自查未通过",
  DELIVERABLE_NOT_SAVED: "尚未保存交付成果", SELF_CHECK_REQUIRED: "需要先完成自查",
  INCOMPLETE_OPERATION: "存在结果未知或待恢复的程序操作", OPERATION_FAILURE: "保留失败尝试记录",
  EVIDENCE_ANALYSIS_SKIPPED: "依据分析按声明跳过",
  EVIDENCE_BINDING_UNAVAILABLE: "冻结的内部依据当前不可用", EVIDENCE_BINDING_CHANGED: "冻结的内部依据版本已变化",
};
const evidenceLabels: Record<string, string> = {
  TASK_CONTEXT: "任务与来源", TASK_BINDING: "任务绑定", DOCUMENT_MAP: "文档结构", SOURCE_ITEMS: "原文条目",
  DECOMPOSITION: "明确派生", REVIEW_HEAD: "自查工作版", DELIVERY: "已保存交付",
  WORKFLOW_BRANCH: "声明分支", WORKFLOW_CHECK: "程序自查", DELIVERY_VERSION: "已保存交付版本",
};
const optionalInputKeys: Record<string, { label: string; reason: string }> = {
  project_references: { label: "workflow.optional.project_references.label", reason: "workflow.optional.project_references.reason" },
  team_knowledge: { label: "workflow.optional.team_knowledge.label", reason: "workflow.optional.team_knowledge.reason" },
};
const expectedOutputKeys: Record<string, string> = {
  "可回查原文与顺序的文档结构": "workflow.expected.structure",
  "保留完整父项的原文条目、分类及明确授权的必要派生": "workflow.expected.items",
  "有内部依据时的引用分析；无内部依据时明确标记未执行": "workflow.expected.evidence",
  "程序自查结果与当前可核对成果版本": "workflow.expected.check",
};

export function FixedWorkflowSummary({ workflow }: { workflow?: ApplicationWorkflowDescriptor | null }) {
  const l = useUiCopy();
  if (!workflow) return <p className="agent-inline-notice" role="status">{l("workflow.noDescriptor", "当前宿主尚未提供固定流程描述；不会用聊天目标替代阶段检查。")}</p>;
  return <section className="fixed-workflow-intake" aria-label={l("workflow.intakeAria", "固定业务流程")}>
    <header><strong>{l("workflow.defaultLabel", workflow.label || "客户需求整理固定流程")}</strong><span>{l("workflow.version", "版本 {version}", { version: workflow.version })}</span></header>
    <dl>
      <div><dt>{l("workflow.required", "必需资料")}</dt><dd>{workflow.required_inputs?.map(item => item.id === "customer_source" ? l("workflow.customerSource", item.label) : item.label).join("、") || l("workflow.customerSource", "客户需求文档")}</dd></div>
      <div><dt>{l("workflow.optional", "可选内部依据")}</dt><dd>{workflow.optional_inputs?.length
        ? workflow.optional_inputs.map(item => {
          const keys = optionalInputKeys[item.id];
          const label = keys ? l(keys.label, item.label) : item.label;
          const reason = item.available ? l("workflow.available", "可选择") : item.reason
            ? keys ? l(keys.reason, item.reason) : item.reason
            : l("workflow.unavailable", "当前不可用");
          return `${label}：${reason}`;
        }).join("；")
        : l("workflow.noOptional", "当前版本未接入项目参考或团队知识库。")}</dd></div>
      <div><dt>{l("workflow.expected", "预期成果")}</dt><dd>{workflow.expected_outputs?.map(text => {
        const key = expectedOutputKeys[text];
        return key ? l(key, text) : text;
      }).join("；") || l("workflow.expectedFallback", "结构、原文条目、分类、自查与可核对成果。")}</dd></div>
    </dl>
    <ol className="fixed-workflow-stage-list">{workflow.stages.map(stage => <li key={stage.id}>{l(`workflow.stageLabel.${stage.id}`, stage.label)}</li>)}</ol>
  </section>;
}

export function FixedWorkflowStatus({ state, unconfirmed, compact = false }: { state?: ApplicationWorkflowState | null;
  unconfirmed?: { reason: string; message?: string } | null; compact?: boolean }) {
  const l = useUiCopy();
  const [expanded, setExpanded] = useState(false);
  const toggle = useRef<HTMLButtonElement>(null);
  const detailsId = useId();
  if (!state) return <p className="agent-inline-notice" role="status">{unconfirmed
    ? l("workflow.unconfirmed", "固定流程状态未确认：{reason}。已保存成果仍保留，请重新读取任务后再核对。", { reason: unconfirmed.message || unconfirmed.reason })
    : l("workflow.noState", "固定流程状态尚未保存；任务尚未开始或成果尚未同步。")}</p>;
  if (state.verification === "UNCONFIRMED") return <section className="fixed-workflow-status" aria-label={l("workflow.statusAria", "固定流程成果")}>
    <header><strong>{l("workflow.statusHeading", "业务成果阶段")}</strong><span>{l("workflow.statusUnconfirmed", "状态未确认")}</span></header>
    <p role="status">{l("workflow.unconfirmed", "固定流程状态未确认：{reason}。已保存成果仍保留，请重新读取任务后再核对。", { reason: (state as ApplicationWorkflowState & { unconfirmed?: { reason?: string } }).unconfirmed?.reason || l("workflow.unconfirmedReason", "派生状态与保存工件不一致") })}</p>
  </section>;
  const active = state.stages.find(stage => stage.id === state.next_stage)
    || state.stages.find(stage => stage.status === "IN_PROGRESS")
    || state.stages.find(stage => ["WAITING", "BLOCKED"].includes(stage.status))
    || state.stages.find(stage => stage.status === "PENDING")
    || [...state.stages].reverse().find(stage => stage.status === "SAVED");
  const activeLabel = active ? l(`workflow.stageLabel.${active.id}`, active.label) : "";
  const activeStatus = active ? l(`workflow.stage.${active.status}`, statusLabels[active.status] || active.status) : "";
  const activeReason = active?.reason ? l(`workflow.reason.${active.reason}`, reasonLabels[active.reason] || active.reason) : "";
  const visibleReasons = [...new Set([activeReason, ...(!state.ready_to_review
    ? (state.open_items || []).filter(reason => reason !== "EVIDENCE_ANALYSIS_SKIPPED")
      .map(reason => l(`workflow.reason.${reason}`, reasonLabels[reason] || reason)) : [])].filter(Boolean))].join("; ");
  const details = <>
    <ol>{state.stages.map(stage => <li key={stage.id} data-status={stage.status}>
      <span>{l(`workflow.stageLabel.${stage.id}`, stage.label)}</span><strong>{l(`workflow.stage.${stage.status}`, statusLabels[stage.status] || stage.status)}</strong>
      {stage.reason && <small>{l(`workflow.reason.${stage.reason}`, reasonLabels[stage.reason] || stage.reason)}</small>}
      {stage.evidence?.length ? <small>{stage.evidence.map(item => l(`workflow.evidence.${item.kind}`, evidenceLabels[item.kind] || item.kind)).join("、")}</small> : null}
    </li>)}</ol>
    {state.knowledge_evidence && <p className={state.knowledge_evidence.status === "CURRENT" ? "" : "agent-inline-notice"} role="status">
      {l("workflow.knowledgeEvidence", "内部依据：{mode} · {status}。已保存分析建议仍按当时引用保留。", {
        mode: state.knowledge_evidence.mode || "NONE",
        status: state.knowledge_evidence.status === "CURRENT"
          ? l("workflow.knowledgeCurrent", "任务冻结版本可读取")
          : state.knowledge_evidence.message || l("workflow.knowledgeUnconfirmed", "任务冻结版本未确认"),
      })}
    </p>}
    <p role="status">{state.ready_to_review ? l("workflow.ready", "已保存成果满足当前可核对条件；人工接受仍未声称。") : `${l("workflow.next", "下一步：")}${state.next_stage ? l(`workflow.stageLabel.${state.next_stage}`, state.stages.find(stage => stage.id === state.next_stage)?.label || state.next_stage) : l("workflow.continueRemaining", "继续处理未决项")}`}</p>
    {state.open_items?.length ? <p className="agent-inline-notice">{l("workflow.openItems", "未决项：")}{state.open_items.map(item => l(`workflow.reason.${item}`, reasonLabels[item] || item)).join("；")}</p> : null}
  </>;
  if (compact) return <section className="fixed-workflow-status is-compact" aria-label={l("workflow.statusAria", "固定流程成果")}>
    <div className="fixed-workflow-compact-row">
      <strong>{l("workflow.statusHeading", "业务成果阶段")}</strong>
      {active && <span className="fixed-workflow-compact-stage">{l("workflow.currentStage", "当前：{stage}", { stage: activeLabel })}</span>}
      {active && <span className="fixed-workflow-compact-result" data-status={active.status}>{activeStatus}</span>}
      {visibleReasons && <small className="fixed-workflow-compact-reason" title={visibleReasons}>{visibleReasons}</small>}
      {!active && <span className="fixed-workflow-compact-result">{activeStatus || l("workflow.statusUnconfirmed", "状态未确认")}</span>}
      <button ref={toggle} type="button" className="button ghost compact fixed-workflow-compact-toggle" aria-expanded={expanded} aria-controls={detailsId} onClick={() => setExpanded(value => !value)}>
        {expanded ? l("workflow.hideDetails", "收起详情") : l("workflow.viewDetails", "查看详情")}
      </button>
    </div>
    <div id={detailsId} className="fixed-workflow-compact-details" hidden={!expanded}>{details}</div>
  </section>;
  return <section className="fixed-workflow-status" aria-label={l("workflow.statusAria", "固定流程成果")}>
    <header><strong>{l("workflow.statusHeading", "业务成果阶段")}</strong><span>{l("workflow.workflowVersion", "流程 v{version}", { version: state.workflow.version })}</span></header>
    {details}
  </section>;
}
