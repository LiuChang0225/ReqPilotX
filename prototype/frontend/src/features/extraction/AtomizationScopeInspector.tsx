import { useEffect, useId, useRef } from "react";
import { Dialog } from "../../shared/ui/Dialog";
import { Icon } from "../../shared/ui/Icon";
import type {
  RequirementAtomizationScopeAttempt,
  RequirementAtomizationScopeReceipt,
} from "../../shared/api/contracts";
import type {
  AtomizationScope,
  AtomizationScopePlan,
  AtomizationScopeViewItem,
  AtomizationScopeViewModel,
} from "./atomizationScopeModel";
import { ATOMIZATION_SCOPE_OPTIONS } from "./atomizationScopeModel";

export interface AtomizationScopeInspectorProps {
  viewModel: AtomizationScopeViewModel;
  dialogOpen: boolean;
  busy: boolean;
  rangeLoading: boolean;
  canOpenDialog: boolean;
  actionGuidance: string;
  serverStateLabel: string;
  canUndo: boolean;
  error: string;
  currentReceipt: RequirementAtomizationScopeReceipt | null;
  lastAttempt: RequirementAtomizationScopeAttempt | null;
  onOpenDialog: () => void;
  onCloseDialog: () => void;
  onScopeChange: (scope: AtomizationScope) => void;
  onApply: (plan: AtomizationScopePlan) => void;
  onRetry: () => void;
  onUndo: () => void;
}

