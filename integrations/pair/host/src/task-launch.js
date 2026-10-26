/** Persist only host receipts/bindings. Source semantics, preparation and task identity come from the fixed shared package. */
import { readFile, writeFile, mkdir, rename, stat, realpath, readdir } from 'node:fs/promises';
import { resolve, join, basename, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash, randomUUID } from 'node:crypto';
import discovery from './application-discovery.cjs';
import { executionObservation } from './execution-observation.js';
const sha = value => createHash('sha256').update(value).digest('hex');
const ID = /^[a-zA-Z0-9_-]{1,100}$/;
const COMMAND = /^[a-f0-9-]{36}$/;
export class TaskLaunchError extends Error { constructor(code, status = 409) { super(code); this.status = status; } }
const validModel = value => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).every(key => ['provider', 'model', 'reasoningEffort'].includes(key))
  && ['provider', 'model'].every(key => typeof value[key] === 'string' && value[key].trim())
  && (value.reasoningEffort === undefined || (typeof value.reasoningEffort === 'string' && value.reasoningEffort.trim()));
const sameModel = (a, b) => a?.provider === b?.provider && a?.model === b?.model && a?.reasoningEffort === b?.reasoningEffort;
const supportedRecord = record => record?.schema === 'reqpilot-pair-launch-record/v1'
  || (record?.schema === 'reqpilot-pair-launch-record/v2' && validModel(record.agentModel)
    && (record.selectedAgentModel === undefined || (validModel(record.selectedAgentModel) && sameModel(record.agentModel, record.selectedAgentModel)))
    && (!['START_REQUESTED', 'ACCEPTED'].includes(record.phase) || sameModel(record.agentModel, record.selectedAgentModel)));
