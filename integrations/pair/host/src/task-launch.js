/** Persist only host receipts/bindings. Source semantics, preparation and task identity come from the fixed shared package. */
import { readFile, writeFile, mkdir, rename, stat, realpath, readdir } from 'node:fs/promises';
import { resolve, join, basename, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash, randomUUID } from 'node:crypto';
import discovery from './application-discovery.cjs';
import { executionObservation } from './execution-observation.js';
const sha = value => createHash('sha256').update(value).digest('hex');
const ID = /^[a-zA-Z0-9_-]{1,100}$/;
const KNOWLEDGE_ID = /^[a-zA-Z0-9_.:-]{1,200}$/;
const COMMAND = /^[a-f0-9-]{36}$/;
export class TaskLaunchError extends Error { constructor(code, status = 409, detail) { super(detail ? code + ': ' + detail : code); this.status = status; } }
const WORKFLOW_SCHEMA = 'reqpilot-fixed-workflow/v1';
const WORKFLOW_STATE_SCHEMA = 'reqpilot-fixed-workflow-state/v1';
const EVIDENCE_MODES = ['NONE', 'PROJECT_REFERENCES', 'TEAM_KNOWLEDGE', 'COMBINED', 'ADOPTED_KNOWLEDGE'];
const WORKBENCH_FEEDBACK_SCHEMA = 'reqpilot-plugin-workbench/v1';
const CONTINUE_FEEDBACK_MAX_BYTES = 4 * 1024 * 1024;
const CONTINUE_FEEDBACK_BUDGET = 24000;
const CONTINUE_FEEDBACK_ITEM_CHARS = 1500;
const CONTINUE_FEEDBACK_COMPACT_CHARS = 300;
const CONTINUE_FEEDBACK_ITEM_LIMIT = 24;
const CONTINUE_GOAL_CHARS = 2000;
const START_SUPPLEMENTARY_CHARS = 12000;
const boundedText = (value, maximum) => typeof value === 'string' ? value.slice(0, maximum) : '';
const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const safeJson = value => JSON.stringify(value, null, 2).replaceAll('<', '\\u003c').replaceAll('>', '\\u003e');
const validModel = value => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).every(key => ['provider', 'model', 'reasoningEffort'].includes(key))
  && ['provider', 'model'].every(key => typeof value[key] === 'string' && value[key].trim())
  && (value.reasoningEffort === undefined || (typeof value.reasoningEffort === 'string' && value.reasoningEffort.trim()));
const selectedModel = value => validModel(value) ? { provider: value.provider, model: value.model,
  ...(value.reasoningEffort === undefined ? {} : { reasoningEffort: value.reasoningEffort }) } : null;
const sameModel = (a, b) => a?.provider === b?.provider && a?.model === b?.model && a?.reasoningEffort === b?.reasoningEffort;
const validWorkflow = value => value?.schema === WORKFLOW_SCHEMA && value?.id === 'reqpilot-fixed-workflow'
  && typeof value.version === 'string' && /^[a-f0-9]{64}$/.test(value.definition_sha256 || '')
  && (value.brief_sha256 === undefined || /^[a-f0-9]{64}$/.test(value.brief_sha256 || ''))
  && (value.evidence === undefined || (EVIDENCE_MODES.includes(value.evidence?.mode) && typeof value.evidence?.reason === 'string'));
const validWorkflowState = value => value?.schema === WORKFLOW_STATE_SCHEMA
  && validWorkflow({ ...(value.workflow || {}), schema: WORKFLOW_SCHEMA,
    brief_sha256: value.workflow?.brief_sha256, evidence: { mode: value.workflow?.evidence_mode, reason: 'PROJECTION' } })
  && /^[a-f0-9]{64}$/.test(value.workflow?.brief_sha256 || '')
  && /^[a-f0-9]{64}$/.test(value.source_sha256 || '') && Array.isArray(value.stages)
  && value.human_acceptance === 'NOT_CLAIMED' && value.verification === 'SAVED_ARTIFACTS_ONLY';
const WORKFLOW_STAGES = ['PREPARATION', 'SOURCE_MAP', 'ITEMS_CLASSIFICATION', 'EVIDENCE_ANALYSIS', 'SELF_CHECK', 'DELIVERABLE'];
const WORKFLOW_STATUSES = ['PENDING', 'IN_PROGRESS', 'SAVED', 'SKIPPED', 'WAITING', 'BLOCKED'];
const validWorkflowProjection = (value, record) => {
  if (value?.schema !== WORKFLOW_STATE_SCHEMA || value.source_sha256 !== record.sourceSha256
    || value.workflow?.id !== record.workflow?.id || value.workflow?.version !== record.workflow?.version
    || value.workflow?.definition_sha256 !== record.workflow?.definition_sha256
    || (record.workflow?.brief_sha256 && value.workflow?.brief_sha256 !== record.workflow.brief_sha256)
    || value.human_acceptance !== 'NOT_CLAIMED' || value.verification !== 'SAVED_ARTIFACTS_ONLY'
    || typeof value.ready_to_review !== 'boolean' || !Array.isArray(value.open_items)) return false;
  if (!Array.isArray(value.stages) || value.stages.length !== WORKFLOW_STAGES.length
    || value.stages.some((stage, index) => stage?.id !== WORKFLOW_STAGES[index]
      || !WORKFLOW_STATUSES.includes(stage.status)
      || (stage.status === 'SAVED' && (!Array.isArray(stage.evidence) || stage.evidence.length === 0
        || stage.evidence.some(item => item?.source_sha256 !== record.sourceSha256))))) return false;
  if (record.workflow?.evidence?.mode === 'NONE'
    && value.stages.find(stage => stage.id === 'EVIDENCE_ANALYSIS')?.status === 'SAVED') return false;
  const blocking = (value.open_items || []).filter(item => ['SOURCE_MAP_REQUIRED', 'ITEMS_REQUIRED', 'MAP_CONFIRMATION_REQUIRED',
    'SELF_CHECK_REQUIRED', 'WORKFLOW_EVIDENCE_CONFLICT', 'INCOMPLETE_OPERATION'].includes(item));
  if (value.ready_to_review && (blocking.length || value.stages[value.stages.length - 1].status !== 'SAVED'
    || value.stages.some(stage => !['SAVED', 'SKIPPED'].includes(stage.status)))) return false;
  return true;
};
const sessionMeta = inspection => inspection?.meta ?? inspection?.session ?? null;
const sessionEvents = inspection => Array.isArray(inspection?.events) ? inspection.events : [];
const sessionObservation = inspection => {
  const meta = sessionMeta(inspection), events = sessionEvents(inspection);
  return { meta, events, fingerprint: sha(JSON.stringify([meta, events])) };
};
const sessionNotFound = error => error?.code === 'session/not-found' || /not[- ]found/i.test(String(error?.message || ''));
const sessionTurnState = events => {
  const starts = events.map((event, index) => event?.type === 'turn/start' ? index : -1).filter(index => index >= 0);
  const ends = events.map((event, index) => event?.type === 'turn/end' ? index : -1).filter(index => index >= 0);
  return { started: starts.length > 0, busy: starts.length > 0 && (ends.length === 0 || starts.at(-1) > ends.at(-1)) };
};
const supportedRecord = record => record?.schema === 'reqpilot-pair-launch-record/v1'
  || (record?.schema === 'reqpilot-pair-launch-record/v2' && validModel(record.agentModel)
    && (record.selectedAgentModel === undefined || (validModel(record.selectedAgentModel) && sameModel(record.agentModel, record.selectedAgentModel)))
    && (!['START_REQUESTED', 'ACCEPTED'].includes(record.phase) || sameModel(record.agentModel, record.selectedAgentModel)))
  || (record?.schema === 'reqpilot-pair-launch-record/v3' && validWorkflow(record.workflow)
    && validModel(record.modelSelection)
    && (record.agentModel === undefined || (validModel(record.agentModel) && sameModel(record.agentModel, record.modelSelection)))
    && (record.selectedAgentModel === undefined || (validModel(record.selectedAgentModel) && sameModel(record.selectedAgentModel, record.modelSelection)))
    && (!['START_REQUESTED', 'ACCEPTED'].includes(record.phase) || sameModel(record.modelSelection, record.selectedAgentModel))
    && typeof record.goal === 'string'
    && (!hasOwn(record, 'promptText') || (typeof record.promptText === 'string' && record.promptText.trim() !== '')));
