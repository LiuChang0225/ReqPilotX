import { useCallback, useEffect, useRef, useState } from "react";
import { api, asRecord, request } from "../../shared/api/client";
import type {
  JsonRecord,
  ProjectProcessingProfile,
  RuntimeWorkerFleetStatusV1,
  RuntimeWorkerStatusV1,
} from "../../shared/api/contracts";
import { InlineNotice, StatusBadge } from "../../shared/ui/Feedback";
import { useWorkspace } from "../workspace/WorkspaceContext";

interface AgentReadiness {
  ready: boolean;
  configured: boolean;
  runtimeId: string;
  providerId: string;
  modelName: string;
  reasonCode: string;
}

function normalizeAgentReadiness(value: unknown): AgentReadiness {
  const record = asRecord(value);
  const runtimeId = String(record.runtime_id || "none");
  const configured = record.configured === true;
  const supported = runtimeId === "codex-app-server" || (
    runtimeId === "provider-tool-loop" && configured &&
    record.readiness_scope === "configuration" && record.authentication_verified === false
  );
  return {
    ready: record.ready === true && supported,
    configured,
    runtimeId,
    providerId: String(record.provider_id || ""),
    modelName: String(record.model_name || ""),
    reasonCode: String(record.reason_code || ""),
  };
}

function agentReadinessMessage(readiness: AgentReadiness | null, loading: boolean): string {
  if (loading) return "正在读取 Agent 运行配置…";
  if (!readiness) return "Agent 状态尚未取得，可刷新状态重试。";
  if (readiness.runtimeId === "provider-tool-loop") {
    return readiness.ready
      ? "供应商与目标模型配置已就绪；尚未核验在线认证、模型权限或服务可用性。此检查不会发送模型请求。"
      : "供应商或目标模型配置不完整，请检查当前服务的 provider 配置后刷新状态。";
  }
  if (readiness.runtimeId !== "codex-app-server") {
    return "当前服务未选择受支持的 Agent 运行时，请检查运行配置。";
  }
  if (readiness.ready) {
    return "ChatGPT 登录与目标模型均已核验；AI 阅读会直接使用 Codex Agent。";
  }
  if (readiness.reasonCode.includes("auth_required")) {
    return "Codex 尚未登录。完成 ChatGPT 登录后刷新状态即可。";
  }
  if (readiness.reasonCode.includes("model_unavailable")) {
    return "当前 Codex 账号不可用该模型，请检查模型权限。";
  }
  return "Codex App Server 当前不可用；原文浏览和确定性捕获不受影响。";
}

function normalizeWorkerFleet(value: unknown): RuntimeWorkerFleetStatusV1 | null {
  const record = asRecord(value);
  const state = String(record.state || "").toUpperCase();
  const revision = Number(record.desired_connection_revision);
  if (
    !["READY", "SYNCING", "UNAVAILABLE"].includes(state) ||
    !Number.isInteger(revision) ||
    revision < 0 ||
    !Array.isArray(record.workers)
  ) {
    return null;
  }
  return {
    ...record,
    schema_version: "runtime-worker-fleet-status/v1",
    state: state as RuntimeWorkerFleetStatusV1["state"],
    desired_connection_revision: revision,
    required_kind: String(record.required_kind || "document.extract"),
    revision_required: record.revision_required !== false,
    registered_worker_count: Math.max(0, Number(record.registered_worker_count || 0)),
    live_worker_count: Math.max(0, Number(record.live_worker_count || 0)),
    eligible_worker_count: Math.max(0, Number(record.eligible_worker_count || 0)),
    idle_worker_count: Math.max(0, Number(record.idle_worker_count || 0)),
    active_worker_count: Math.max(0, Number(record.active_worker_count || 0)),
    draining_worker_count: Math.max(0, Number(record.draining_worker_count || 0)),
    stale_revision_worker_count: Math.max(
      0,
      Number(record.stale_revision_worker_count || 0),
    ),
    workers: (record.workers as unknown[]).map((worker) => {
      const item = asRecord(worker);
      return {
        ...item,
        slot: String(item.slot ?? "?"),
        claim_profile: String(item.claim_profile || "unknown"),
        accepted_kinds: Array.isArray(item.accepted_kinds)
          ? item.accepted_kinds.map((kind) => String(kind))
          : [],
        connection_revision: Math.max(0, Number(item.connection_revision || 0)),
        state: String(item.state || "UNKNOWN").toUpperCase(),
        fresh: item.fresh === true,
        revision_match: item.revision_match === true,
        claim_profile_match: item.claim_profile_match !== false,
        eligible: item.eligible === true,
        last_seen_age_seconds: Math.max(0, Number(item.last_seen_age_seconds || 0)),
      } satisfies RuntimeWorkerStatusV1;
    }),
  };
}