export function AtomizationScopeInspector(
  {
    viewModel,
    dialogOpen,
    busy,
    rangeLoading,
    canOpenDialog,
    actionGuidance,
    serverStateLabel,
    canUndo,
    error,
    currentReceipt,
    lastAttempt,
    onOpenDialog,
    onCloseDialog,
    onScopeChange,
    onApply,
    onRetry,
    onUndo,
  }: AtomizationScopeInspectorProps,
) {
  const scopeDescriptionIdPrefix = useId();
  const errorRef = useRef<HTMLDivElement>(null);
  const closeWhenIdle = () => {
    if (!busy) onCloseDialog();
  };

  useEffect(() => {
    if (!error) return;
    const frame = requestAnimationFrame(() => errorRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [dialogOpen, error]);

  const errorNotice = error ? (
    <div
      ref={errorRef}
      className="atomization-scope-error"
      role="alert"
      tabIndex={-1}
    >
      <div>
        <strong>原子化操作未完成</strong>
        <span>{error}</span>
      </div>
      <button
        className="text-button"
        type="button"
        disabled={busy || rangeLoading}
        onClick={onRetry}
      >
        重新读取范围
      </button>
    </div>
  ) : null;
  const actionNotice = actionGuidance ? (
    <p className="atomization-scope-guidance" role="status">
      {actionGuidance}
    </p>
  ) : null;

  return (
    <>
      <section
        className="entry-classification-inspector atomization-scope-inspector"
        aria-label="需求原子化"
      >
        <header>
          <div>
            <span>可选 · 需求原子化</span>
            <strong>{viewModel.scopeLabel}</strong>
          </div>
          <small aria-live="polite">{serverStateLabel}</small>
        </header>
        <p>
          这是需求分析前的独立、人工触发步骤。拆分可能产生带语义归纳的派生条目；完整父条目和来源证据始终保留。{viewModel.summary}
        </p>
        <AtomizationExecutionReceipt
          currentReceipt={currentReceipt}
          lastAttempt={lastAttempt}
        />
        {!dialogOpen ? errorNotice : null}
        {!dialogOpen ? actionNotice : null}
        <footer className="atomization-scope-actions">
          <span>
            {viewModel.defaultBehavior}；未显式选择时不原子化，非技术、信息、结构或非直接需求保持原条目
          </span>
          <div>
            <button
              className="button ghost"
              type="button"
              aria-label="撤销最近一次原子化"
              disabled={busy || rangeLoading || !canUndo}
              onClick={onUndo}
            >
              撤销最近一次
            </button>
            <button
              className="button secondary"
              type="button"
              disabled={busy || rangeLoading || !canOpenDialog}
              onClick={() => {
                if (!busy && !rangeLoading && canOpenDialog) onOpenDialog();
              }}
            >
              选择原子化范围
            </button>
          </div>
        </footer>
      </section>

      <Dialog
        open={dialogOpen}
        title="选择原子化范围"
        onClose={closeWhenIdle}
        className="confirm-action-dialog atomization-scope-dialog"
      >
        <header className="modal-header">
          <div>
            <p className="section-label">需求基线之后 · 需求分析之前</p>
            <h2>选择原子化范围</h2>
            <p>
              默认不拆分。原子化可能把一条完整原文转换为便于分析的派生条目，因此必须由用户显式发起；系统保留完整父条目、来源证据以及拆分前后关系。
            </p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭原子化范围"
            disabled={busy}
            onClick={closeWhenIdle}
          >
            <Icon name="close" />
          </button>
        </header>

        {dialogOpen ? errorNotice : null}
        {dialogOpen ? actionNotice : null}

        <div className="entry-classification-batch-body atomization-scope-dialog-body">
          <fieldset>
            <legend>执行范围</legend>
            <div className="entry-classification-options atomization-scope-options">
              {ATOMIZATION_SCOPE_OPTIONS.map((option) => {
                const descriptionId = `${scopeDescriptionIdPrefix}-${option.value.toLowerCase()}`;
                return (
                  <button
                    key={option.value}
                    type="button"
                    data-scope={option.value}
                    aria-label={option.label}
                    aria-describedby={descriptionId}
                    aria-pressed={viewModel.scope === option.value}
                    disabled={busy || rangeLoading || !canOpenDialog}
                    onClick={() => {
                      if (!busy && !rangeLoading && canOpenDialog) {
                        onScopeChange(option.value);
                      }
                    }}
                  >
                    <strong>{option.label}</strong>
                    <small id={descriptionId}>{option.description}</small>
                  </button>
                );
              })}
            </div>
          </fieldset>

          <p>
            “所选项”来自独立的原子化复选框，不跟随树当前高亮或筛选自动变化；来源文件或当前结构版本改变时会自动清空。
          </p>

          <div className="atomization-scope-summary" aria-live="polite">
            <strong>{viewModel.summary}</strong>
            <small>{viewModel.defaultBehavior}</small>
          </div>

          {viewModel.emptyReason ? (
            <p className="classification-dialog-error" role="status">
              {viewModel.emptyReason}
            </p>
          ) : null}

          <ScopeItemList
            title={`可执行原子化 · ${viewModel.targetCount}`}
            emptyLabel="当前范围没有可执行的技术需求目标。"
            items={viewModel.targets}
          />
          <ScopeItemList
            title={`保持原条目 · ${viewModel.skippedCount}`}
            emptyLabel="当前范围没有跳过项。"
            items={viewModel.skipped}
            showReason
          />
        </div>

        <footer className="confirm-action-footer">
          <button
            className="button ghost"
            type="button"
            disabled={busy}
            onClick={closeWhenIdle}
          >
            取消
          </button>
          <button
            className="button primary"
            type="button"
            disabled={busy || rangeLoading || !viewModel.canApply}
            onClick={() => {
              if (viewModel.canApply && !busy && !rangeLoading) {
                onApply(viewModel.plan);
              }
            }}
          >
            {viewModel.actionLabel}
          </button>
        </footer>
      </Dialog>
    </>
  );
}

function AtomizationExecutionReceipt({
  currentReceipt,
  lastAttempt,
}: {
  currentReceipt: RequirementAtomizationScopeReceipt | null;
  lastAttempt: RequirementAtomizationScopeAttempt | null;
}) {
  const attemptIsLatest = Boolean(
    lastAttempt && lastAttempt.status !== "SUCCEEDED",
  );
  const attempt = attemptIsLatest || !currentReceipt ? lastAttempt : null;
  const receipt = attempt ? null : currentReceipt;
  if (!attempt && !receipt) return null;

  const requested = attempt
    ? attempt.requested_review_node_ids.length
    : receipt?.requested_review_node_ids.length || 0;
  const executed = attempt
    ? attempt.executed_review_node_ids.length
    : receipt?.executed_review_node_ids.length || 0;
  const skipped = attempt
    ? attempt.skipped_targets.length
    : receipt?.skipped_targets.length || 0;
  const completed = attempt
    ? attempt.completed_review_node_ids.length
    : receipt?.succeeded_review_node_ids.length || 0;
  const failed = attempt
    ? attempt.failed_review_node_ids.length
    : receipt?.failed_review_node_ids.length || 0;
  const providerCalls = attempt
    ? attempt.provider_call_count
    : receipt?.provider_call_count || 0;
  const checkpointCreated = attempt
    ? attempt.checkpoint_created_review_node_ids.length
    : receipt?.checkpoint_created_count || 0;
  const checkpointReused = attempt
    ? attempt.checkpoint_reused_review_node_ids.length
    : receipt?.checkpoint_reuse_count || 0;
  const checkpointInvalidated = attempt
    ? attempt.checkpoint_invalidated_review_node_ids.length
    : 0;
  const title = attempt
    ? attempt.status === "INTERRUPTED"
      ? "最近一次中断"
      : attempt.status === "RUNNING"
        ? "正在执行"
        : "最近一次完成"
    : receipt?.operation === "UNDO"
      ? "最近撤销已完成"
      : "最近原子化已完成";
  const metrics = [
    ["范围", requested, "项"],
    ["实际处理", executed, "项"],
    ["保留", skipped, "项"],
    ["已完成", completed, "项"],
    ["失败", failed, "项"],
    ["AI 处理", providerCalls, "次"],
    ["保存进度", checkpointCreated, "项"],
    ["沿用进度", checkpointReused, "项"],
    ["失效进度", checkpointInvalidated, "项"],
  ] as const;

  return (
    <section
      className="atomization-execution-receipt"
      aria-label="原子化执行记录"
      aria-live="polite"
    >
      <header>
        <strong>{title}</strong>
        {attempt?.error_code ? (
          <span>{atomizationAttemptErrorLabel(attempt.error_code)}</span>
        ) : null}
      </header>
      <div className="atomization-execution-metrics">
        {metrics.map(([label, value, unit]) => (
          <span
            key={label}
            aria-label={`${label} ${value} ${unit}`}
          >
            {label} <strong>{value}</strong>
          </span>
        ))}
      </div>
      {attempt?.status === "INTERRUPTED" ? (
        <footer>
          <span>{attempt.retryable ? "可重试" : "不可重试"}</span>
          <span>
            {attempt.checkpoint_resumable
              ? "将从上次进度继续"
              : "需重新处理当前范围"}
          </span>
        </footer>
      ) : null}
    </section>
  );
}

function atomizationAttemptErrorLabel(errorCode: string): string {
  const labels: Record<string, string> = {
    PROVIDER_INTERRUPTED: "AI 拆分服务暂时中断",
  };
  return labels[errorCode] || "执行过程中出现异常";
}

const ITEM_DISPLAY_LIMIT = 20;

function ScopeItemList({
  title,
  emptyLabel,
  items,
  showReason = false,
}: {
  title: string;
  emptyLabel: string;
  items: readonly AtomizationScopeViewItem[];
  showReason?: boolean;
}) {
  const visibleItems = items.slice(0, ITEM_DISPLAY_LIMIT);
  const remaining = items.length - visibleItems.length;
  return (
    <section className="atomization-scope-item-list">
      <h3>{title}</h3>
      {visibleItems.length ? (
        <ul>
          {visibleItems.map((item) => (
            <li key={item.treeNodeId}>
              <strong>{item.label}</strong>
              <small>{item.pathLabel}</small>
              {showReason && item.reasonLabel ? (
                <small>{item.reasonLabel}</small>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p>{emptyLabel}</p>
      )}
      {remaining > 0 ? <small>另有 {remaining} 项未展开显示</small> : null}
    </section>
  );
}