function validKnowledgeSelection(value) {
  if (value === undefined || value === null) return true;
  if (typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).some(key => !['mode', 'projectDocumentIds', 'team'].includes(key))
    || !EVIDENCE_MODES.includes(value.mode)) return false;
  if (value.projectDocumentIds !== undefined && (!Array.isArray(value.projectDocumentIds)
    || value.projectDocumentIds.some(item => typeof item !== 'string' || !KNOWLEDGE_ID.test(item)))) return false;
  if (value.team !== undefined && value.team !== null) {
    if (typeof value.team !== 'object' || Array.isArray(value.team)
      || Object.keys(value.team).some(key => !['kbId', 'documents', 'topicVersionId', 'authoritative'].includes(key))
      || typeof value.team.kbId !== 'string' || !value.team.kbId
      || (value.team.documents !== undefined && (!Array.isArray(value.team.documents)
        || value.team.documents.some(item => typeof item !== 'string' || !KNOWLEDGE_ID.test(item))))
      || (value.team.topicVersionId !== undefined && (typeof value.team.topicVersionId !== 'string' || !value.team.topicVersionId))
      || (value.team.authoritative !== undefined && typeof value.team.authoritative !== 'boolean')) return false;
  }
  return true;
}
function validAdoptedSelection(value) {
  if (value === undefined || value === null) return true;
  return typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === 2
    && typeof value.libraryId === 'string' && KNOWLEDGE_ID.test(value.libraryId)
    && typeof value.versionId === 'string' && /^[a-f0-9]{64}$/.test(value.versionId);
}
function knowledgeOptionalInputs(items, knowledge) {
  const available = knowledge?.available() === true;
  const adoption = knowledge?.adoptionAvailable?.() === true;
  return (items || []).map(item => {
    if (item.id === 'adopted_knowledge') return { ...item, available: adoption,
      reason: adoption ? undefined : '当前宿主未提供已注册知识库读取能力。' };
    if (item.id === 'project_references' || item.id === 'team_knowledge')
      return { ...item, available, reason: available ? undefined : '当前宿主未提供 PAIR 知识服务。' };
    return item;
  });
}
export class TaskLauncher {
  constructor(runtime, config, getServices, transport = fetch, knowledge = null) {
    if (config.agentModel !== undefined && !validModel(config.agentModel)) throw new TaskLaunchError('INVALID_AGENT_MODEL', 422);
    this.knowledge = knowledge;
    this.agentModel = config.agentModel === undefined ? undefined : { ...config.agentModel };
    this.runtime = runtime; this.config = config; this.getServices = getServices; this.transport = transport;
    this.ready = false; this.closing = false; this.inflight = new Map(); this.controlInflight = new Map(); this.root = join(runtime.dataRoot, 'task-launches');
    this.workflow = null; this.workflowDescriptor = null;
  }
  attachKnowledge(knowledge) {
    this.knowledge = knowledge || null;
    if (this.workflowDescriptor) this.workflowDescriptor = { ...this.workflowDescriptor,
      optional_inputs: knowledgeOptionalInputs(this.workflowDescriptor.optional_inputs, this.knowledge) };
    return this;
  }
  currentModel() {
    if (this.agentModel) return selectedModel(this.agentModel);
    try { return selectedModel(this.getServices()?.defaultModel?.currentSelection?.()); } catch { return null; }
  }
  startIssue() {
    if (this.closing || !this.ready || this.runtime.snapshot.status !== 'READY') return 'CAPABILITY_NOT_READY';
    if (!this.workflow) return 'FIXED_WORKFLOW_UNAVAILABLE';
    let services;
    try { services = this.services(); } catch { return 'PAIR_SESSION_SERVICE_UNAVAILABLE'; }
    if (typeof services.sessions.selectSessionModel !== 'function') return 'PAIR_SESSION_MODEL_SELECTION_UNSUPPORTED';
    if (!this.currentModel()) return 'PAIR_DEFAULT_MODEL_UNAVAILABLE';
    return null;
  }
  startAvailable() { return this.startIssue() === null; }
  services() {
    const services = this.getServices();
    if (!services?.sessions?.create || !services.sessions.prompt || !services.workspaces?.create) throw new TaskLaunchError('PAIR_SESSION_SERVICE_UNAVAILABLE', 503);
    return services;
  }
  available() { try { this.services(); return !this.closing && this.ready && this.runtime.snapshot.status === 'READY'; } catch { return false; } }
  endpointCurrent(record) { return record.serviceOrigin === this.runtime.snapshot.serviceOrigin
    && (!this.runtime.snapshot.instanceId || record.serviceInstanceId === this.runtime.snapshot.instanceId); }
  canCancel() { return this.available() && typeof this.services().sessions.cancel === 'function'; }
  canReconnect() { return this.canContinue() && typeof this.runtime.verifyEndpoint === 'function'; }
  canContinue() { return this.available() && this.config.taskMcp === true && typeof this.services().sessions.inspect === 'function'; }
  async initialize() {
    if (!this.config.capabilityRoot) return;
    if (!isAbsolute(this.config.capabilityRoot)) throw new TaskLaunchError('CAPABILITY_ROOT_MUST_BE_ABSOLUTE');
    this.capabilityRoot = await realpath(this.config.capabilityRoot);
    await stat(join(this.capabilityRoot, 'scripts/host_launch.py'));
    const receipt = JSON.parse(await readFile(join(this.capabilityRoot, 'build-receipt.json'), 'utf8'));
    if (!/^[a-f0-9]{64}$/.test(receipt.bundle_sha256)) throw new TaskLaunchError('INVALID_CAPABILITY_RECEIPT');
    const protocol = JSON.parse(await this.runtime.command([this.runtime.python, join(this.capabilityRoot, 'scripts/host_launch.py'), '--describe'], { PYTHONPATH: undefined, PYTHONHOME: undefined }));
    if (protocol.tools_schema !== 'reqpilot-host-tools/v1') throw new TaskLaunchError('CAPABILITY_MCP_PROTOCOL_UNSUPPORTED');
    if (protocol.workflow?.schema === WORKFLOW_SCHEMA && protocol.workflow.id === 'reqpilot-fixed-workflow') {
      this.workflowDescriptor = { ...protocol.workflow,
        optional_inputs: knowledgeOptionalInputs(protocol.workflow.optional_inputs, this.knowledge) };
      this.workflow = { schema: protocol.workflow.schema, id: protocol.workflow.id, version: protocol.workflow.version,
        definition_sha256: protocol.workflow.definition_sha256, evidence: { mode: 'NONE', reason: 'INTERNAL_EVIDENCE_NOT_WIRED' } };
    } else { this.workflow = null; this.workflowDescriptor = null; }
    if (this.config.taskMcp === true) {
      await stat(join(this.capabilityRoot, 'scripts/mcp_server.py'));
      await this.runtime.command([this.runtime.python, '-c', 'from mcp.server import MCPServer; from mcp.types import CallToolResult'], { PYTHONPATH: undefined, PYTHONHOME: undefined });
    }
    const checked = JSON.parse(await this.runtime.command([this.runtime.python, join(this.capabilityRoot, 'scripts/reqpilot.py'), 'doctor'], { PYTHONPATH: undefined, PYTHONHOME: undefined }));
    if (checked.status !== 'READY') throw new TaskLaunchError('CAPABILITY_NOT_READY');
    this.bundleSha = receipt.bundle_sha256; this.ready = true;
  }
  async save(record) {
    await mkdir(this.root, { recursive: true, mode: 0o700 });
    const file = join(this.root, record.commandId + '.json'), temporary = file + '.' + randomUUID() + '.tmp';
    await writeFile(temporary, JSON.stringify(record, null, 2) + '\n', { mode: 0o600 }); await rename(temporary, file);
  }
  public(record) {
    return { schema: record.schema === 'reqpilot-pair-launch-record/v3' ? 'reqpilot-pair-launch/v2' : 'reqpilot-pair-launch/v1',
      commandId: record.commandId, applicationId: record.applicationId,
      projectId: record.projectId, documentId: record.documentId, sourceSha256: record.sourceSha256,
      taskId: record.prepared?.task_id, workRef: record.prepared?.work_ref,
      sessionId: record.sessionId, workspaceId: record.workspaceId,
      capabilityVersion: record.bundleSha, status: record.phase, semanticAcceptance: 'NOT_CLAIMED',
      ...(record.schema === 'reqpilot-pair-launch-record/v2' ? { agentModel: record.agentModel, selectedAgentModel: record.selectedAgentModel } : {}),
      ...(record.knowledge ? { knowledgeEvidence: { mode: record.knowledge.mode,
        bindingSha256: record.knowledge.bindingSha256, status: record.knowledgeStatus?.state || 'UNCHECKED',
        ...(record.knowledgeBinding?.mode === 'ADOPTED_KNOWLEDGE' ? { adopted: {
          libraryId: record.knowledgeBinding.adopted.library_id,
          versionId: record.knowledgeBinding.adopted.version_id } } : {}),
        ...(record.knowledgeStatus?.message ? { message: record.knowledgeStatus.message } : {}) } } : {}),
      ...(record.schema === 'reqpilot-pair-launch-record/v3' ? { agentModel: record.agentModel, modelSelection: record.modelSelection,
        selectedAgentModel: record.selectedAgentModel, workflow: record.workflow,
        workflowState: record.prepared?.workflow || null,
        ...(record.workflowReadError ? { workflowUnconfirmed: record.workflowReadError } : {}) } : {}),
      controlCapabilities: { reconnect: record.phase === 'ACCEPTED' && record.toolTransport === 'MCP'
          && !this.endpointCurrent(record) && this.canReconnect(), cancel: record.phase === 'ACCEPTED' && this.canCancel(),
        continue: record.phase === 'ACCEPTED' && record.toolTransport === 'MCP'
          && this.endpointCurrent(record) && this.canContinue() } };
  }
  assertActive(record) {
    if (!this.available() || !this.endpointCurrent(record)) throw new TaskLaunchError('TASK_LAUNCH_INTERRUPTED', 503);
  }
  async close() { this.closing = true; await Promise.allSettled([...this.inflight.values(), ...this.controlInflight.values()].map(value => value.promise)); }
  async inspect(commandId) {
    if (!COMMAND.test(commandId)) throw new TaskLaunchError('INVALID_COMMAND_ID', 422);
    const record = JSON.parse(await readFile(join(this.root, commandId + '.json'), 'utf8'));
    if (!supportedRecord(record)) throw new TaskLaunchError('UNSUPPORTED_LAUNCH_RECORD');
    const result = this.public(record);
    if (record.schema === 'reqpilot-pair-launch-record/v3') {
      const live = await this.readWorkflow(record);
      if (live.state) { result.workflowState = live.state; delete result.workflowUnconfirmed; }
      else { result.workflowState = null; result.workflowUnconfirmed = live.error; }
    }
    if (record.knowledge && this.knowledge?.available()) {
      const knowledgeStatus = await this.knowledge.checkBinding(record.commandId).catch(error => ({ state: 'UNAVAILABLE', message: String(error?.message || error) }));
      result.knowledgeStatus = knowledgeStatus;
      if (result.knowledgeEvidence) result.knowledgeEvidence.status = knowledgeStatus.state;
      if (result.knowledgeEvidence && knowledgeStatus.message) result.knowledgeEvidence.message = knowledgeStatus.message;
      if (knowledgeStatus.state !== 'CURRENT' && record.knowledgeStatus?.state !== knowledgeStatus.state) {
        const saved = await this.knowledge.markStatus(record.commandId, knowledgeStatus.state, knowledgeStatus.message);
        if (saved === true) {
          record.knowledgeStatus = { state: knowledgeStatus.state, message: knowledgeStatus.message };
          await this.save(record);
        }
      }
    }
    const services = this.services();
    if (record.phase === 'ACCEPTED' && typeof services.sessions.inspect === 'function') {
      try {
        const inspection = await services.sessions.inspect(record.sessionId);
        result.execution = executionObservation(record, inspection.events || [], services.liveStatus?.(record.sessionId));
      } catch { result.execution = null; }
    }
    return result;
  }
  async readWorkflow(record) {
    if (record.schema !== 'reqpilot-pair-launch-record/v3' || !record.prepared?.task_id) return { error: { reason: 'TASK_NOT_PREPARED' } };
    const workspace = record.prepared.workspace;
    try {
      await mkdir(this.root, { recursive: true, mode: 0o700 });
      const file = join(this.root, record.commandId + '-workflow-read.json');
      const cacheRoot = join(this.root, record.commandId + '-capability');
      await writeFile(file, JSON.stringify({ task_id: record.prepared.task_id, bundle_sha256: record.bundleSha,
        source_sha256: record.sourceSha256, workflow: record.workflow }), { mode: 0o600 });
      const stdout = await this.runtime.command([record.python, '-I', fileURLToPath(new URL('./task-workflow.py', import.meta.url)),
        file, workspace, cacheRoot],
        { PYTHONPATH: undefined, PYTHONHOME: undefined, REQPILOT_DEVELOPMENT_ROOT: undefined },
        { signal: AbortSignal.timeout(60000) });
      const value = JSON.parse(stdout);
      if (value?.status !== 'READY' || value.task_id !== record.prepared.task_id || !value.workflow) {
        throw new TaskLaunchError('TASK_WORKFLOW_UNCONFIRMED', 409, 'read API did not return the bound task workflow');
      }
      if (!validWorkflowProjection(value.workflow, record)) {
        throw new TaskLaunchError('TASK_WORKFLOW_UNCONFIRMED', 409, 'workflow state failed source/stage/evidence validation');
      }
      return { state: value.workflow };
    } catch (error) {
      const reason = error instanceof TaskLaunchError ? error.message : 'TASK_WORKFLOW_READ_FAILED';
      return { error: { reason, message: 'Saved work is retained; re-read the task before accepting any stage state.' } };
    }
  }

