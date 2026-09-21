import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, api } from "../../shared/api/client";
import type {
  AgentStructureIntent,
  AgentStructureOperation,
  AgentStructureProposalPublicV1,
  AgentStructureProposalStatusV1,
} from "../../shared/api/contracts";
import { cx } from "../../shared/lib/format";
import { Dialog } from "../../shared/ui/Dialog";
import { Icon } from "../../shared/ui/Icon";

const ACTIVE_JOB_STATES = new Set(["PENDING", "RUNNING", "CANCEL_REQUESTED"]);

interface AgentStructureProposalDockProps {
  projectId: string;
  documentId: string;
  sourceSha256: string;
  revisionNumber: number;
  effectiveSnapshotId: string;
  selectedStructureNodeId?: string;
  disabled?: boolean;
  onFocusNode: (nodeId: string) => void;
  onApplied: () => Promise<void>;
}

function commandId(prefix: string): string {
  const suffix =
    typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}:${suffix}`;
}

function operationTitle(operation: AgentStructureOperation): string {
  const kind = String(operation.operation || "").toLowerCase();
  if (kind === "re_parent") return "调整父级";
  if (kind === "re_order") return "调整同级顺序";
  if (kind === "merge_continuation") return "合并续接内容";
  if (kind === "split_boundary") return "拆分来源边界";
  if (kind === "add_relation") return "新增结构关系";
  if (kind === "remove_relation") return "移除结构关系";
  return "结构修正";
}

function operationDescription(operation: AgentStructureOperation): string {
  const parent = String(operation.new_parent_structure_node_id || "");
  const order = Number(operation.new_sibling_index);
  if (parent) {
    return `移至目标父级${Number.isFinite(order) ? `，位于第 ${order + 1} 项` : ""}`;
  }
  if (operation.relation_id) return "变更一条已验证的结构关系";
  return "按来源绑定的结构差异更新当前树";
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return "请求未完成，请重新读取当前状态后重试。";
}

export function AgentStructureProposalDock({
  projectId,
  documentId,
  sourceSha256,
  revisionNumber,
  effectiveSnapshotId,
  selectedStructureNodeId = "",
  disabled = false,
  onFocusNode,
  onApplied,
}: AgentStructureProposalDockProps) {
  const [state, setState] = useState<AgentStructureProposalStatusV1 | null>(null);
  const [loading, setLoading] = useState(true);
  const [mutation, setMutation] = useState("");
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [applyOpen, setApplyOpen] = useState(false);
  const [revisionIntent, setRevisionIntent] =
    useState<AgentStructureIntent>("REVIEW_PARENTAGE");

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const next = await api.agentStructureProposalStatus(
          projectId,
          documentId,
          signal,
        );
        setState(next);
        setError("");
      } catch (caught) {
        if (signal?.aborted) return;
        setError(errorMessage(caught));
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [documentId, projectId],
  );

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const jobStatus = String(state?.job?.status || "").toUpperCase();
  const jobActive = ACTIVE_JOB_STATES.has(jobStatus);

  useEffect(() => {
    if (!jobActive) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => void load(controller.signal), 1200);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [jobActive, load]);

  const proposal = state?.proposal || null;
  const activeJob = state?.job || null;
  const latestSteer = state?.steer || null;
  const proposalReady = proposal?.status === "READY";
  const currentBase =
    proposal?.base.expected_revision_number === revisionNumber &&
    proposal?.base.expected_effective_snapshot_id === effectiveSnapshotId &&
    proposal.source_sha256 === sourceSha256;
  const actionDisabled =
    disabled ||
    Boolean(mutation) ||
    jobActive ||
    !sourceSha256 ||
    !effectiveSnapshotId;
  const result = state?.job?.result || null;
  const noProposal =
    state?.job?.status === "SUCCEEDED" && result?.status === "NO_PROPOSAL";

  const statusLabel = useMemo(() => {
    if (loading) return "读取中";
    if (jobStatus === "PENDING") return "等待后台检查";
    if (jobStatus === "RUNNING") return "正在检查结构";
    if (jobStatus === "CANCEL_REQUESTED") return "正在安全停止";
    if (jobStatus === "FAILED") return "检查未完成";
    if (jobStatus === "CANCELLED") return "检查已取消";
    if (proposalReady && currentBase) return "建议待确认";
    if (proposal?.status === "APPLIED") return "建议已应用";
    if (proposal?.status === "REJECTED") return "建议已拒绝";
    if (proposal && !currentBase) return "建议已过期";
    if (noProposal) return "未发现安全修正";
    return "尚未检查";
  }, [currentBase, jobStatus, loading, noProposal, proposal, proposalReady]);

  const runMutation = async (label: string, action: () => Promise<unknown>) => {
    setMutation(label);
    setError("");
    try {
      await action();
      await load();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setMutation("");
    }
  };

  const start = () =>
    runMutation("start", () =>
      api.startAgentStructureProposal(projectId, documentId, {
        schema_version: "agent-structure-proposal-start-request/v1",
        command_id: commandId("structure-proposal-start"),
        expected_source_sha256: sourceSha256,
        expected_revision_number: revisionNumber,
        expected_effective_snapshot_id: effectiveSnapshotId,
        intent: selectedStructureNodeId ? "REVIEW_PARENTAGE" : "GENERAL",
        focus_structure_node_ids: selectedStructureNodeId
          ? [selectedStructureNodeId]
          : [],
        suggested_operations: [],
        suggested_operations_authority: "NONE",
      }),
    );

  const revise = () => {
    if (!proposal) return Promise.resolve();
    return runMutation("revise", () =>
      api.reviseAgentStructureProposal(projectId, documentId, {
        schema_version: "agent-structure-proposal-revise-request/v1",
        proposal_record_id: proposal.proposal_record_id,
        expected_proposal_payload_sha256: proposal.proposal_payload_sha256,
        command_id: commandId("structure-proposal-revise"),
        expected_source_sha256: sourceSha256,
        expected_revision_number: revisionNumber,
        expected_effective_snapshot_id: effectiveSnapshotId,
        intent: revisionIntent,
        focus_structure_node_ids: selectedStructureNodeId
          ? [selectedStructureNodeId]
          : [],
        suggested_operations: [],
        suggested_operations_authority: "NONE",
      }),
    );
  };

  const steer = () => {
    if (!activeJob || !selectedStructureNodeId) return Promise.resolve();
    return runMutation("steer", () =>
      api.steerAgentStructureProposal(projectId, documentId, {
        schema_version: "agent-structure-proposal-steer-request/v1",
        job_id: activeJob.job_id,
        command_id: commandId("structure-proposal-steer"),
        expected_job_generation: activeJob.generation,
        expected_source_sha256: sourceSha256,
        expected_revision_number: revisionNumber,
        expected_effective_snapshot_id: effectiveSnapshotId,
        intent: revisionIntent as Exclude<AgentStructureIntent, "GENERAL">,
        focus_structure_node_ids: [selectedStructureNodeId],
        suggested_operations: [],
        suggested_operations_authority: "NONE",
      }),
    );
  };

  const reject = () => {
    if (!proposal) return Promise.resolve();
    return runMutation("reject", () =>
      api.rejectAgentStructureProposal(
        projectId,
        documentId,
        proposal.proposal_record_id,
        proposal.proposal_payload_sha256,
        commandId("structure-proposal-reject"),
      ),
    );
  };

  const apply = async () => {
    if (!proposal) return;
    setApplyOpen(false);
    await runMutation("apply", async () => {
      await api.applyAgentStructureProposal(
        projectId,
        documentId,
        proposal.proposal_record_id,
        proposal.proposal_payload_sha256,
        commandId("structure-proposal-apply"),
      );
      await onApplied();
    });
  };

  return (
    <section
      className={cx(
        "agent-structure-dock",
        proposalReady && currentBase && "has-ready-proposal",
      )}
      aria-label="AI 结构建议"
    >
      <div className="agent-structure-dock-summary">
        <span className="notice-pip" aria-hidden="true" />
        <div>
          <strong>AI 结构建议</strong>
          <span>{statusLabel}</span>
        </div>
        {proposalReady && currentBase ? (
          <span className="agent-structure-count">
            {proposal.operation_count} 项差异
          </span>
        ) : null}
        <button
          type="button"
          className="extraction-progress-drawer-toggle"
          aria-expanded={expanded}
          aria-controls="agent-structure-proposal-details"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起" : "详情"}
          <Icon name={expanded ? "chevron-down" : "chevron"} size={14} />
        </button>
      </div>
      <div
        id="agent-structure-proposal-details"
        className="agent-structure-dock-details"
        hidden={!expanded}
      >
        {error ? (
          <div className="agent-structure-error" role="alert">
            <strong>操作未完成</strong>
            <span>{error}</span>
            <button className="text-button" type="button" onClick={() => void load()}>
              重新读取
            </button>
          </div>
        ) : null}
        {jobActive ? (
          <div className="agent-structure-active-turn" role="status" aria-live="polite">
            <p>
              {String(state?.job?.progress?.stage || "后台任务已提交")}。完成前不会修改当前结构。
            </p>
            {jobStatus === "RUNNING" ? (
              <div className="agent-structure-revise-row">
                <label>
                  <span>调整当前检查方向</span>
                  <select
                    value={revisionIntent}
                    disabled={Boolean(mutation)}
                    onChange={(event) =>
                      setRevisionIntent(event.target.value as AgentStructureIntent)
                    }
                  >
                    <option value="REVIEW_PARENTAGE">父子关系</option>
                    <option value="REVIEW_ORDER">同级顺序</option>
                    <option value="REVIEW_BOUNDARY">内容边界</option>
                    <option value="REVIEW_RELATIONS">结构关系</option>
                  </select>
                </label>
                <button
                  className="button ghost compact"
                  type="button"
                  disabled={
                    Boolean(mutation) ||
                    !selectedStructureNodeId ||
                    latestSteer?.status === "PENDING" ||
                    latestSteer?.status === "DELIVERING"
                  }
                  title={
                    selectedStructureNodeId
                      ? "发送到当前正在运行的 exact turn"
                      : "请先选择一个可编辑的结构节点"
                  }
                  onClick={() => void steer()}
                >
                  调整当前检查
                </button>
              </div>
            ) : null}
            {latestSteer ? (
              <small>
                最近调整：
                {latestSteer.status === "DELIVERED"
                  ? "已送达当前 turn"
                  : latestSteer.status === "REJECTED"
                    ? "未送达，请在本轮结束后生成新版本"
                    : "等待当前 turn 接收"}
              </small>
            ) : null}
          </div>
        ) : null}
        {jobStatus === "FAILED" ? (
          <p>后台检查未形成建议（{state?.job?.error_code || "未知原因"}）。当前结构未修改。</p>
        ) : null}
        {noProposal ? (
          <p>
            本轮没有发现可由来源与当前结构共同证明的安全修正。当前结构保持不变。
          </p>
        ) : null}
        {proposalReady && currentBase ? (
          <>
            <ol className="agent-structure-diff-list">
              {proposal.operations.map((operation, index) => {
                const nodeId = String(operation.structure_node_id || "");
                return (
                  <li key={`${operation.operation}:${nodeId}:${index}`}>
                    <div>
                      <strong>{operationTitle(operation)}</strong>
                      <span>{operationDescription(operation)}</span>
                    </div>
                    {nodeId ? (
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => onFocusNode(nodeId)}
                      >
                        定位来源
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ol>
            <div className="agent-structure-revise-row">
              <label>
                <span>重新检查方向</span>
                <select
                  value={revisionIntent}
                  disabled={actionDisabled}
                  onChange={(event) =>
                    setRevisionIntent(event.target.value as AgentStructureIntent)
                  }
                >
                  <option value="REVIEW_PARENTAGE">父子关系</option>
                  <option value="REVIEW_ORDER">同级顺序</option>
                  <option value="REVIEW_BOUNDARY">内容边界</option>
                  <option value="REVIEW_RELATIONS">结构关系</option>
                </select>
              </label>
              <button
                className="button ghost compact"
                type="button"
                disabled={actionDisabled}
                onClick={() => void revise()}
              >
                生成新版本
              </button>
            </div>
            <div className="agent-structure-actions">
              <button
                className="button ghost compact"
                type="button"
                disabled={actionDisabled}
                onClick={() => void reject()}
              >
                拒绝建议
              </button>
              <button
                className="button primary compact"
                type="button"
                disabled={actionDisabled}
                onClick={() => setApplyOpen(true)}
              >
                应用 {proposal.operation_count} 项修正
              </button>
            </div>
          </>
        ) : (
          <div className="agent-structure-empty-action">
            <p>只读取当前 SourceIR 与结构快照；建议经服务端验证后仍需由你明确应用。</p>
            <button
              className="button secondary compact"
              type="button"
              disabled={actionDisabled || loading}
              onClick={() => void start()}
            >
              <Icon name="spark" size={14} />
              {selectedStructureNodeId ? "检查当前节点" : "检查当前结构"}
            </button>
          </div>
        )}
      </div>

      <Dialog
        open={applyOpen}
        title="应用 AI 结构建议"
        onClose={() => setApplyOpen(false)}
        busy={mutation === "apply"}
      >
        <header className="modal-header">
          <div>
            <p className="section-label">结构修订</p>
            <h2>应用 {proposal?.operation_count || 0} 项结构修正？</h2>
            <p>
              系统将在提交时重新校验来源、结构版本和全部操作。AI 建议本身没有写入权限。
            </p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭应用确认"
            onClick={() => setApplyOpen(false)}
          >
            <Icon name="close" />
          </button>
        </header>
        <footer className="modal-actions">
          <button
            className="button ghost"
            type="button"
            onClick={() => setApplyOpen(false)}
          >
            取消
          </button>
          <button className="button primary" type="button" onClick={() => void apply()}>
            校验并应用
          </button>
        </footer>
      </Dialog>
    </section>
  );
}
