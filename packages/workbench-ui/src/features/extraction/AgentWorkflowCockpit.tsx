import { useId, useState } from "react";
import { cx } from "../../shared/lib/format";
import { Icon } from "../../shared/ui/Icon";

export type AgentWorkflowStepState =
  | "complete"
  | "current"
  | "pending"
  | "blocked";

export interface AgentWorkflowStep {
  id: string;
  label: string;
  state: AgentWorkflowStepState;
}

export interface AgentWorkflowControl {
  id: string;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  tone?: "primary" | "secondary" | "danger";
}

interface AgentWorkflowCockpitProps {
  goal: string;
  statusLabel: string;
  statusTone: "working" | "ready" | "attention" | "blocked";
  steps: readonly AgentWorkflowStep[];
  currentAction: string;
  findings: readonly string[];
  controls: readonly AgentWorkflowControl[];
  defaultExpanded?: boolean;
}

const STEP_STATE_LABELS: Record<AgentWorkflowStepState, string> = {
  complete: "已完成",
  current: "进行中",
  pending: "待开始",
  blocked: "受阻",
};

export function AgentWorkflowCockpit({
  goal,
  statusLabel,
  statusTone,
  steps,
  currentAction,
  findings,
  controls,
  defaultExpanded = true,
}: AgentWorkflowCockpitProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const detailsId = useId();

  return (
    <section
      className={cx("agent-workflow-cockpit", `is-${statusTone}`)}
      aria-label="Agent 工作流驾驶舱"
      aria-live="polite"
    >
      <header className="agent-workflow-cockpit-summary">
        <span className="agent-workflow-status-mark" aria-hidden="true">
          <Icon name="spark" size={14} />
        </span>
        <div className="agent-workflow-summary-copy">
          <div>
            <strong>Agent 工作流</strong>
            <span>{statusLabel}</span>
          </div>
          <p title={currentAction}>{currentAction}</p>
        </div>
        <button
          type="button"
          className="agent-workflow-cockpit-toggle"
          aria-expanded={expanded}
          aria-controls={detailsId}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起" : "展开"}
          <Icon name={expanded ? "chevron-down" : "chevron"} size={15} />
        </button>
      </header>

      <ol className="agent-workflow-compact-plan" aria-label="Agent 执行阶段">
        {steps.map((step) => (
          <li key={step.id} className={`is-${step.state}`}>
            <span aria-hidden="true">
              {step.state === "complete"
                ? "✓"
                : step.state === "blocked"
                  ? "!"
                  : ""}
            </span>
            <strong>{step.label}</strong>
          </li>
        ))}
      </ol>

      <div
        id={detailsId}
        className="agent-workflow-cockpit-details"
        hidden={!expanded}
      >
        <section className="agent-workflow-goal">
          <h3>目标</h3>
          <p>{goal}</p>
        </section>

        <section className="agent-workflow-plan">
          <h3>计划</h3>
          <ol>
            {steps.map((step) => (
              <li key={step.id} className={`is-${step.state}`}>
                <span className="agent-workflow-step-mark" aria-hidden="true">
                  {step.state === "complete"
                    ? "✓"
                    : step.state === "current"
                      ? "●"
                      : step.state === "blocked"
                        ? "!"
                        : "○"}
                </span>
                <span>{step.label}</span>
                <small>{STEP_STATE_LABELS[step.state]}</small>
              </li>
            ))}
          </ol>
        </section>

        <section className="agent-workflow-current-action">
          <h3>当前行动</h3>
          <p>{currentAction}</p>
        </section>

        <section className="agent-workflow-findings">
          <h3>发现</h3>
          <ul>
            {findings.map((finding) => (
              <li key={finding}>{finding}</li>
            ))}
          </ul>
        </section>

        <footer className="agent-workflow-controls">
          <div>
            <h3>控制</h3>
            <small>只显示当前真正可执行的操作</small>
          </div>
          {controls.length > 0 ? (
            <div className="agent-workflow-control-actions">
              {controls.map((control) => (
                <button
                  key={control.id}
                  type="button"
                  className={cx(
                    "button compact",
                    control.tone === "primary"
                      ? "primary"
                      : control.tone === "danger"
                        ? "secondary agent-workflow-danger-control"
                        : "ghost",
                  )}
                  disabled={control.disabled}
                  title={control.title}
                  onClick={control.onClick}
                >
                  {control.label}
                </button>
              ))}
            </div>
          ) : (
            <span>当前没有可安全执行的集中控制。</span>
          )}
        </footer>
        <p className="agent-workflow-disclosure">
          此处展示可审核状态、行动与证据发现，不展示模型内部推理过程。
        </p>
      </div>
    </section>
  );
}