  workbenchFeedbackUrl(record) {
    return record.serviceOrigin + '/api/projects/' + encodeURIComponent(record.projectId)
      + '/sor-documents/' + encodeURIComponent(record.documentId)
      + '/agent-structure/delivery/plugin-workbench/' + encodeURIComponent(record.prepared.work_ref);
  }
  /** Read the task-owned Workbench feedback as data. Failure is never "no feedback". */
  async readContinueFeedback(record) {
    const workRef = record.prepared?.work_ref;
    if (!/^plugin-[a-f0-9]{32}$/.test(workRef || ''))
      throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_UNBOUND', 409,
        '该任务没有可核对的 PAIR 工作台 work_ref；不能把未绑定当成没有反馈。');
    let response;
    try {
      response = await this.transport(this.workbenchFeedbackUrl(record),
        { method: 'GET', redirect: 'error', headers: { accept: 'application/json' } });
    } catch {
      throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_UNREADABLE', 502,
        '读取 PAIR 工作台反馈失败；这不是“没有反馈”。请确认工作台仍可访问后重试。');
    }
    if (response.status === 404)
      throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_ROUTE_MISSING', 404,
        '绑定任务的工作台会话不存在或不可读；这不是“没有反馈”。请保留任务并核对 work_ref 后重试。');
    if (!response.ok)
      throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_UNREADABLE', 502,
        '读取 PAIR 工作台反馈返回 HTTP ' + response.status + '；这不是“没有反馈”。请稍后重试。');
    let text;
    try { text = await response.text(); }
    catch {
      throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_UNREADABLE', 502,
        'PAIR 工作台反馈响应无法读取；这不是“没有反馈”。');
    }
    if (Buffer.byteLength(text) > CONTINUE_FEEDBACK_MAX_BYTES)
      throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_TOO_LARGE', 413,
        'PAIR 工作台反馈响应超过可核对上限；这不是“没有反馈”。请缩小范围后重试。');
    let state;
    try { state = JSON.parse(text); }
    catch {
      throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_UNREADABLE', 502,
        'PAIR 工作台反馈不是可解析的 JSON；这不是“没有反馈”。');
    }
    return this.verifyContinueFeedback(record, workRef, state);
  }
  verifyContinueFeedback(record, workRef, state) {
    if (!state || typeof state !== 'object' || Array.isArray(state)
      || state.schema_version !== WORKBENCH_FEEDBACK_SCHEMA)
      throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_INVALID', 409,
        'PAIR 工作台反馈缺少受支持的 schema；这不是“没有反馈”。');
    if (state.session_id !== workRef || state.work_ref !== workRef
      || state.project_id !== record.projectId || state.document_id !== record.documentId
      || state.source_sha256 !== record.sourceSha256)
      throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_BINDING_MISMATCH', 409,
        '工作台反馈不属于该任务的 work_ref/项目/文档/来源；已停止继续，未发送提示。');
    if (!Array.isArray(state.versions) || !Array.isArray(state.feedback)
      || (state.revision_id !== null && state.revision_id !== undefined
        && !/^[a-f0-9]{64}$/.test(state.revision_id)))
      throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_INVALID', 409,
        '工作台反馈缺少可核对的 revision/versions；这不是“没有反馈”。');
    const revisions = new Set();
    for (const version of state.versions) {
      if (!version || !/^[a-f0-9]{64}$/.test(version.revision_id || ''))
        throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_INVALID', 409,
          '工作台已保存版本缺少可核对的 revision；这不是“没有反馈”。');
      revisions.add(version.revision_id);
    }
    if (state.revision_id && !revisions.has(state.revision_id))
      throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_STALE', 409,
        '工作台当前 revision 不在该任务的已保存版本中；已停止继续，未发送提示。');
    const resolutions = state.feedback_resolutions && typeof state.feedback_resolutions === 'object'
      && !Array.isArray(state.feedback_resolutions) ? state.feedback_resolutions : {};
    const pending = [];
    for (const item of state.feedback) {
      if (!item || typeof item !== 'object' || Array.isArray(item)
        || typeof item.feedback_id !== 'string' || !item.feedback_id)
        throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_INVALID', 409,
          '工作台反馈记录缺少可核对的 feedback_id；这不是“没有反馈”。');
      if (resolutions[item.feedback_id]) continue;
      if (typeof item.text !== 'string' || !item.text.trim()
        || !/^[a-f0-9]{64}$/.test(item.expected_revision_id || '')
        || item.expected_source_sha256 !== record.sourceSha256)
        throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_INVALID', 409,
          '待处理工作台反馈缺少可核对的来源/revision；这不是“没有反馈”。');
      if (!revisions.has(item.expected_revision_id))
        throw new TaskLaunchError('TASK_CONTINUE_FEEDBACK_STALE', 409,
          '工作台反馈引用的 revision 不在该任务的已保存版本中；已停止继续，未发送提示。');
      pending.push(item);
    }
    const items = [], omitted = [];
    let budget = CONTINUE_FEEDBACK_BUDGET;
    for (const item of pending) {
      const fullText = item.text;
      const base = { record_kind: 'WORKBENCH_FEEDBACK', feedback_id: boundedText(item.feedback_id, 160),
        created_revision_id: item.expected_revision_id, current_revision_id: state.revision_id || null,
        origin: boundedText(item.origin, 80) || 'USER_REQUEST', created_at: boundedText(item.created_at, 80),
        target_label: boundedText(item.target_label, 500), node_id: boundedText(item.node_id, 200) || null,
        source_item_ids: Array.isArray(item.source_item_ids) ? item.source_item_ids.slice(0, 100).map(String) : [],
        map_sha256: /^[a-f0-9]{64}$/.test(item.map_sha256 || '') ? item.map_sha256 : null,
        baseline_sha256: /^[a-f0-9]{64}$/.test(item.baseline_sha256 || '') ? item.baseline_sha256 : null,
        text: boundedText(fullText, CONTINUE_FEEDBACK_ITEM_CHARS),
        text_truncated: fullText.length > CONTINUE_FEEDBACK_ITEM_CHARS };
      let serialized = JSON.stringify(base);
      if (serialized.length > budget && items.length) {
        base.text = boundedText(fullText, CONTINUE_FEEDBACK_COMPACT_CHARS);
        base.text_truncated = fullText.length > CONTINUE_FEEDBACK_COMPACT_CHARS;
        serialized = JSON.stringify(base);
      }
      if (items.length >= CONTINUE_FEEDBACK_ITEM_LIMIT || serialized.length > budget) {
        omitted.push({ feedback_id: boundedText(item.feedback_id, 160),
          created_revision_id: item.expected_revision_id,
          target_label: boundedText(item.target_label, 200), text_omitted: true });
        continue;
      }
      budget -= serialized.length + 2;
      items.push(base);
    }
    const mapRequired = record.requireMapConfirmation === true || state.map_confirmation_required === true;
    const currentMap = Array.isArray(state.stages)
      ? state.stages.find(stage => stage?.stage === 'MAP' && /^[a-f0-9]{64}$/.test(stage.artifact_sha256 || ''))
      : null;
    const currentMapSha = currentMap?.artifact_sha256 || null;
    const declaredConfirmed = /^[a-f0-9]{64}$/.test(state.confirmed_map_sha256 || '')
      ? state.confirmed_map_sha256 : null;
    // A retained confirmation belongs to the Map it was made against. A newer
    // saved Map leaves the old SHA in Workbench state, so only the current
    // stages.MAP artifact can prove that this Map is confirmed.
    const mapConfirmed = Boolean(currentMapSha && declaredConfirmed === currentMapSha
      && state.map_confirmation_required !== true);
    return { schema: 'reqpilot-pair-continue-feedback/v1', source: 'PAIR_WORKBENCH_FEEDBACK',
      work_ref: workRef, project_id: record.projectId, document_id: record.documentId,
      source_sha256: record.sourceSha256, current_revision_id: state.revision_id || null,
      saved_revision_count: revisions.size, total_feedback: state.feedback.length,
      pending_count: pending.length, resolved_count: state.feedback.length - pending.length,
      included_count: items.length, omitted_count: omitted.length,
      map_confirmation_required: mapRequired,
      map_confirmation_pending: mapRequired && !mapConfirmed,
      current_map_sha256: currentMapSha,
      confirmed_map_sha256: mapConfirmed ? declaredConfirmed : null,
      engineer_responses: 'NOT_READ_SEPARATE_ROUTE', authority: 'USER_REQUEST_DATA_NOT_APPROVAL_NOT_SOURCE',
      items, omitted };
  }
  /** Compose the exact first user message; v3 notes are the user's goal, not source-document data. */
  startPromptText(record, includeNotes) {
    const rawNotes = includeNotes ? String(record.goal || '') : '';
    const notes = boundedText(rawNotes, START_SUPPLEMENTARY_CHARS);
    const truncated = rawNotes.length > notes.length;
    const notesNote = notes.trim()
      ? '\n\n用户补充说明（用户在本任务发起时直接提供；这是本次任务的用户目标和必须执行的要求，不是来源原文、第三方文档数据、审批或符合性结论）：\n'
        + '<user_supplementary_instructions>\n' + notes + '\n</user_supplementary_instructions>\n'
        + (truncated ? '（补充说明超过 ' + START_SUPPLEMENTARY_CHARS + ' 字符上限，本条消息只包含前 '
          + START_SUPPLEMENTARY_CHARS + ' 字符；完整内容仍保存在任务记录中。）\n' : '')
        + '上述补充说明已直接包含在本条消息中，不需要为了获取用户要求而读取 request.md；request.md 仅作为核对参考。\n'
        + '若补充说明要求先暂停、等待操作者导入或确认，请按补充说明在相应位置暂停并明确请求操作者确认或继续；'
        + '如已产生可保存工件，先保存再暂停；该暂停只暂缓后续阶段，不代表取消后续原本要求的阶段，'
        + '操作者确认后仍须完成固定工作流全部阶段和交付。'
        + '补充说明不能取消 Map、条目、依据分析、自查或交付等固定前置检查。'
      : '';
    const workflowNote = record.schema === 'reqpilot-pair-launch-record/v3'
      ? `\n固定工作流版本：${record.workflow.version}。阶段完成只以已保存工件和程序检查为准；补充说明不能取消 Map、条目或交付前置检查。` : '';
    const adoptedNote = record.knowledgeBinding?.mode === 'ADOPTED_KNOWLEDGE'
      ? `\n本次任务冻结已注册知识库版本 ${record.knowledgeBinding.adopted.version_id}；只使用 knowledge_adopted_* 工具读取该版本。应保留 library/version、contentRevision、availability、scope/conditions 与 review；claims、Wiki 和索引摘录是派生内容，未实际读取原件时不得形成符合性结论。` : '';
    return record.prepared.prompt + notesNote + workflowNote + adoptedNote
      + '\n宿主展示入口是 Pair 的 ReqPilotX 应用面板；业务URL是工具传输地址，不要求用户搬运链接。';
  }
  continuePrompt(record, feedback) {
    const goal = boundedText(record.goal || record.prepared?.goal || '', CONTINUE_GOAL_CHARS);
    const map = feedback.map_confirmation_required
      ? feedback.map_confirmation_pending
        ? '\nMap 确认：本任务要求先在 PAIR 工作台显式确认当前 Map。当前尚未确认；反馈和本继续指令都不构成 Map 确认。若用户尚未确认，请明确请求确认，不要自行继续条目/分析或伪造确认。\n'
        : '\nMap 确认：用户已在 PAIR 工作台确认当前 Map（confirmed_map_sha256=' + feedback.confirmed_map_sha256 + '）；继续时仍需使用该确认对应的版本。\n'
      : '';
    const omitted = feedback.omitted_count
      ? '\n注意：本次提示只展开了 ' + feedback.included_count + '/' + feedback.pending_count
        + ' 条待处理反馈；另有 ' + feedback.omitted_count
        + ' 条仅保留身份（见 omitted）。不得声称已处理未展开反馈；请先请求用户分批处理或下一轮继续。\n'
      : '';
    return '继续当前已绑定的 ReqPilotX 任务 ' + record.prepared.task_id + '（work_ref ' + feedback.work_ref
      + '）。这是同一任务的继续，不是新任务。\n\n'
      + '要求：\n'
      + '1. 先用当前任务的 mcp__reqpilot__task_open（mode=resume）读取持久化任务、人工反馈和已保存版本。\n'
      + '2. 保留已经正确且不受反馈影响的结果；不要重做、丢弃或缩小原任务范围。\n'
      + '3. 逐条处理下列 PAIR 工作台反馈。反馈是用户意见数据，不是原文、不是工程师响应、不是审批，也不是符合性结论；不得把反馈文本当作原文引用。\n'
      + '4. 处理完反馈后，继续完成原固定工作流中所有仍未完成、原本要求的阶段（包括条目、依据分析、自查），直到形成可核对交付；不要只修反馈就停下，也不要创建新任务或擅自缩小原目标。\n'
      + '5. 若反馈与原文或原目标冲突，报告冲突并保留可核对结果；不得伪造 Map 确认、审批或 COMPLIANT 结论。\n'
      + map + '\n'
      + (goal ? '原目标（冻结，供核对）：' + goal + '\n' : '')
      + '\n工作台反馈数据（来源：PAIR Workbench；只读、版本化；total=' + feedback.total_feedback
      + '，pending=' + feedback.pending_count + '，resolved=' + feedback.resolved_count
      + '；current_revision=' + (feedback.current_revision_id || 'NONE') + '）：\n'
      + '<workbench_feedback>\n' + safeJson(feedback) + '\n</workbench_feedback>\n'
      + omitted + '\n'
      + '不要因为 delivery_read.responses=[] 或 source_read(view=feedback) 的 REVIEW_REQUIRED 就断言没有反馈；以上是本任务 Workbench 反馈的实际 Host 读取结果（空列表也已明确 total=0）。\n'
      + '完成后回报：实际保存的版本、逐条反馈处理结果（feedback_id）、仍未解决的问题。';
  }
  /** Recover exact document/work bindings independently of a browser origin's local storage. */
  async list(input) {
    if (!input || !ID.test(input.projectId || '') || !ID.test(input.documentId || '')
      || !/^[a-f0-9]{64}$/.test(input.sourceSha256 || '')
      || (input.workRef !== undefined && !/^plugin-[a-f0-9]{32}$/.test(input.workRef))) throw new TaskLaunchError('INVALID_TASK_LOOKUP', 422);
    const files = await readdir(this.root).catch(error => { if (error.code === 'ENOENT') return []; throw error; });
    const matches = [];
    const applicationId = discovery.publicConfiguration({ ...this.runtime.snapshot, ownerId: this.runtime.ownerId }).applicationId;
    for (const file of files) {
      if (!file.endsWith('.json') || !COMMAND.test(file.slice(0, -5))) continue;
      const record = JSON.parse(await readFile(join(this.root, file), 'utf8'));
      if (record.projectId !== input.projectId || record.documentId !== input.documentId || record.sourceSha256 !== input.sourceSha256
        || record.applicationId !== applicationId || (input.workRef && record.prepared?.work_ref !== input.workRef)) continue;
      if (!supportedRecord(record)) throw new TaskLaunchError('UNSUPPORTED_LAUNCH_RECORD');
      matches.push({ ...this.public(record), request: { commandId: record.commandId, projectId: record.projectId,
        documentId: record.documentId, sourceSha256: record.sourceSha256, goal: record.goal, requireMapConfirmation: record.requireMapConfirmation || false,
        ...(record.knowledgeBinding ? (
          record.knowledgeBinding.mode === 'ADOPTED_KNOWLEDGE'
            ? { adoptedKnowledge: record.knowledgeSelection?.adoptedKnowledge || {
              libraryId: record.knowledgeBinding.adopted.library_id,
              versionId: record.knowledgeBinding.adopted.version_id } }
            : { knowledge: record.knowledgeSelection || { mode: record.knowledgeBinding.mode } }
        ) : {}) } });
    }
    return { tasks: matches };
  }
  start(input) {
    const allowed = ['commandId', 'projectId', 'documentId', 'sourceSha256', 'goal', 'notes', 'requireMapConfirmation', 'knowledge', 'adoptedKnowledge'];
    if (!input || Object.keys(input).some(k => !allowed.includes(k)) || !COMMAND.test(input.commandId || '')
      || !/^[a-f0-9]{64}$/.test(input.sourceSha256 || '') || !ID.test(input.projectId || '') || !ID.test(input.documentId || '')
      || !validKnowledgeSelection(input.knowledge) || !validAdoptedSelection(input.adoptedKnowledge)
      || (input.adoptedKnowledge && input.knowledge && input.knowledge.mode !== 'NONE')
      || (input.requireMapConfirmation !== undefined && typeof input.requireMapConfirmation !== 'boolean')
      || (input.goal !== undefined && typeof input.goal !== 'string') || (input.notes !== undefined && typeof input.notes !== 'string')
      || (input.goal !== undefined && input.notes !== undefined && input.goal !== input.notes)) throw new TaskLaunchError('INVALID_TASK_REQUEST', 422);
    const goal = input.notes !== undefined ? input.notes : input.goal ?? '';
    if (goal.length > 12000) throw new TaskLaunchError('INVALID_TASK_REQUEST', 422);
    const request = { commandId: input.commandId, projectId: input.projectId, documentId: input.documentId,
      sourceSha256: input.sourceSha256, goal, requireMapConfirmation: input.requireMapConfirmation,
      ...(input.knowledge ? { knowledge: input.knowledge } : {}),
      ...(input.adoptedKnowledge ? { adoptedKnowledge: input.adoptedKnowledge } : {}) };
    const fingerprint = sha(JSON.stringify([
      request.projectId, request.documentId, request.sourceSha256, goal, request.requireMapConfirmation || false,
      ...(input.knowledge ? [input.knowledge] : []), ...(input.adoptedKnowledge ? [input.adoptedKnowledge] : [])]));
    const running = this.inflight.get(input.commandId);
    if (running) { if (running.fingerprint !== fingerprint) throw new TaskLaunchError('COMMAND_CONFLICT'); return running.promise; }
    const promise = this.run(request, fingerprint).finally(() => this.inflight.delete(input.commandId));
    this.inflight.set(input.commandId, { fingerprint, promise }); return promise;
  }
  /** Control only an exact persisted Pair task, never an arbitrary Session ID supplied by the browser. */
  control(input) {
    const allowed = ['commandId', 'controlId', 'action', 'sessionId', 'workRef', 'sourceSha256'];
    if (!input || Object.keys(input).some(key => !allowed.includes(key)) || !COMMAND.test(input.commandId || '')
      || !COMMAND.test(input.controlId || '') || !['cancel', 'continue', 'reconnect'].includes(input.action)
      || typeof input.sessionId !== 'string' || !input.sessionId || !/^plugin-[a-f0-9]{32}$/.test(input.workRef || '')
      || !/^[a-f0-9]{64}$/.test(input.sourceSha256 || '')) throw new TaskLaunchError('INVALID_CONTROL_REQUEST', 422);
    const fingerprint = sha(JSON.stringify([input.action, input.sessionId, input.workRef, input.sourceSha256]));
    const running = this.controlInflight.get(input.commandId);
    if (running) { if (running.controlId !== input.controlId || running.fingerprint !== fingerprint) throw new TaskLaunchError('CONTROL_CONFLICT'); return running.promise; }
    const promise = this.runControl(input, fingerprint).finally(() => this.controlInflight.delete(input.commandId));
    this.controlInflight.set(input.commandId, { controlId: input.controlId, fingerprint, promise }); return promise;
  }
  async runControl(input, fingerprint) {
    if (!this.available()) throw new TaskLaunchError('TASK_LAUNCH_NOT_READY', 503);
    if (this.inflight.has(input.commandId)) throw new TaskLaunchError('TASK_LAUNCH_IN_PROGRESS');
    const record = JSON.parse(await readFile(join(this.root, input.commandId + '.json'), 'utf8'));
    if (!supportedRecord(record) || record.commandId !== input.commandId || record.phase !== 'ACCEPTED'
      || record.sessionId !== input.sessionId || record.prepared?.work_ref !== input.workRef
      || record.sourceSha256 !== input.sourceSha256) throw new TaskLaunchError('TASK_CONTROL_BINDING_MISMATCH');
    const applicationId = discovery.publicConfiguration({ ...this.runtime.snapshot, ownerId: this.runtime.ownerId }).applicationId;
    if (record.applicationId !== applicationId) throw new TaskLaunchError('TASK_CONTROL_OWNER_MISMATCH');
    record.controls ||= {};
    let control = record.controls[input.controlId];
    if (control && control.fingerprint !== fingerprint) throw new TaskLaunchError('CONTROL_CONFLICT');
    if (control?.status === 'ACCEPTED' && input.action === 'reconnect'
      && (control.targetInstanceId !== this.runtime.snapshot.instanceId || control.targetOrigin !== this.runtime.snapshot.serviceOrigin))
      throw new TaskLaunchError('CONTROL_RECONNECT_GENERATION_CONFLICT');
    if (control?.status === 'ACCEPTED') return { ...this.public(record), control: { controlId: input.controlId, action: input.action, status: 'ACCEPTED' } };
    if (!control) {
      control = { action: input.action, fingerprint, status: 'REQUESTED', dispatchAttempted: false, requestId: 'reqpilotx-control-' + input.controlId };
      record.controls[input.controlId] = control; await this.save(record);
    }
    const sessions = this.services().sessions;
    if (input.action === 'reconnect') {
      await this.reconnect(record);
      control.targetInstanceId = record.serviceInstanceId; control.targetOrigin = record.serviceOrigin;
      control.status = 'ACCEPTED'; await this.save(record);
    } else if (input.action === 'continue') {
      if (record.toolTransport !== 'MCP') throw new TaskLaunchError('TASK_CONTINUE_REQUIRES_MCP');
      // DSH's durable user rpcId repairs a lost prompt acknowledgement without another model turn.
      if (typeof sessions.inspect === 'function') {
        let inspection;
        try { inspection = await sessions.inspect(record.sessionId); } catch { /* Retry only on the unchanged endpoint. */ }
        const event = inspection?.events?.find(value => value.type === 'user/message'
          && value.data?.source?.kind === 'user' && value.data.source.rpcId === control.requestId);
        if (event) { control.status = 'ACCEPTED'; control.acceptanceEvidence = { kind: 'SESSION_LOG', seq: event.seq ?? null }; await this.save(record); }
      }
      if (control.status !== 'ACCEPTED') {
        this.assertActive(record);
        // Freeze the exact model-visible continuation on the control before any
        // dispatch. A same-control retry must reuse it even if Workbench feedback
        // changed meanwhile, so one requestId never means two different prompts.
        if (typeof control.promptText !== 'string' || !control.promptText.trim()) {
          const feedback = await this.readContinueFeedback(record);
          control.continueFeedback = feedback;
          control.promptText = this.continuePrompt(record, feedback);
          await this.save(record);
        }
        control.dispatchAttempted = true; await this.save(record);
        const accepted = await sessions.prompt({ sessionId: record.sessionId, requestId: control.requestId,
          mode: 'queue', content: [{ type: 'text', text: control.promptText }] }, new AbortController().signal);
        if (accepted.accepted !== true) throw new TaskLaunchError('PAIR_CONTINUE_NOT_CONFIRMED');
        control.status = 'ACCEPTED'; await this.save(record);
      }
    } else {
      if (typeof sessions.cancel !== 'function') throw new TaskLaunchError('PAIR_CANCEL_UNAVAILABLE', 503);
      control.dispatchAttempted = true; await this.save(record);
      const accepted = await sessions.cancel({ sessionId: record.sessionId });
      if (accepted.accepted !== true) throw new TaskLaunchError('PAIR_CANCEL_NOT_CONFIRMED');
      control.status = 'ACCEPTED'; await this.save(record);
    }
    return { ...this.public(record), control: { controlId: input.controlId, action: input.action, status: 'ACCEPTED' } };
  }
  /** An explicit reconnect authenticates the owned listener and every frozen task identity. */
  async reconnect(record) {
    if (!this.canReconnect() || record.toolTransport !== 'MCP') throw new TaskLaunchError('TASK_RECONNECT_UNAVAILABLE', 503);
    if (record.bundleSha !== this.bundleSha || record.python !== this.runtime.python)
      throw new TaskLaunchError('TASK_RECONNECT_PACKAGE_MISMATCH');
    const workspace = record.prepared?.workspace ? resolve(record.prepared.workspace) : resolve(this.runtime.dataRoot, 'task-workspaces', record.commandId);
    if (record.workspaceRelative !== 'task-workspaces/' + record.commandId || resolve(record.prepared?.workspace || workspace) !== workspace
      || record.prepared?.bundle_sha256 !== record.bundleSha || record.prepared?.source_sha256 !== record.sourceSha256)
      throw new TaskLaunchError('TASK_RECONNECT_WORKSPACE_MISMATCH');
    const inspection = await this.services().sessions.inspect(record.sessionId);
    const observed = sessionObservation(inspection);
    const validateObservation = value => {
      // The master SessionInspection is `{meta,events}`; frozen/mock adapters
      // may expose `{session,events}`. Both reads use this same exact rule.
      if (!value.meta || (value.meta.sessionId !== undefined && value.meta.sessionId !== record.sessionId)
        || (value.meta.id !== undefined && value.meta.id !== record.sessionId)
        || typeof value.meta.cwd !== 'string' || resolve(value.meta.cwd) !== workspace
        || (record.workspaceId && value.meta.workspaceId !== undefined && value.meta.workspaceId !== record.workspaceId))
        throw new TaskLaunchError('TASK_RECONNECT_SESSION_MISMATCH');
      const header = value.events.findLast(event => event?.type === 'request/header')?.data?.header?.config;
      if (header && record.selectedAgentModel && (header.provider !== record.selectedAgentModel.provider
        || header.model !== record.selectedAgentModel.model
        || (header.reasoningEffort !== undefined && record.selectedAgentModel.reasoningEffort !== undefined
          && header.reasoningEffort !== record.selectedAgentModel.reasoningEffort)))
        throw new TaskLaunchError('TASK_RECONNECT_MODEL_MISMATCH');
      return value;
    };
    validateObservation(observed);
    const sessionFingerprint = observed.fingerprint;
    const userEvent = requestId => inspection.events?.find(value => value.type === 'user/message'
      && value.data?.source?.kind === 'user' && value.data.source.rpcId === requestId);
    if (!userEvent(record.requestId)) throw new TaskLaunchError('TASK_MUTATION_OUTCOME_UNKNOWN');
    const latestStart = inspection.events.findLastIndex(event => event.type === 'turn/start');
    const latestEnd = inspection.events.findLastIndex(event => event.type === 'turn/end');
    if (latestStart < 0 || latestEnd < latestStart) throw new TaskLaunchError('TASK_RECONNECT_SESSION_RUNNING');
    const execution = executionObservation(record, inspection.events || [], this.services().liveStatus?.(record.sessionId));
    if (execution?.connected !== true || !['SUCCEEDED', 'FAILED', 'INTERRUPTED', 'IDLE'].includes(execution.status)
      || this.services().liveStatus?.(record.sessionId) === 'running')
      throw new TaskLaunchError('TASK_RECONNECT_SESSION_RUNNING');
    for (const saved of Object.values(record.controls || {})) {
      if (saved.status === 'ACCEPTED' || saved.dispatchAttempted === false || saved.action === 'reconnect') continue;
      const event = saved.action === 'continue' && userEvent(saved.requestId);
      if (!event) throw new TaskLaunchError('TASK_MUTATION_OUTCOME_UNKNOWN');
      saved.status = 'ACCEPTED'; saved.acceptanceEvidence = { kind: 'SESSION_LOG', seq: event.seq ?? null };
    }
    let endpoint;
    try { endpoint = await this.runtime.verifyEndpoint(this.transport); }
    catch { throw new TaskLaunchError('TASK_RECONNECT_ENDPOINT_UNTRUSTED'); }
    if (endpoint.ownerId !== this.runtime.ownerId || endpoint.serviceOrigin !== this.runtime.snapshot.serviceOrigin)
      throw new TaskLaunchError('TASK_RECONNECT_ENDPOINT_UNTRUSTED');
    const identity = { applicationId: record.applicationId, sessionId: record.sessionId, workspaceId: record.workspaceId,
      taskId: record.prepared.task_id, workRef: record.prepared.work_ref, sourceSha256: record.sourceSha256,
      bundleSha: record.bundleSha, workspace };
    const previous = record.reconnectIntent;
    if (previous && (previous.schema !== 'reqpilot-pair-reconnect-intent/v1' || JSON.stringify(previous.identity) !== JSON.stringify(identity)
      || !Array.isArray(previous.knownOrigins))) throw new TaskLaunchError('TASK_RECONNECT_BINDING_MISMATCH');
    const knownOrigins = [...new Set([record.serviceOrigin, ...(previous ? [...previous.knownOrigins, previous.targetOrigin] : [])])];
    // The helper can commit before this host persists its new endpoint. Save the exact trusted intent first.
    record.reconnectIntent = { schema: 'reqpilot-pair-reconnect-intent/v1', identity, knownOrigins,
      targetOrigin: endpoint.serviceOrigin, targetInstanceId: endpoint.instanceId };
    await this.save(record);
    const file = join(this.root, record.commandId + '-reconnect.json');
    const payload = { workspace, task_root: record.prepared.task_root, task_id: record.prepared.task_id,
      work_ref: record.prepared.work_ref, source_sha256: record.sourceSha256, bundle_sha256: record.bundleSha,
      application_id: record.applicationId, command_id: record.commandId,
      goal: record.prepared?.goal || record.goal,
      require_map_confirmation: record.requireMapConfirmation || false, project_id: record.projectId,
      document_id: record.documentId, from_origin: record.serviceOrigin, to_origin: endpoint.serviceOrigin, known_origins: knownOrigins,
      ...(record.schema === 'reqpilot-pair-launch-record/v3' ? { workflow: record.workflow,
        preparation_identity: record.prepared?.preparation_identity,
        preparation_request: record.prepared?.preparation_request } : {}) };
    await writeFile(file, JSON.stringify(payload), { mode: 0o600 });
    let receipt;
    try {
      receipt = JSON.parse(await this.runtime.command([record.python, '-I', fileURLToPath(new URL('./task-reconnect.py', import.meta.url)), file],
        { PYTHONPATH: undefined, PYTHONHOME: undefined, REQPILOT_DEVELOPMENT_ROOT: undefined }, { signal: AbortSignal.timeout(60000) }));
    } catch (error) {
      const code = /TASK_(?:RECONNECT_[A-Z_]+|MUTATION_OUTCOME_UNKNOWN)/.exec(String(error.message))?.[0];
      throw new TaskLaunchError(code || 'TASK_RECONNECT_NOT_CONFIRMED');
    }
    if (receipt.schema !== 'reqpilot-pair-reconnect/v1' || receipt.workspace !== workspace
      || receipt.task_id !== record.prepared.task_id || receipt.work_ref !== record.prepared.work_ref
      || receipt.source_sha256 !== record.sourceSha256 || receipt.bundle_sha256 !== record.bundleSha
      || receipt.service_origin !== endpoint.serviceOrigin || receipt.mutation_replayed !== false)
      throw new TaskLaunchError('TASK_RECONNECT_NOT_CONFIRMED');
    let after;
    try { after = await this.runtime.verifyEndpoint(this.transport); }
    catch { throw new TaskLaunchError('TASK_RECONNECT_ENDPOINT_UNTRUSTED'); }
    if (JSON.stringify(after) !== JSON.stringify(endpoint)) throw new TaskLaunchError('TASK_RECONNECT_ENDPOINT_UNTRUSTED');
    const finalInspection = await this.services().sessions.inspect(record.sessionId);
    const finalObserved = sessionObservation(finalInspection);
    validateObservation(finalObserved);
    if (finalObserved.fingerprint !== sessionFingerprint
      || this.services().liveStatus?.(record.sessionId) === 'running')
      throw new TaskLaunchError('TASK_RECONNECT_SESSION_CHANGED');
    const prior = record.serviceOrigin;
    record.serviceOrigin = endpoint.serviceOrigin; record.serviceInstanceId = endpoint.instanceId;
    record.reconnectEvidence = { fromOrigin: prior, instanceId: endpoint.instanceId, ownerId: endpoint.ownerId,
      taskId: record.prepared.task_id, workRef: record.prepared.work_ref, sessionId: record.sessionId, workspaceId: record.workspaceId };
    delete record.reconnectIntent;
    await this.save(record);
  }
  async toolBinding(sessionId) {
    const files = await readdir(this.root).catch(error => { if (error.code === 'ENOENT') return []; throw error; });
    for (const file of files) {
      if (!COMMAND.test(file.replace(/\.json$/, '')) || !file.endsWith('.json')) continue;
      const record = JSON.parse(await readFile(join(this.root, file), 'utf8'));
      if (record.sessionId !== sessionId || record.toolTransport !== 'MCP') continue;
      if (!supportedRecord(record) || !['START_REQUESTED', 'ACCEPTED'].includes(record.phase)) throw new TaskLaunchError('TASK_TOOL_BINDING_NOT_READY');
      if (!this.endpointCurrent(record)) throw new TaskLaunchError('TASK_ENDPOINT_RECONNECT_REQUIRED');
      const workspace = record.prepared?.workspace ? resolve(record.prepared.workspace) : resolve(this.runtime.dataRoot, record.workspaceRelative), tools = record.prepared?.tools;
      const expected = [join(workspace, '.reqpilot/scripts/mcp_server.py'), '--working-root', workspace, '--source-root', workspace];
      if (tools?.schema !== 'reqpilot-host-tools/v1' || tools.mcp?.command !== record.python || tools.mcp?.cwd !== workspace
          || JSON.stringify(tools.mcp?.args) !== JSON.stringify(expected)) throw new TaskLaunchError('TASK_MCP_DESCRIPTOR_INVALID');
      if (this.knowledge) this.knowledge.registerRecord(record);
      return tools.mcp;
    }
    return null;
  }
  async toolBindings(sessionId) {
    const files = await readdir(this.root).catch(error => { if (error.code === 'ENOENT') return []; throw error; });
    for (const file of files) {
      if (!COMMAND.test(file.replace(/\.json$/, '')) || !file.endsWith('.json')) continue;
      const record = JSON.parse(await readFile(join(this.root, file), 'utf8'));
      if (record.sessionId !== sessionId || record.toolTransport !== 'MCP') continue;
      if (!supportedRecord(record) || !['START_REQUESTED', 'ACCEPTED'].includes(record.phase)) throw new TaskLaunchError('TASK_TOOL_BINDING_NOT_READY');
      if (!this.endpointCurrent(record)) throw new TaskLaunchError('TASK_ENDPOINT_RECONNECT_REQUIRED');
      const workspace = record.prepared?.workspace ? resolve(record.prepared.workspace) : resolve(this.runtime.dataRoot, record.workspaceRelative), tools = record.prepared?.tools;
      const expected = [join(workspace, '.reqpilot/scripts/mcp_server.py'), '--working-root', workspace, '--source-root', workspace];
      if (tools?.schema !== 'reqpilot-host-tools/v1' || tools.mcp?.command !== record.python || tools.mcp?.cwd !== workspace
          || JSON.stringify(tools.mcp?.args) !== JSON.stringify(expected)) throw new TaskLaunchError('TASK_MCP_DESCRIPTOR_INVALID');
      if (this.knowledge) this.knowledge.registerRecord(record);
      const bindings = [tools.mcp];
      if (record.knowledge && this.knowledge?.available()) bindings.push(await this.knowledge.mcpBinding(record.commandId, record));
      return bindings;
    }
    return [];
  }
  async recoverAcceptance(record) {
    // A lost acknowledgement can be confirmed from the existing Session without sending another prompt.
    // This confirms admission only; it neither resumes the business task nor changes its endpoint binding.
    const sessions = this.services().sessions;
    if (record.phase !== 'START_REQUESTED' || typeof sessions.inspect !== 'function') return false;
    let inspection;
    try { inspection = await sessions.inspect(record.sessionId); } catch { return false; }
    const meta = sessionMeta(inspection);
    if (meta) {
      const workspace = record.prepared?.workspace
        ? resolve(record.prepared.workspace)
        : resolve(this.runtime.dataRoot, record.workspaceRelative);
      if ((meta.sessionId !== undefined && meta.sessionId !== record.sessionId)
        || (meta.id !== undefined && meta.id !== record.sessionId)
        || typeof meta.cwd !== 'string' || resolve(meta.cwd) !== workspace
        || (record.workspaceId && meta.workspaceId !== undefined && meta.workspaceId !== record.workspaceId))
        throw new TaskLaunchError('TASK_SESSION_WORKSPACE_MISMATCH');
    }
    const event = Array.isArray(inspection?.events) ? inspection.events.find(value => value.type === 'user/message'
      && value.data?.source?.kind === 'user' && value.data.source.rpcId === record.requestId) : null;
    if (!event) return false;
    record.phase = 'ACCEPTED'; record.acceptanceEvidence = { kind: 'SESSION_LOG', seq: event.seq ?? null };
    await this.save(record); return true;
  }
  async run(input, fingerprint) {
    if (!this.available()) throw new TaskLaunchError('TASK_LAUNCH_NOT_READY', 503);
    const applicationId = discovery.publicConfiguration({ ...this.runtime.snapshot, ownerId: this.runtime.ownerId }).applicationId;
    const origin = this.runtime.snapshot.serviceOrigin;
    let record;
    try { record = JSON.parse(await readFile(join(this.root, input.commandId + '.json'), 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    const replay = Boolean(record);
    if (record) {
      if (!supportedRecord(record)) throw new TaskLaunchError('UNSUPPORTED_LAUNCH_RECORD');
      if (record.fingerprint !== fingerprint || record.applicationId !== applicationId) throw new TaskLaunchError('COMMAND_CONFLICT');
      if (record.prepared && (record.prepared.source_sha256 !== record.sourceSha256
        || record.prepared.bundle_sha256 !== record.bundleSha)) throw new TaskLaunchError('TASK_PREPARED_SOURCE_MISMATCH');
      if (record.phase === 'ACCEPTED') return this.public(record);
      // An exact durable user request closes an ambiguous prompt acknowledgement
      // without another dispatch, even when the local service origin changed.
      if (record.phase === 'START_REQUESTED' && await this.recoverAcceptance(record)) return this.public(record);
      if (!this.endpointCurrent(record)) throw new TaskLaunchError('TASK_ENDPOINT_RECONNECT_REQUIRED');
    } else {
      const issue = this.startIssue();
      if (issue) throw new TaskLaunchError(issue, 503);
      const target = this.currentModel();
      if (!target) throw new TaskLaunchError('PAIR_DEFAULT_MODEL_UNAVAILABLE', 503);
      record = { schema: 'reqpilot-pair-launch-record/v3', workflow: this.workflow, modelSelection: target,
        ...(this.agentModel === undefined ? {} : { agentModel: { ...this.agentModel } }),
        agentPreset: this.config.agentPreset || 'standard', toolTransport: this.config.taskMcp === true ? 'MCP' : 'CLI', ...input, fingerprint, applicationId, serviceOrigin: origin, serviceInstanceId: this.runtime.snapshot.instanceId || null, commandId: input.commandId,
        sessionId: 'session-' + randomUUID(), requestId: 'reqpilotx-' + input.commandId, phase: 'RECORDED',
        bundleSha: this.bundleSha, capabilityRoot: this.capabilityRoot, python: this.runtime.python,
        workspaceRelative: 'task-workspaces/' + input.commandId };
      await this.save(record);
    }
    if ((record.schema === 'reqpilot-pair-launch-record/v3' || record.schema === 'reqpilot-pair-launch-record/v2') && !record.selectedAgentModel
      && typeof this.services().sessions.selectSessionModel !== 'function') throw new TaskLaunchError('PAIR_SESSION_MODEL_SELECTION_UNSUPPORTED', 503);
    let workspace = resolve(this.runtime.dataRoot, record.workspaceRelative);
    if (!record.prepared) {
      if (!this.knowledge?.available() && (input.knowledge?.mode && input.knowledge.mode !== 'NONE')) throw new TaskLaunchError('KNOWLEDGE_SERVICE_UNAVAILABLE', 503);
      if (input.adoptedKnowledge && !this.knowledge?.adoptionAvailable?.()) throw new TaskLaunchError('ADOPTED_KNOWLEDGE_UNAVAILABLE', 503);
      if (this.knowledge?.available()) {
        const binding = input.adoptedKnowledge
          ? await this.knowledge.prepareAdoptedBinding({ commandId: input.commandId, projectId: input.projectId,
            libraryId: input.adoptedKnowledge.libraryId, versionId: input.adoptedKnowledge.versionId })
          : await this.knowledge.prepareBinding({
            commandId: input.commandId, projectId: input.projectId,
            mode: input.knowledge?.mode || 'NONE',
            projectDocumentIds: input.knowledge?.projectDocumentIds || [],
            team: input.knowledge?.team || null });
        record.knowledge = { mode: binding.mode, bindingSha256: binding.binding_sha256,
          bindingFile: this.knowledge.bindingPath(input.commandId) };
        record.knowledgeBinding = binding;
        record.knowledgeSelection = input.adoptedKnowledge
          ? { adoptedKnowledge: { ...input.adoptedKnowledge } }
          : input.knowledge ? { ...input.knowledge } : { mode: binding.mode };
        await this.save(record);
      }
      const documents = await this.transport(origin + '/api/projects/' + input.projectId + '/sor-documents', { redirect: 'error' });
      if (!documents.ok) throw new TaskLaunchError('DOCUMENT_LOOKUP_FAILED', 502);
      const document = (await documents.json()).find(d => d.id === input.documentId);
      if (!document || !/^[a-f0-9]{64}$/.test(document.source_sha256)) throw new TaskLaunchError('SOURCE_NOT_FOUND', 404);
      if (record.sourceSha256 && record.sourceSha256 !== document.source_sha256) throw new TaskLaunchError('SOURCE_CHANGED');
      const response = await this.transport(origin + '/api/projects/' + input.projectId + '/sor-documents/' + input.documentId + '/file', { redirect: 'error' });
      if (!response.ok) throw new TaskLaunchError('SOURCE_READ_FAILED', 502);
      const chunks = []; let size = 0;
      for await (const bytes of response.body) { size += bytes.length; if (size > 128 * 1024 * 1024) throw new TaskLaunchError('SOURCE_TOO_LARGE', 413); chunks.push(bytes); }
      const original = Buffer.concat(chunks); if (sha(original) !== document.source_sha256) throw new TaskLaunchError('SOURCE_CHANGED');
      record.sourceSha256 = document.source_sha256;
      record.documentName = String(document.file_name || '').trim().slice(0, 160) || undefined;
      await this.save(record);
      const inputRoot = join(this.root, input.commandId + '-input'); await mkdir(inputRoot, { recursive: true, mode: 0o700 });
      const source = join(inputRoot, basename(document.file_name || 'source.pdf')); await writeFile(source, original, { mode: 0o600 });
      const helper = await stat(join(workspace, '.reqpilot/scripts/host_launch.py')).then(() => join(workspace, '.reqpilot/scripts/host_launch.py')).catch(error => { if (error.code === 'ENOENT') return join(record.capabilityRoot, 'scripts/host_launch.py'); throw error; });
      const file = join(inputRoot, 'prepare.json');
      const workbench = new URL('/workspace', origin); workbench.searchParams.set('projectId', input.projectId); workbench.searchParams.set('documentId', input.documentId);
      await writeFile(file, JSON.stringify({ workspace, expected_source_sha256: record.sourceSha256,
        bundle_sha256: record.bundleSha, application_id: applicationId,
        ...(record.knowledgeBinding ? { knowledge_evidence: record.knowledgeBinding,
          evidence_mode: record.knowledgeBinding.mode,
          evidence_reason: record.knowledgeBinding.mode === 'NONE' ? 'NO_INTERNAL_EVIDENCE' : 'HOST_SELECTED_TASK_EVIDENCE' } : {}),
        request: { mode: 'create', source,
          ...(record.schema === 'reqpilot-pair-launch-record/v3' ? { supplementary_instructions: input.goal } : { goal: input.goal }),
          creation_key: input.commandId,
          project_key: 'project-' + input.projectId, document_key: 'document-' + input.documentId,
          workbench_url: workbench.href, require_map_confirmation: input.requireMapConfirmation || false } }), { mode: 0o600 });
      const result = JSON.parse(await this.runtime.command([record.python, helper, file], { PYTHONPATH: undefined, PYTHONHOME: undefined, REQPILOT_DEVELOPMENT_ROOT: undefined }));
      const sameWorkspace = await realpath(workspace).then(value => value === resolve(result.workspace), () => false);
      if (result.schema !== 'reqpilot-host-launch/v1' || result.source_sha256 !== record.sourceSha256 || result.bundle_sha256 !== record.bundleSha
        || typeof result.task_id !== 'string' || !result.task_id || typeof result.prompt !== 'string' || !result.prompt.trim()
        || (!sameWorkspace && resolve(result.workspace) !== workspace) || !/^plugin-[a-f0-9]{32}$/.test(result.work_ref || '')) throw new TaskLaunchError('PREPARATION_NOT_CONFIRMED');
      workspace = await realpath(workspace);
      if (record.schema === 'reqpilot-pair-launch-record/v3') {
        const workflowState = result.workflow;
        if (!validWorkflowState(workflowState) || workflowState.source_sha256 !== record.sourceSha256
          || workflowState.workflow?.id !== record.workflow.id || workflowState.workflow?.version !== record.workflow.version
          || workflowState.workflow?.definition_sha256 !== record.workflow.definition_sha256
          || workflowState.human_acceptance !== 'NOT_CLAIMED')
          throw new TaskLaunchError('FIXED_WORKFLOW_NOT_CONFIRMED');
        record.workflow = { ...record.workflow, brief_sha256: workflowState.workflow.brief_sha256,
          evidence: { mode: workflowState.workflow.evidence_mode,
            reason: workflowState.workflow.evidence_mode === 'NONE' ? 'NO_INTERNAL_EVIDENCE' : 'HOST_SELECTED_TASK_EVIDENCE' } };
      }
      record.prepared = result; record.phase = 'TASK_PREPARED';
      if (this.knowledge) this.knowledge.registerRecord(record);
      await this.save(record);
      if (record.knowledgeBinding && record.knowledgeBinding.mode !== 'NONE' && typeof this.knowledge?.markStatus === 'function') {
        if (await this.knowledge.markStatus(record.commandId, 'CURRENT', '') === true) {
          record.knowledgeStatus = { state: 'CURRENT', message: '' };
          await this.save(record);
        }
      }
    }
    this.assertActive(record);
    const services = this.services();
    if (!record.workspaceId) {
      const created = await services.workspaces.create({ path: workspace });
      if (!created.workspace?.workspaceId) throw new TaskLaunchError('PAIR_WORKSPACE_NOT_CONFIRMED');
      record.workspaceId = created.workspace.workspaceId;
      // New native workspaces get a readable registry title. Existing task
      // workspaces keep their path and identity so retries are unchanged.
      if (record.documentName && typeof services.workspaces.rename === 'function') {
        const title = ('ReqPilotX · ' + record.documentName + ' · ' + record.commandId.slice(0, 8)).slice(0, 180);
        await services.workspaces.rename({ workspaceId: record.workspaceId, title });
      }
      await this.save(record);
    }
    // Resolve a retried record against the real Session. The new Host create
    // operation only reuses a named *blank* Session; calling it for a started
    // Session would mint a different identity and duplicate the work.
    this.assertActive(record);
    let restored = false;
    if (replay) {
      if (typeof services.sessions.inspect !== 'function') throw new TaskLaunchError('PAIR_SESSION_INSPECT_UNAVAILABLE', 503);
      let inspection;
      try { inspection = await services.sessions.inspect(record.sessionId); }
      catch (error) {
        if (!sessionNotFound(error)) throw new TaskLaunchError('PAIR_SESSION_INSPECT_FAILED', 503, error?.message);
        inspection = null;
      }
      if (inspection) {
        const meta = sessionMeta(inspection), events = sessionEvents(inspection);
        if (!meta || (meta.sessionId !== undefined && meta.sessionId !== record.sessionId)
          || (meta.id !== undefined && meta.id !== record.sessionId)) throw new TaskLaunchError('PAIR_SESSION_IDENTITY_MISMATCH');
        if (typeof meta.cwd !== 'string' || resolve(meta.cwd) !== workspace) throw new TaskLaunchError('TASK_SESSION_WORKSPACE_MISMATCH');
        if (record.workspaceId && meta.workspaceId !== undefined && meta.workspaceId !== record.workspaceId)
          throw new TaskLaunchError('TASK_SESSION_WORKSPACE_MISMATCH');
        const header = events.findLast(event => event?.type === 'request/header')?.data?.header?.config;
        if (header && record.selectedAgentModel && (header.provider !== record.selectedAgentModel.provider
          || header.model !== record.selectedAgentModel.model
          || (header.reasoningEffort !== undefined && record.selectedAgentModel.reasoningEffort !== undefined
            && header.reasoningEffort !== record.selectedAgentModel.reasoningEffort)))
          throw new TaskLaunchError('PAIR_SESSION_MODEL_CONFLICT');
        const requestEvent = events.find(event => event?.type === 'user/message'
          && event.data?.source?.kind === 'user' && event.data.source.rpcId === record.requestId);
        if (requestEvent) {
          record.phase = 'ACCEPTED';
          record.acceptanceEvidence = { kind: 'SESSION_LOG', seq: requestEvent.seq ?? null };
          await this.save(record); return this.public(record);
        }
        const { started, busy } = sessionTurnState(events);
        if (started) {
          if (!record.prepared) throw new TaskLaunchError('PAIR_SESSION_EXISTING_UNBOUND');
          if (busy) throw new TaskLaunchError('PAIR_SESSION_BUSY', 409);
          if (!record.selectedAgentModel) throw new TaskLaunchError('PAIR_SESSION_EXISTING_MODEL_UNCONFIRMED', 409);
          restored = true;
        }
      }
    }
    if (!restored) {
      // Explicit identity is safe here: the name is absent, or it is a blank
      // reusable Session that the Host returns under the same identity.
      const session = await services.sessions.create({ sessionId: record.sessionId, workspaceId: record.workspaceId, agentPreset: record.agentPreset });
      if (session.sessionId !== record.sessionId) throw new TaskLaunchError('PAIR_SESSION_IDENTITY_MISMATCH');
    }
    // A saved `selectedAgentModel` proves a selection for the old Agent
    // instance, not for an Agent recreated after restart/ENOSPC. Whenever this
    // run had to call create, re-install the exact frozen selection on that
    // live instance before any prompt. A started Session is restored instead
    // and must already carry the frozen request header (checked above).
    const createdInstance = !restored;
    if (record.schema === 'reqpilot-pair-launch-record/v2' && (createdInstance || !record.selectedAgentModel)) {
      if (typeof services.sessions.selectSessionModel !== 'function') throw new TaskLaunchError('PAIR_SESSION_MODEL_SELECTION_UNSUPPORTED', 503);
      this.assertActive(record);
      const result = await services.sessions.selectSessionModel({ sessionId: record.sessionId, ...record.agentModel });
      if (!validModel(result?.selected) || !sameModel(result.selected, record.agentModel)) throw new TaskLaunchError('PAIR_TASK_MODEL_NOT_CONFIRMED');
      record.selectedAgentModel = selectedModel(result.selected); await this.save(record);
    }
    if (record.schema === 'reqpilot-pair-launch-record/v3' && (createdInstance || !record.selectedAgentModel)) {
      if (typeof services.sessions.selectSessionModel !== 'function') throw new TaskLaunchError('PAIR_SESSION_MODEL_SELECTION_UNSUPPORTED', 503);
      this.assertActive(record);
      let selected;
      try { selected = await services.sessions.selectSessionModel({ sessionId: record.sessionId, ...record.modelSelection }); }
      catch (error) { throw new TaskLaunchError('PAIR_SESSION_MODEL_UNAVAILABLE', 503, error?.message); }
      if (!validModel(selected?.selected) || !sameModel(selected.selected, record.modelSelection)) throw new TaskLaunchError('PAIR_TASK_MODEL_NOT_CONFIRMED');
      record.selectedAgentModel = selectedModel(selected.selected); await this.save(record);
    }
    let promptText;
    if (record.schema === 'reqpilot-pair-launch-record/v3') {
      if (!hasOwn(record, 'promptText')) {
        // Only a record that predates this field gets the legacy fallback. A
        // present value has already been validated by supportedRecord; it must
        // never be recomposed under an existing requestId.
        record.promptText = this.startPromptText(record, record.phase !== 'START_REQUESTED');
      }
      promptText = record.promptText;
    } else {
      promptText = this.startPromptText(record, false);
    }
    record.phase = 'START_REQUESTED'; await this.save(record);
    this.assertActive(record);
    const accepted = await services.sessions.prompt({ sessionId: record.sessionId, requestId: record.requestId,
      mode: 'queue', content: [{ type: 'text', text: promptText }] }, new AbortController().signal);
    if (accepted.accepted !== true) throw new TaskLaunchError('PAIR_START_NOT_CONFIRMED');
    record.phase = 'ACCEPTED';
    const live = await this.readWorkflow(record);
    if (live.state) { record.prepared.workflow = live.state; delete record.workflowReadError; }
    else { record.prepared.workflow = null; record.workflowReadError = live.error; }
    await this.save(record); return this.public(record);
  }
}
