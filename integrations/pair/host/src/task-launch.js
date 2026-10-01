/** Persist only host receipts/bindings. Source semantics, preparation and task identity come from the fixed shared package. */
import { readFile, writeFile, mkdir, rename, stat, realpath, readdir } from 'node:fs/promises';
import { resolve, join, basename, isAbsolute } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import discovery from './application-discovery.cjs';
import { executionObservation } from './execution-observation.js';
const sha = value => createHash('sha256').update(value).digest('hex');
const ID = /^[a-zA-Z0-9_-]{1,100}$/;
const COMMAND = /^[a-f0-9-]{36}$/;
export class TaskLaunchError extends Error { constructor(code, status = 409) { super(code); this.status = status; } }
export class TaskLauncher {
  constructor(runtime, config, getServices, transport = fetch) {
    this.runtime = runtime; this.config = config; this.getServices = getServices; this.transport = transport;
    this.ready = false; this.closing = false; this.inflight = new Map(); this.controlInflight = new Map(); this.root = join(runtime.dataRoot, 'task-launches');
  }
  services() {
    const services = this.getServices();
    if (!services?.sessions?.create || !services.sessions.prompt || !services.workspaces?.create) throw new TaskLaunchError('PAIR_SESSION_SERVICE_UNAVAILABLE', 503);
    return services;
  }
  available() { try { this.services(); return !this.closing && this.ready && this.runtime.snapshot.status === 'READY'; } catch { return false; } }
  canCancel() { return this.available() && typeof this.services().sessions.cancel === 'function'; }
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
      controlCapabilities: { cancel: record.phase === 'ACCEPTED' && this.canCancel(),
        continue: record.phase === 'ACCEPTED' && record.toolTransport === 'MCP'
          && record.serviceOrigin === this.runtime.snapshot.serviceOrigin && this.canContinue() } };
  }
  assertActive(record) {
    if (!this.available() || this.runtime.snapshot.serviceOrigin !== record.serviceOrigin) throw new TaskLaunchError('TASK_LAUNCH_INTERRUPTED', 503);
  }
  async close() { this.closing = true; await Promise.allSettled([...this.inflight.values(), ...this.controlInflight.values()].map(value => value.promise)); }
  async inspect(commandId) {
    if (!COMMAND.test(commandId)) throw new TaskLaunchError('INVALID_COMMAND_ID', 422);
    const record = JSON.parse(await readFile(join(this.root, commandId + '.json'), 'utf8'));
    if (record.schema !== 'reqpilot-pair-launch-record/v1') throw new TaskLaunchError('UNSUPPORTED_LAUNCH_RECORD');
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
      || !COMMAND.test(input.controlId || '') || !['cancel', 'continue'].includes(input.action)
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
    if (record.schema !== 'reqpilot-pair-launch-record/v1' || record.phase !== 'ACCEPTED'
      || record.sessionId !== input.sessionId || record.prepared?.work_ref !== input.workRef
      || record.sourceSha256 !== input.sourceSha256) throw new TaskLaunchError('TASK_CONTROL_BINDING_MISMATCH');
    const applicationId = discovery.publicConfiguration({ ...this.runtime.snapshot, ownerId: this.runtime.ownerId }).applicationId;
    if (record.applicationId !== applicationId) throw new TaskLaunchError('TASK_CONTROL_OWNER_MISMATCH');
    record.controls ||= {};
    let control = record.controls[input.controlId];
    if (control && control.fingerprint !== fingerprint) throw new TaskLaunchError('CONTROL_CONFLICT');
    if (control?.status === 'ACCEPTED') return { ...this.public(record), control: { controlId: input.controlId, action: input.action, status: 'ACCEPTED' } };
    if (!control) {
      control = { action: input.action, fingerprint, status: 'REQUESTED', requestId: 'reqpilotx-control-' + input.controlId };
      record.controls[input.controlId] = control; await this.save(record);
    }
    const sessions = this.services().sessions;
    if (input.action === 'continue') {
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
        const accepted = await sessions.prompt({ sessionId: record.sessionId, requestId: control.requestId,
          mode: 'queue', content: [{ type: 'text', text: `继续当前已绑定的 ReqPilotX 任务 ${record.prepared.task_id}。先用当前任务的 mcp__reqpilot__task_open（mode=resume）读取持久化任务与人工反馈，再核对来源和已保存版本，只修订受影响范围。不要创建新任务；完成后回报实际保存的版本和未解决问题。` }] }, new AbortController().signal);
        if (accepted.accepted !== true) throw new TaskLaunchError('PAIR_CONTINUE_NOT_CONFIRMED');
        control.status = 'ACCEPTED'; await this.save(record);
      }
    } else {
      if (typeof sessions.cancel !== 'function') throw new TaskLaunchError('PAIR_CANCEL_UNAVAILABLE', 503);
      const accepted = await sessions.cancel({ sessionId: record.sessionId });
      if (accepted.accepted !== true) throw new TaskLaunchError('PAIR_CANCEL_NOT_CONFIRMED');
      control.status = 'ACCEPTED'; await this.save(record);
    }
    return { ...this.public(record), control: { controlId: input.controlId, action: input.action, status: 'ACCEPTED' } };
  }
  async toolBinding(sessionId) {
    const files = await readdir(this.root).catch(error => { if (error.code === 'ENOENT') return []; throw error; });
    for (const file of files) {
      if (!COMMAND.test(file.replace(/\.json$/, '')) || !file.endsWith('.json')) continue;
      const record = JSON.parse(await readFile(join(this.root, file), 'utf8'));
      if (record.sessionId !== sessionId || record.toolTransport !== 'MCP') continue;
      if (record.schema !== 'reqpilot-pair-launch-record/v1' || !['START_REQUESTED', 'ACCEPTED'].includes(record.phase)) throw new TaskLaunchError('TASK_TOOL_BINDING_NOT_READY');
      if (record.serviceOrigin !== this.runtime.snapshot.serviceOrigin) throw new TaskLaunchError('TASK_ENDPOINT_RECONNECT_REQUIRED');
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
      if (record.schema !== 'reqpilot-pair-launch-record/v1') throw new TaskLaunchError('UNSUPPORTED_LAUNCH_RECORD');
      if (record.fingerprint !== fingerprint || record.applicationId !== applicationId) throw new TaskLaunchError('COMMAND_CONFLICT');
      if (record.phase === 'ACCEPTED' || await this.recoverAcceptance(record)) return this.public(record);
      if (record.serviceOrigin !== origin) throw new TaskLaunchError('TASK_ENDPOINT_RECONNECT_REQUIRED');
    } else {
      record = { schema: 'reqpilot-pair-launch-record/v1', agentPreset: this.config.agentPreset || 'standard', toolTransport: this.config.taskMcp === true ? 'MCP' : 'CLI', ...input, fingerprint, applicationId, serviceOrigin: origin, commandId: input.commandId,
        sessionId: 'session-' + randomUUID(), requestId: 'reqpilotx-' + input.commandId, phase: 'RECORDED',
        bundleSha: this.bundleSha, capabilityRoot: this.capabilityRoot, python: this.runtime.python,
        workspaceRelative: 'task-workspaces/' + input.commandId };
      await this.save(record);
    }
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
    record.phase = 'START_REQUESTED'; await this.save(record);
    this.assertActive(record);
    const accepted = await services.sessions.prompt({ sessionId: record.sessionId, requestId: record.requestId,
      mode: 'queue', content: [{ type: 'text', text: record.prepared.prompt + '\n宿主展示入口是 Pair 的 ReqPilotX 应用面板；业务URL是工具传输地址，不要求用户搬运链接。' }] }, new AbortController().signal);
    if (accepted.accepted !== true) throw new TaskLaunchError('PAIR_START_NOT_CONFIRMED');
    record.phase = 'ACCEPTED'; await this.save(record); return this.public(record);
  }
}