export class TaskLauncher {
  constructor(runtime, config, getServices, transport = fetch) {
    if (config.agentModel !== undefined && !validModel(config.agentModel)) throw new TaskLaunchError('INVALID_AGENT_MODEL', 422);
    this.agentModel = config.agentModel === undefined ? undefined : { ...config.agentModel };
    this.runtime = runtime; this.config = config; this.getServices = getServices; this.transport = transport;
    this.ready = false; this.closing = false; this.inflight = new Map(); this.controlInflight = new Map(); this.root = join(runtime.dataRoot, 'task-launches');
  }
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
    if (this.config.taskMcp === true) {
      await stat(join(this.capabilityRoot, 'scripts/mcp_server.py'));
      const protocol = JSON.parse(await this.runtime.command([this.runtime.python, join(this.capabilityRoot, 'scripts/host_launch.py'), '--describe'], { PYTHONPATH: undefined, PYTHONHOME: undefined }));
      if (protocol.tools_schema !== 'reqpilot-host-tools/v1') throw new TaskLaunchError('CAPABILITY_MCP_PROTOCOL_UNSUPPORTED');
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
    return { schema: 'reqpilot-pair-launch/v1', commandId: record.commandId, applicationId: record.applicationId,
      projectId: record.projectId, documentId: record.documentId, sourceSha256: record.sourceSha256,
      taskId: record.prepared?.task_id, workRef: record.prepared?.work_ref,
      sessionId: record.sessionId, workspaceId: record.workspaceId,
      capabilityVersion: record.bundleSha, status: record.phase, semanticAcceptance: 'NOT_CLAIMED',
      ...(record.schema === 'reqpilot-pair-launch-record/v2' ? { agentModel: record.agentModel, selectedAgentModel: record.selectedAgentModel } : {}),
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
    const services = this.services();
    if (record.phase === 'ACCEPTED' && typeof services.sessions.inspect === 'function') {
      try {
        const inspection = await services.sessions.inspect(record.sessionId);
        result.execution = executionObservation(record, inspection.events || [], services.liveStatus?.(record.sessionId));
      } catch { result.execution = null; }
    }
    return result;
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
        documentId: record.documentId, sourceSha256: record.sourceSha256, goal: record.goal, requireMapConfirmation: record.requireMapConfirmation || false } });
    }
    return { tasks: matches };
  }
  start(input) {
    const allowed = ['commandId', 'projectId', 'documentId', 'sourceSha256', 'goal', 'requireMapConfirmation'];
    if (!input || Object.keys(input).some(k => !allowed.includes(k)) || !COMMAND.test(input.commandId || '')
      || !/^[a-f0-9]{64}$/.test(input.sourceSha256 || '') || !ID.test(input.projectId || '') || !ID.test(input.documentId || '') || typeof input.goal !== 'string'
      || !input.goal.trim() || input.goal.length > 12000 || (input.requireMapConfirmation !== undefined && typeof input.requireMapConfirmation !== 'boolean')) throw new TaskLaunchError('INVALID_TASK_REQUEST', 422);
    const fingerprint = sha(JSON.stringify([input.projectId, input.documentId, input.sourceSha256, input.goal, input.requireMapConfirmation || false]));
    const running = this.inflight.get(input.commandId);
    if (running) { if (running.fingerprint !== fingerprint) throw new TaskLaunchError('COMMAND_CONFLICT'); return running.promise; }
    const promise = this.run(input, fingerprint).finally(() => this.inflight.delete(input.commandId));
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
        control.dispatchAttempted = true; await this.save(record);
        const accepted = await sessions.prompt({ sessionId: record.sessionId, requestId: control.requestId,
          mode: 'queue', content: [{ type: 'text', text: `继续当前已绑定的 ReqPilotX 任务 ${record.prepared.task_id}。先用当前任务的 mcp__reqpilot__task_open（mode=resume）读取持久化任务与人工反馈，再核对来源和已保存版本，只修订受影响范围。不要创建新任务；完成后回报实际保存的版本和未解决问题。` }] }, new AbortController().signal);
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
    const workspace = resolve(this.runtime.dataRoot, 'task-workspaces', record.commandId);
    if (record.workspaceRelative !== 'task-workspaces/' + record.commandId || record.prepared?.workspace !== workspace
      || record.prepared?.bundle_sha256 !== record.bundleSha || record.prepared?.source_sha256 !== record.sourceSha256)
      throw new TaskLaunchError('TASK_RECONNECT_WORKSPACE_MISMATCH');
    const inspection = await this.services().sessions.inspect(record.sessionId);
    // This adapter contract requires an explicit Session/workspace identity. Missing metadata fails closed.
    if (inspection?.session?.sessionId !== record.sessionId || inspection.session.workspaceId !== record.workspaceId)
      throw new TaskLaunchError('TASK_RECONNECT_SESSION_MISMATCH');
    const sessionFingerprint = sha(JSON.stringify([inspection.session, inspection.events]));
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
      application_id: record.applicationId, command_id: record.commandId, goal: record.goal,
      require_map_confirmation: record.requireMapConfirmation || false, project_id: record.projectId,
      document_id: record.documentId, from_origin: record.serviceOrigin, to_origin: endpoint.serviceOrigin, known_origins: knownOrigins };
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
    if (sha(JSON.stringify([finalInspection?.session, finalInspection?.events])) !== sessionFingerprint
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
      const workspace = resolve(this.runtime.dataRoot, record.workspaceRelative), tools = record.prepared?.tools;
      const expected = [join(workspace, '.reqpilot/scripts/mcp_server.py'), '--working-root', workspace, '--source-root', workspace];
      if (tools?.schema !== 'reqpilot-host-tools/v1' || tools.mcp?.command !== record.python || tools.mcp?.cwd !== workspace
          || JSON.stringify(tools.mcp?.args) !== JSON.stringify(expected)) throw new TaskLaunchError('TASK_MCP_DESCRIPTOR_INVALID');
      return tools.mcp;
    }
    return null;
  }
  async recoverAcceptance(record) {
    // A lost acknowledgement can be confirmed from the existing Session without sending another prompt.
    // This confirms admission only; it neither resumes the business task nor changes its endpoint binding.
    const sessions = this.services().sessions;
    if (record.phase !== 'START_REQUESTED' || typeof sessions.inspect !== 'function') return false;
    let inspection;
    try { inspection = await sessions.inspect(record.sessionId); } catch { return false; }
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
    if (record) {
      if (!supportedRecord(record)) throw new TaskLaunchError('UNSUPPORTED_LAUNCH_RECORD');
      if (record.fingerprint !== fingerprint || record.applicationId !== applicationId) throw new TaskLaunchError('COMMAND_CONFLICT');
      if (record.phase === 'ACCEPTED' || await this.recoverAcceptance(record)) return this.public(record);
      if (!this.endpointCurrent(record)) throw new TaskLaunchError('TASK_ENDPOINT_RECONNECT_REQUIRED');
    } else {
      record = { schema: this.agentModel === undefined ? 'reqpilot-pair-launch-record/v1' : 'reqpilot-pair-launch-record/v2',
        ...(this.agentModel === undefined ? {} : { agentModel: { ...this.agentModel } }),
        agentPreset: this.config.agentPreset || 'standard', toolTransport: this.config.taskMcp === true ? 'MCP' : 'CLI', ...input, fingerprint, applicationId, serviceOrigin: origin, serviceInstanceId: this.runtime.snapshot.instanceId || null, commandId: input.commandId,
        sessionId: 'session-' + randomUUID(), requestId: 'reqpilotx-' + input.commandId, phase: 'RECORDED',
        bundleSha: this.bundleSha, capabilityRoot: this.capabilityRoot, python: this.runtime.python,
        workspaceRelative: 'task-workspaces/' + input.commandId };
      await this.save(record);
    }
    if (record.schema === 'reqpilot-pair-launch-record/v2' && !record.selectedAgentModel
      && typeof this.services().sessions.selectSessionModel !== 'function') throw new TaskLaunchError('PAIR_SESSION_MODEL_SELECTION_UNSUPPORTED', 503);
    const workspace = resolve(this.runtime.dataRoot, record.workspaceRelative);
    if (!record.prepared) {
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
      record.sourceSha256 = document.source_sha256; await this.save(record);
      const inputRoot = join(this.root, input.commandId + '-input'); await mkdir(inputRoot, { recursive: true, mode: 0o700 });
      const source = join(inputRoot, basename(document.file_name || 'source.pdf')); await writeFile(source, original, { mode: 0o600 });
      const helper = await stat(join(workspace, '.reqpilot/scripts/host_launch.py')).then(() => join(workspace, '.reqpilot/scripts/host_launch.py')).catch(error => { if (error.code === 'ENOENT') return join(record.capabilityRoot, 'scripts/host_launch.py'); throw error; });
      const file = join(inputRoot, 'prepare.json');
      const workbench = new URL('/workspace', origin); workbench.searchParams.set('projectId', input.projectId); workbench.searchParams.set('documentId', input.documentId);
      await writeFile(file, JSON.stringify({ workspace, expected_source_sha256: record.sourceSha256,
        bundle_sha256: record.bundleSha, application_id: applicationId,
        request: { mode: 'create', source, goal: input.goal, creation_key: input.commandId,
          project_key: 'project-' + input.projectId, document_key: 'document-' + input.documentId,
          workbench_url: workbench.href, require_map_confirmation: input.requireMapConfirmation || false } }), { mode: 0o600 });
      const result = JSON.parse(await this.runtime.command([record.python, helper, file], { PYTHONPATH: undefined, PYTHONHOME: undefined, REQPILOT_DEVELOPMENT_ROOT: undefined }));
      if (result.schema !== 'reqpilot-host-launch/v1' || result.source_sha256 !== record.sourceSha256 || result.bundle_sha256 !== record.bundleSha
        || typeof result.task_id !== 'string' || !result.task_id || typeof result.prompt !== 'string' || !result.prompt.trim() || resolve(result.workspace) !== workspace || !/^plugin-[a-f0-9]{32}$/.test(result.work_ref || '')) throw new TaskLaunchError('PREPARATION_NOT_CONFIRMED');
      record.prepared = result; record.phase = 'TASK_PREPARED'; await this.save(record);
    }
    this.assertActive(record);
    const services = this.services();
    if (!record.workspaceId) {
      const created = await services.workspaces.create({ path: workspace });
      if (!created.workspace?.workspaceId) throw new TaskLaunchError('PAIR_WORKSPACE_NOT_CONFIRMED');
      record.workspaceId = created.workspace.workspaceId; await this.save(record);
    }
    // Explicit identities let DSH adopt the same Session and deduplicate ambiguous prompt responses.
    this.assertActive(record);
    const session = await services.sessions.create({ sessionId: record.sessionId, workspaceId: record.workspaceId, agentPreset: record.agentPreset });
    if (session.sessionId !== record.sessionId) throw new TaskLaunchError('PAIR_SESSION_IDENTITY_MISMATCH');
    if (record.schema === 'reqpilot-pair-launch-record/v2' && !record.selectedAgentModel) {
      this.assertActive(record);
      const result = await services.sessions.selectSessionModel({ sessionId: record.sessionId, ...record.agentModel });
      if (!validModel(result?.selected) || !sameModel(result.selected, record.agentModel)) throw new TaskLaunchError('PAIR_TASK_MODEL_NOT_CONFIRMED');
      record.selectedAgentModel = { ...result.selected }; await this.save(record);
    }
    record.phase = 'START_REQUESTED'; await this.save(record);
    this.assertActive(record);
    const accepted = await services.sessions.prompt({ sessionId: record.sessionId, requestId: record.requestId,
      mode: 'queue', content: [{ type: 'text', text: record.prepared.prompt + '\n宿主展示入口是 Pair 的 ReqPilotX 应用面板；业务URL是工具传输地址，不要求用户搬运链接。' }] }, new AbortController().signal);
    if (accepted.accepted !== true) throw new TaskLaunchError('PAIR_START_NOT_CONFIRMED');
    record.phase = 'ACCEPTED'; await this.save(record); return this.public(record);
  }
}