function workerLastSeenLabel(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return `${Math.floor(seconds / 3600)} 小时前`;
}

function workerStateLabel(worker: RuntimeWorkerStatusV1): string {
  if (!worker.fresh) return "心跳过期";
  if (!worker.revision_match) return "配置待同步";
  if (worker.state === "DRAINING") return "排空中";
  if (worker.state === "ACTIVE" || worker.state === "BUSY") return "工作中";
  return worker.eligible ? "可接单" : "当前不可接单";
}

function isAutomaticProfile(profile: ProjectProcessingProfile | null): boolean {
  if (!profile) return false;
  return (
    profile.document.primary_parser === "pymupdf" &&
    profile.document.enhancement_chain.length === 0 &&
    profile.document.complex_vision === null &&
    profile.document.auto_escalation === true &&
    profile.llm_provider === "codex" &&
    profile.knowledge_backend === "none"
  );
}

export function SettingsPanel() {
  const {
    readiness,
    processingProfile,
    selectedProject,
    busyAction,
    runAction,
    refreshProject,
  } = useWorkspace();
  const [agentReadiness, setAgentReadiness] = useState<AgentReadiness | null>(null);
  const [agentLoading, setAgentLoading] = useState(true);
  const [agentError, setAgentError] = useState("");
  const [workerFleet, setWorkerFleet] = useState<RuntimeWorkerFleetStatusV1 | null>(null);
  const [workerLoading, setWorkerLoading] = useState(true);
  const [workerError, setWorkerError] = useState("");
  const workerRequestRef = useRef<Promise<RuntimeWorkerFleetStatusV1 | null> | null>(null);

  const readWorkerFleet = useCallback(() => {
    if (workerRequestRef.current) return workerRequestRef.current;
    const request = api.workerFleet().then(normalizeWorkerFleet).finally(() => {
      if (workerRequestRef.current === request) workerRequestRef.current = null;
    });
    workerRequestRef.current = request;
    return request;
  }, []);

  const refreshAgent = useCallback(async () => {
    setAgentLoading(true);
    setAgentError("");
    try {
      setAgentReadiness(normalizeAgentReadiness(await request<JsonRecord>("/api/runtime/agent-readiness")));
    } catch {
      setAgentReadiness(null);
      setAgentError("暂时无法读取 Agent 状态。请确认本地服务正在运行后重试。");
    } finally {
      setAgentLoading(false);
    }
  }, []);

  const refreshWorkers = useCallback(async () => {
    setWorkerLoading(true);
    setWorkerError("");
    try {
      const normalized = await readWorkerFleet();
      setWorkerFleet(normalized);
      if (!normalized) setWorkerError("后台执行器返回了无法识别的状态。");
    } catch {
      setWorkerFleet(null);
      setWorkerError("暂时无法读取后台执行器状态。");
    } finally {
      setWorkerLoading(false);
    }
  }, [readWorkerFleet]);

  useEffect(() => {
    void Promise.all([refreshAgent(), refreshWorkers()]);
  }, [refreshAgent, refreshWorkers]);

  const providerRuntime = agentReadiness?.runtimeId === "provider-tool-loop";
  const codexRuntime = agentReadiness?.runtimeId === "codex-app-server";
  const runtimeLabel = providerRuntime ? "Provider Agent" : codexRuntime ? "Codex Agent" : "Agent";
  const automaticProfile = isAutomaticProfile(processingProfile);
  const captureOnly = processingProfile?.llm_provider === "none";
  const restoreAutomaticProfile = async () => {
    if (!selectedProject || !processingProfile) return;
    const saved = await runAction(
      "恢复 Agent 自动方案",
      () =>
        api.saveProjectProcessingProfile(selectedProject.id, {
          schema_version: "project-processing-profile-update/v1",
          expected_revision: processingProfile.revision,
          document: {
            primary_parser: "pymupdf",
            enhancement_chain: [],
            complex_vision: null,
            auto_escalation: true,
          },
          llm_provider: "codex",
          knowledge_backend: "none",
          reindex_knowledge: processingProfile.knowledge_backend !== "none",
        } satisfies JsonRecord),
      { refresh: "none" },
    );
    if (saved) await refreshProject();
  };

  const workersReady = Boolean(
    workerFleet?.state === "READY" && workerFleet.eligible_worker_count > 0,
  );

  return (
    <section className="management-page settings-page">
      <header className="page-command-header">
        <div>
          <p className="section-label">运行设置</p>
          <h1>{runtimeLabel} 与自动解析</h1>
          <p>
            {codexRuntime
              ? "登录一次 Codex，AI 阅读、文档理解和结构修订由 Agent Skills 接管。"
              : "文档自主任务使用服务已选择的 Agent 运行时；供应商模式仅检查配置，不核验在线认证。"}
          </p>
        </div>
        <button
          className="button secondary"
          type="button"
          disabled={agentLoading || workerLoading}
          onClick={() => void Promise.all([refreshAgent(), refreshWorkers()])}
        >
          {agentLoading || workerLoading ? "正在核验" : "刷新状态"}
        </button>
      </header>

      <div className="connection-card-grid">
        <article className="connection-card" aria-label="Agent 运行状态">
          <header>
            <div>
              <p className="section-label">Agent Runtime</p>
              <h2>{runtimeLabel}</h2>
            </div>
            <StatusBadge
              tone={agentLoading || (providerRuntime && agentReadiness?.ready) ? "warning" : agentReadiness?.ready ? "success" : "danger"}
            >
              {agentLoading ? "正在核验" : agentReadiness?.ready ? providerRuntime ? "配置就绪" : "已登录" : "需要处理"}
            </StatusBadge>
          </header>
          <p>{agentReadinessMessage(agentReadiness, agentLoading)}</p>
          <dl className="worker-fleet-checks">
            <div><dt>运行时</dt><dd>{agentReadiness?.runtimeId || "尚未取得"}</dd></div>
            {providerRuntime ? <div><dt>供应商</dt><dd>{agentReadiness?.providerId || "尚未配置"}</dd></div> : null}
            <div><dt>模型</dt><dd>{agentReadiness?.modelName || "尚未配置"}</dd></div>
          </dl>
          {agentError ? <InlineNotice tone="danger">{agentError}</InlineNotice> : null}
        </article>

        <article className="connection-card" aria-label="Agent Skills 解析链">
          <header>
            <div><p className="section-label">Agent Skills</p><h2>受控解析链</h2></div>
            <StatusBadge tone="success">自动</StatusBadge>
          </header>
          <p>Agent 决定何时调用受控工具；来源身份、页码、表格单元格、需求边界和结构合法性仍由宿主校验。</p>
          <ul className="capability-lines">
            <li><span><strong>AI 阅读</strong><small>reqpilotx-document-reading</small></span><StatusBadge tone="success">Skill</StatusBadge></li>
            <li><span><strong>PDF 理解</strong><small>reqpilotx-pdf-understanding</small></span><StatusBadge tone="success">Skill</StatusBadge></li>
            <li><span><strong>结构修订</strong><small>reqpilotx-structure-tree-generation</small></span><StatusBadge tone="success">Skill</StatusBadge></li>
            <li><span><strong>需求识别</strong><small>reqpilotx-requirement-recognition</small></span><StatusBadge tone="success">Skill</StatusBadge></li>
            <li><span><strong>内容拆分</strong><small>reqpilotx-requirement-decomposition</small></span><StatusBadge tone="success">Skill</StatusBadge></li>
          </ul>
        </article>

        <article className="connection-card" aria-label="确定性来源捕获">
          <header>
            <div><p className="section-label">Source Capture</p><h2>确定性来源捕获</h2></div>
            <StatusBadge tone={!readiness ? "neutral" : readiness.runtime_ready ? "success" : "danger"}>
              {!readiness ? "本地核心状态未取得" : readiness.runtime_ready ? "本地核心就绪" : "本地核心异常"}
            </StatusBadge>
          </header>
          <p>PDF、DOCX、XLSX 先生成不可变 SourceIR，再交给 Agent 工具链理解。这个捕获层是证据工具，不是另一套“本地规则模型”。</p>
          <ul className="capability-lines">
            <li><span><strong>PDF</strong><small>原生文字、表格、图片与页坐标</small></span><StatusBadge tone="success">自动</StatusBadge></li>
            <li><span><strong>DOCX / XLSX</strong><small>段落、表格、单元格与顺序</small></span><StatusBadge tone="success">自动</StatusBadge></li>
            <li><span><strong>Apply</strong><small>必须通过宿主校验与人工确认</small></span><StatusBadge tone="neutral">受控</StatusBadge></li>
          </ul>
        </article>
      </div>

      <section className="worker-fleet-summary" aria-label="后台执行器状态">
        <header>
          <div><p className="section-label">Execution</p><h2>后台执行器</h2></div>
          <StatusBadge tone={workerLoading ? "warning" : workersReady ? "success" : "danger"}>
            {workerLoading ? "正在确认" : workersReady ? `${workerFleet?.eligible_worker_count || 0} 个槽位可用` : "暂无可用槽位"}
          </StatusBadge>
        </header>
        {workerError ? <InlineNotice tone="danger">{workerError}</InlineNotice> : null}
        {workerFleet?.workers.length ? (
          <ul className="worker-slot-list" aria-label="后台解析槽位">
            {workerFleet.workers.map((worker) => (
              <li key={`${worker.slot}:${worker.claim_profile}`}>
                <span><strong>槽位 {worker.slot}</strong><small>{worker.claim_profile} · 心跳 {workerLastSeenLabel(worker.last_seen_age_seconds)}</small></span>
                <StatusBadge tone={worker.eligible ? "success" : worker.fresh ? "warning" : "danger"}>{workerStateLabel(worker)}</StatusBadge>
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <section className="settings-project-card" aria-label="当前项目自动方案">
        <header>
          <div><p className="section-label">当前项目</p><h2>{selectedProject?.name || "尚未选择项目"}</h2></div>
          <StatusBadge tone={!selectedProject || !processingProfile || captureOnly ? "neutral" : automaticProfile ? "success" : "warning"}>{!selectedProject ? "尚未选择项目" : !processingProfile ? "方案尚未读取" : automaticProfile ? "自动方案" : captureOnly ? "仅原文捕获" : "旧自定义方案"}</StatusBadge>
        </header>
        <p>{providerRuntime
          ? "自主 Agent 任务使用上方供应商配置；当前项目的历史处理方案不改变运行时选择。单独阅读功能的可用性由对应入口判断。"
          : "自动方案固定为“确定性来源捕获 → Agent Skills → 宿主校验 → 人工 Diff / Apply”，不保存项目级模型供应商选择。"}</p>
        {codexRuntime && !automaticProfile && processingProfile ? (
          <footer>
            <small>{captureOnly ? "当前项目尚未启用 Agent 处理，可按需切换到自动方案。" : "该项目仍保留旧 provider 方案；恢复后只使用自动解析主路径。"}</small>
            <button className="button secondary" type="button" disabled={Boolean(busyAction) || !selectedProject} onClick={() => void restoreAutomaticProfile()}>{captureOnly ? "启用 Agent 自动方案" : "恢复自动方案"}</button>
          </footer>
        ) : null}
      </section>
    </section>
  );
}
