import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, mkdir, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash } from 'node:crypto';
import { TaskLauncher } from '../host/src/task-launch.js';
const commandId = '12345678-1234-4234-8234-123456789abc';
const defaultModel = { provider: 'opencode-go', model: 'deepseek-v4.1-flash', reasoningEffort: 'max' };
const workflow = { schema: 'reqpilot-fixed-workflow/v1', id: 'reqpilot-fixed-workflow', version: '1.0.0',
  definition_sha256: 'd'.repeat(64), label: 'Fixed workflow', stages: [] };
const workflowSpec = { schema: workflow.schema, id: workflow.id, version: workflow.version,
  definition_sha256: workflow.definition_sha256, evidence: { mode: 'NONE', reason: 'INTERNAL_EVIDENCE_NOT_WIRED' } };
const workflowState = hash => ({ schema: 'reqpilot-fixed-workflow-state/v1',
  workflow: { id: workflow.id, version: workflow.version, definition_sha256: workflow.definition_sha256,
    brief_sha256: 'f'.repeat(64), evidence_mode: 'NONE' }, source_sha256: hash,
  stages: [{ id: 'PREPARATION', label: '资料准备', status: 'SAVED' }], next_stage: 'SOURCE_MAP',
  ready_to_review: false, verification: 'SAVED_ARTIFACTS_ONLY', open_items: [], human_acceptance: 'NOT_CLAIMED' });
const taskWorkflowState = hash => ({ ...workflowState(hash), stages: [
  { id: 'PREPARATION', label: '资料准备', status: 'SAVED', evidence: [{ kind: 'TASK_CONTEXT', source_sha256: hash }] },
  { id: 'SOURCE_MAP', label: '来源与结构', status: 'PENDING' },
  { id: 'ITEMS_CLASSIFICATION', label: '条目、分类与明确派生', status: 'BLOCKED', reason: 'SOURCE_MAP_REQUIRED' },
  { id: 'EVIDENCE_ANALYSIS', label: '依据分析', status: 'BLOCKED', reason: 'ITEMS_REQUIRED' },
  { id: 'SELF_CHECK', label: '自查与修正', status: 'BLOCKED', reason: 'ITEMS_REQUIRED' },
  { id: 'DELIVERABLE', label: '可核对成果', status: 'BLOCKED', reason: 'ITEMS_REQUIRED' } ] });
async function fixture(config = {}) {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-launch-')); const source = Buffer.from('original'); const hash = createHash('sha256').update(source).digest('hex');
  const calls = { prepare: 0, create: [], prompt: [], workspace: [], model: [] };
  let taskWorkflow = taskWorkflowState(hash);
  const runtime = { dataRoot: root, python: '/installed/python', ownerId: commandId, snapshot: { status: 'READY', mode: 'managed', serviceOrigin: 'http://127.0.0.1:1234' }, async command(args) {
    if (args.some(value => value.endsWith('task-workflow.py'))) return JSON.stringify({ status: 'READY', task_id: 'task-one', workflow: taskWorkflow });
    calls.prepare++; const input = JSON.parse(await readFile(args[2], 'utf8')); await mkdir(input.workspace, { recursive: true });
    return JSON.stringify({ schema: 'reqpilot-host-launch/v1', workspace: input.workspace, task_id: 'task-one', work_ref: 'plugin-' + 'a'.repeat(32), source_sha256: hash, bundle_sha256: 'b'.repeat(64), prompt: 'Read request.md', workflow: workflowState(hash), tools: { schema: 'reqpilot-host-tools/v1', mcp: { command: runtime.python, args: [join(input.workspace, '.reqpilot/scripts/mcp_server.py'), '--working-root', input.workspace, '--source-root', input.workspace], cwd: input.workspace } } });
  } };
  const services = { defaultModel: { currentSelection: () => ({ ...defaultModel }) },
    workspaces: { async create(value) { calls.workspace.push(value); return { workspace: { workspaceId: 'workspace-one' } }; } },
    sessions: { async create(value) { calls.create.push(value); return { sessionId: value.sessionId }; },
      async selectSessionModel(value) { calls.model.push(value); const { sessionId, ...selected } = value; return { selected }; },
      async prompt(value) { calls.prompt.push(value); return { accepted: true }; } } };
  const transport = async url => url.endsWith('/file') ? new Response(source) : new Response(JSON.stringify([{ id: 'd', file_name: 'source.pdf', source_sha256: hash }]));
  const launcher = new TaskLauncher(runtime, config, () => services, transport); launcher.ready = true; launcher.capabilityRoot = root; launcher.bundleSha = 'b'.repeat(64);
  launcher.workflow = workflowSpec; launcher.workflowDescriptor = workflow;
  const input = { commandId, projectId: 'p', documentId: 'd', sourceSha256: hash, goal: 'User goal' };
  return { root, calls, runtime, services, launcher, input, get workflowState() { return taskWorkflow; }, set workflowState(value) { taskWorkflow = value; }, async close() { await launcher.close(); await rm(root, { recursive: true, force: true }); } };
}
const codexModel = { provider: 'openai-codex', model: 'gpt-6.1-sol', reasoningEffort: 'xhigh' };
test('native tasks bind the configured model before admission and retain it across acknowledgement retries', async () => {
  const f = await fixture({ agentModel: codexModel, taskMcp: true }); try {
    f.services.sessions.selectSessionModel = async request => {
      f.calls.model.push(request); assert.equal(f.calls.prompt.length, 0);
      const { sessionId, ...selected } = request; assert.equal(sessionId, f.calls.create[0].sessionId);
      return { selected };
    };
    f.services.sessions.prompt = async request => { f.calls.prompt.push(request); if (f.calls.prompt.length === 1) throw Error('ack lost'); return { accepted: true }; };
    await assert.rejects(f.launcher.start(f.input), /ack lost/);
    await f.launcher.close();
    const retry = new TaskLauncher(f.runtime, { agentModel: { ...codexModel, model: 'different-model' }, taskMcp: true }, () => f.services, f.launcher.transport);
    retry.ready = true; retry.capabilityRoot = f.root; retry.bundleSha = 'b'.repeat(64);
    try {
      const receipt = await retry.start(f.input);
      assert.deepEqual(receipt.agentModel, codexModel); assert.deepEqual(receipt.selectedAgentModel, codexModel);
      assert.equal(f.calls.model.length, 1); assert.equal(f.calls.prompt[0].requestId, f.calls.prompt[1].requestId);
      assert.equal((await retry.inspect(commandId)).selectedAgentModel.reasoningEffort, 'xhigh');
      assert.ok(await retry.toolBinding(receipt.sessionId));
      const record = JSON.parse(await readFile(join(f.root, 'task-launches', commandId + '.json'), 'utf8'));
      assert.equal(record.schema, 'reqpilot-pair-launch-record/v3');
      assert.deepEqual(record.modelSelection, codexModel);
      assert.deepEqual(record.selectedAgentModel, codexModel);
      assert.equal(receipt.schema, 'reqpilot-pair-launch/v2');
      assert.equal(receipt.workflowState.schema, 'reqpilot-fixed-workflow-state/v1');
    } finally { await retry.close(); }
  } finally { await f.close(); }
});
test('a host without Session-only model selection refuses dispatch before business preparation', async () => {
  const f = await fixture({ agentModel: codexModel }); try {
    delete f.services.sessions.selectSessionModel;
    await assert.rejects(f.launcher.start(f.input), /PAIR_SESSION_MODEL_SELECTION_UNSUPPORTED/);
    assert.equal(f.calls.prepare, 0); assert.equal(f.calls.create.length, 0); assert.equal(f.calls.prompt.length, 0);
  } finally { await f.close(); }
});
test('a lost model acknowledgement retries the exact Session selection before any prompt', async () => {
  const f = await fixture({ agentModel: codexModel }); try {
    f.services.sessions.selectSessionModel = async request => {
      f.calls.model.push(request);
      if (f.calls.model.length === 1) throw Error('model acknowledgement lost');
      const { sessionId, ...selected } = request; return { selected };
    };
    await assert.rejects(f.launcher.start(f.input), /model acknowledgement lost/);
    assert.equal(f.calls.prompt.length, 0);
    assert.equal((await f.launcher.start(f.input)).status, 'ACCEPTED');
    assert.deepEqual(f.calls.model[0], f.calls.model[1]); assert.equal(f.calls.prompt.length, 1);
  } finally { await f.close(); }
});
test('a corrupted model-bound receipt cannot submit a prompt on retry', async () => {
  const f = await fixture({ agentModel: codexModel }); try {
    f.services.sessions.selectSessionModel = async ({ sessionId, ...selected }) => ({ selected });
    f.services.sessions.prompt = async () => { throw Error('ack lost'); };
    await assert.rejects(f.launcher.start(f.input), /ack lost/);
    const file = join(f.root, 'task-launches', commandId + '.json');
    const record = JSON.parse(await readFile(file, 'utf8')); record.agentModel.reasoningEffort = 'high';
    await writeFile(file, JSON.stringify(record));
    f.services.sessions.prompt = async request => { f.calls.prompt.push(request); return { accepted: true }; };
    await assert.rejects(f.launcher.start(f.input), /UNSUPPORTED_LAUNCH_RECORD/);
    assert.equal(f.calls.prompt.length, 0);
  } finally { await f.close(); }
});
test('refused or silently downgraded model selections never dispatch a prompt', async () => {
  const f = await fixture({ agentModel: codexModel }); try {
    f.services.sessions.selectSessionModel = async () => { throw Error('model unavailable'); };
    await assert.rejects(f.launcher.start(f.input), /PAIR_SESSION_MODEL_UNAVAILABLE/);
    f.services.sessions.selectSessionModel = async () => ({ selected: { ...codexModel, reasoningEffort: 'high' } });
    await assert.rejects(f.launcher.start(f.input), /PAIR_TASK_MODEL_NOT_CONFIRMED/);
    assert.equal(f.calls.prompt.length, 0);
    f.services.sessions.selectSessionModel = async ({ sessionId, ...selected }) => ({ selected });
    assert.equal((await f.launcher.start(f.input)).status, 'ACCEPTED');
  } finally { await f.close(); }
});
test('legacy v1 receipts keep their original default-model policy after configuration changes', async () => {
  const f = await fixture(); try {
    const fingerprint = createHash('sha256').update(JSON.stringify([f.input.projectId, f.input.documentId, f.input.sourceSha256, f.input.goal, false])).digest('hex');
    const applicationId = 'reqpilotx-pair:' + commandId;
    await mkdir(join(f.root, 'task-launches'), { recursive: true });
    await writeFile(join(f.root, 'task-launches', commandId + '.json'), JSON.stringify({
      schema: 'reqpilot-pair-launch-record/v1', commandId, projectId: 'p', documentId: 'd', sourceSha256: f.input.sourceSha256,
      goal: f.input.goal, requireMapConfirmation: false, fingerprint, applicationId,
      serviceOrigin: 'http://127.0.0.1:1234', serviceInstanceId: null, sessionId: 'session-legacy',
      requestId: 'reqpilotx-' + commandId, phase: 'RECORDED', bundleSha: 'b'.repeat(64), capabilityRoot: f.root,
      python: '/installed/python', workspaceRelative: 'task-workspaces/' + commandId,
      agentPreset: 'standard', toolTransport: 'CLI' }));
    f.services.sessions.prompt = async () => { throw Error('ack lost'); };
    await assert.rejects(f.launcher.start(f.input), /ack lost/);
    await f.launcher.close();
    f.services.sessions.prompt = async () => ({ accepted: true });
    const retry = new TaskLauncher(f.runtime, { agentModel: codexModel }, () => f.services, f.launcher.transport);
    retry.ready = true; retry.capabilityRoot = f.root; retry.bundleSha = 'b'.repeat(64);
    try {
      const result = await retry.start(f.input); assert.equal(result.status, 'ACCEPTED');
      assert.equal(result.agentModel, undefined); assert.equal(result.modelSelection, undefined);
      assert.equal(f.calls.model.length, 0);
    } finally { await retry.close(); }
  } finally { await f.close(); }
});
test('invalid model configuration fails when the task launcher is constructed', () => {
  for (const agentModel of [null, {}, { ...codexModel, reasoningEffort: '' }, { ...codexModel, fallback: true }]) {
    assert.throws(() => new TaskLauncher({}, { agentModel }, () => ({})), /INVALID_AGENT_MODEL/);
  }
});
test('concurrent and repeated starts prepare once and keep one Pair workspace/session/request identity', async () => {
  const f = await fixture(); try {
    const [a, b] = await Promise.all([f.launcher.start(f.input), f.launcher.start(f.input)]); assert.deepEqual(a, b); assert.equal(a.status, 'ACCEPTED');
    assert.equal(a.workspaceId, 'workspace-one'); assert.equal(f.calls.prepare, 1); assert.equal(f.calls.prompt.length, 1);
    assert.deepEqual(await f.launcher.start(f.input), a); assert.equal(f.calls.prompt.length, 1);
    assert.equal(a.semanticAcceptance, 'NOT_CLAIMED'); assert.ok(!JSON.stringify(a).includes(f.root));
    assert.equal((await f.launcher.inspect(commandId)).taskId, 'task-one');
    await assert.rejects(f.launcher.start({ ...f.input, goal: 'different goal' }), /COMMAND_CONFLICT/);
  } finally { await f.close(); }
});
test('durable task lookup binds the exact project, source and work without dispatching another prompt', async () => {
  const f = await fixture(); try {
    const created = await f.launcher.start(f.input);
    const scope = { projectId: f.input.projectId, documentId: f.input.documentId, sourceSha256: f.input.sourceSha256 };
    const found = await f.launcher.list(scope);
    assert.equal(found.tasks.length, 1); assert.equal(found.tasks[0].commandId, created.commandId);
    assert.equal(found.tasks[0].request.goal, f.input.goal);
    assert.equal((await f.launcher.list({ ...scope, workRef: 'plugin-' + 'b'.repeat(32) })).tasks.length, 0);
    assert.equal((await f.launcher.list({ ...scope, sourceSha256: 'c'.repeat(64) })).tasks.length, 0);
    assert.equal(f.calls.prompt.length, 1);
    await assert.rejects(f.launcher.list({ ...scope, sourceSha256: 'invalid' }), /INVALID_TASK_LOOKUP/);
  } finally { await f.close(); }
});
test('lost prompt acknowledgement retries the same DSH request instead of a second session', async () => {
  const f = await fixture(); try {
    f.services.sessions.prompt = async value => { f.calls.prompt.push(value); if (f.calls.prompt.length === 1) throw Error('ack lost'); return { accepted: true }; };
    await assert.rejects(f.launcher.start(f.input), /ack lost/); f.launcher.config.agentPreset = 'changed-preset'; const result = await f.launcher.start(f.input);
    assert.equal(result.status, 'ACCEPTED'); assert.equal(f.calls.prepare, 1);
    assert.equal(f.calls.prompt[0].requestId, f.calls.prompt[1].requestId); assert.equal(f.calls.create[0].sessionId, f.calls.create[1].sessionId); assert.equal(f.calls.create[0].agentPreset, f.calls.create[1].agentPreset);
  } finally { await f.close(); }
});
test('source mismatch prevents preparation and model dispatch', async () => {
  const f = await fixture(); try {
    f.launcher.transport = async url => url.endsWith('/file') ? new Response('changed') : new Response(JSON.stringify([{ id: 'd', source_sha256: 'a'.repeat(64) }]));
    await assert.rejects(f.launcher.start(f.input), /SOURCE_CHANGED/); assert.equal(f.calls.prepare, 0); assert.equal(f.calls.prompt.length, 0);
  } finally { await f.close(); }
});
test('endpoint change after an ambiguous start requires reconnect, never silently edits a fixed task', async () => {
  const f = await fixture(); try {
    f.services.sessions.prompt = async () => { throw Error('connection lost'); };
    await assert.rejects(f.launcher.start(f.input)); f.runtime.snapshot.serviceOrigin = 'http://127.0.0.1:9999';
    await assert.rejects(f.launcher.start(f.input), /RECONNECT_REQUIRED/); assert.equal(f.calls.prepare, 1);
  } finally { await f.close(); }
});
test('shutdown before session admission prevents a model request', async () => {
  const f = await fixture(); try {
    f.services.sessions.create = async value => { f.launcher.closing = true; return { sessionId: value.sessionId }; };
    await assert.rejects(f.launcher.start(f.input), /INTERRUPTED/); assert.equal(f.calls.prompt.length, 0);
  } finally { await f.close(); }
});

test('durable native acceptance repairs a lost acknowledgement after endpoint change without another prompt', async () => {
  const f = await fixture(); try {
    let acceptedRequest;
    f.services.sessions.prompt = async value => { acceptedRequest = value; f.calls.prompt.push(value); throw Error('ack lost'); };
    await assert.rejects(f.launcher.start(f.input), /ack lost/);
    f.runtime.snapshot.serviceOrigin = 'http://127.0.0.1:7777';
    f.services.sessions.inspect = async id => { assert.equal(id, acceptedRequest.sessionId); return { events: [{ type: 'user/message', seq: 9, data: { source: { kind: 'user', rpcId: acceptedRequest.requestId } } }] }; };
    const receipt = await f.launcher.start(f.input); assert.equal(receipt.status, 'ACCEPTED');
    assert.equal(f.calls.prompt.length, 1); assert.equal(f.calls.create.length, 1); assert.equal(f.calls.prepare, 1);
    const record = JSON.parse(await readFile(join(f.root, 'task-launches', commandId + '.json'), 'utf8'));
    assert.equal(record.acceptanceEvidence.kind, 'SESSION_LOG'); assert.equal(record.serviceOrigin, 'http://127.0.0.1:1234');
  } finally { await f.close(); }
});
test('context injection or another request cannot confirm a lost user acknowledgement', async () => {
  const f = await fixture(); try {
    let request;
    f.services.sessions.prompt = async value => { request = value; throw Error('ack lost'); };
    await assert.rejects(f.launcher.start(f.input)); f.runtime.snapshot.serviceOrigin = 'http://127.0.0.1:7777';
    f.services.sessions.inspect = async () => ({ events: [
      { type: 'user/message', data: { source: { kind: 'agent-instructions', rpcId: request.requestId } } },
      { type: 'user/message', data: { source: { kind: 'user', rpcId: 'different-request' } } },
    ] });
    await assert.rejects(f.launcher.start(f.input), /RECONNECT_REQUIRED/);
  } finally { await f.close(); }
});


test('read-only task observation projects the bound session without prompting', async () => {
  const f = await fixture(); try {
    const result = await f.launcher.start(f.input);
    f.services.liveStatus = () => 'running';
    f.services.sessions.inspect = async id => { assert.equal(id,result.sessionId); return {events:[
      {type:'turn/start',time:1790070000000,data:{turn:1}},
      {type:'user/message',data:{source:{kind:'user',rpcId:f.calls.prompt[0].requestId}}},
      {type:'step/start',time:1790070000001,data:{step:1}},
    ]}; };
    const observed=await f.launcher.inspect(commandId);assert.equal(observed.execution.status,'RUNNING');
    assert.equal(observed.semanticAcceptance,'NOT_CLAIMED');assert.equal(f.calls.prompt.length,1);
    f.services.sessions.inspect=async()=>{throw Error('private transport detail');};
    assert.equal((await f.launcher.inspect(commandId)).execution,null);
  } finally {await f.close();}
});


test('MCP binding is pinned to the exact session, fixed package and workspace', async () => {
  const f=await fixture(); try {
    f.launcher.config.taskMcp=true;const result=await f.launcher.start(f.input);
    const binding=await f.launcher.toolBinding(result.sessionId);assert.equal(binding.command,f.runtime.python);
    assert.equal(await f.launcher.toolBinding('unrelated-session'),null);
    const path=join(f.root,'task-launches',commandId+'.json');const record=JSON.parse(await readFile(path,'utf8'));
    record.prepared.tools.mcp.cwd='/different';await writeFile(path,JSON.stringify(record));
    await assert.rejects(f.launcher.toolBinding(result.sessionId),/DESCRIPTOR_INVALID/);
    f.runtime.snapshot.serviceOrigin='http://127.0.0.1:7777';
    await assert.rejects(f.launcher.toolBinding(result.sessionId),/RECONNECT_REQUIRED/);
  }finally{await f.close();}
});

test('task control is bound to one saved work and idempotently cancels only its Pair session', async () => {
  const f = await fixture(); try {
    const started = await f.launcher.start(f.input); const cancelled = [];
    f.services.sessions.cancel = async value => { cancelled.push(value); return { accepted: true }; };
    const input = { commandId, controlId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', action: 'cancel',
      sessionId: started.sessionId, workRef: started.workRef, sourceSha256: started.sourceSha256 };
    assert.equal((await f.launcher.control(input)).control.status, 'ACCEPTED');
    assert.equal((await f.launcher.control(input)).control.status, 'ACCEPTED');
    assert.deepEqual(cancelled, [{ sessionId: started.sessionId }]);
    await assert.rejects(f.launcher.control({ ...input, controlId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', workRef: 'plugin-' + 'c'.repeat(32) }), /BINDING_MISMATCH/);
    await assert.rejects(f.launcher.control({ ...input, action: 'continue' }), /CONTROL_CONFLICT/);
  } finally { await f.close(); }
});

test('continuation reuses its request identity and repairs lost acknowledgement from the same Session journal', async () => {
  const f = await fixture(); try {
    f.launcher.config.taskMcp = true;
    const started = await f.launcher.start(f.input), controlId = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
    const input = { commandId, controlId, action: 'continue', sessionId: started.sessionId,
      workRef: started.workRef, sourceSha256: started.sourceSha256 };
    f.services.sessions.inspect = async () => ({ events: [] });
    f.services.sessions.prompt = async value => { f.calls.prompt.push(value); throw Error('ack lost'); };
    await assert.rejects(f.launcher.control(input), /ack lost/);
    const resumed = f.calls.prompt.at(-1);
    assert.equal(resumed.sessionId, started.sessionId); assert.match(resumed.content[0].text, /task_open/);
    f.runtime.snapshot.serviceOrigin = 'http://127.0.0.1:9999';
    f.services.sessions.inspect = async id => { assert.equal(id, started.sessionId); return { events: [{ type: 'user/message', seq: 18, data: { source: { kind: 'user', rpcId: resumed.requestId } } }] }; };
    assert.equal((await f.launcher.control(input)).control.status, 'ACCEPTED');
    assert.equal(f.calls.prompt.length, 2); // Initial prompt and one continuation, never a duplicate.
    const record = JSON.parse(await readFile(join(f.root, 'task-launches', commandId + '.json'), 'utf8'));
    assert.equal(record.controls[controlId].acceptanceEvidence.seq, 18);
  } finally { await f.close(); }
});

test('new tasks use the selected PAIR default model and freeze it before dispatch', async () => {
  const f = await fixture(); try {
    let seenRecord;
    f.services.sessions.selectSessionModel = async value => {
      f.calls.model.push(value);
      const record = JSON.parse(await readFile(join(f.root, 'task-launches', commandId + '.json'), 'utf8'));
      assert.equal(f.calls.prompt.length, 0); assert.deepEqual(record.modelSelection, defaultModel);
      assert.equal(record.selectedAgentModel, undefined);
      seenRecord = record; const { sessionId, ...selected } = value; return { selected };
    };
    const receipt = await f.launcher.start(f.input);
    assert.equal(receipt.status, 'ACCEPTED'); assert.equal(receipt.schema, 'reqpilot-pair-launch/v2');
    assert.deepEqual(receipt.modelSelection, defaultModel); assert.deepEqual(receipt.selectedAgentModel, defaultModel);
    assert.equal(seenRecord.schema, 'reqpilot-pair-launch-record/v3');
    assert.equal(seenRecord.workflow.id, 'reqpilot-fixed-workflow');
    assert.equal(f.calls.model.length, 1); assert.deepEqual(f.calls.model[0], { sessionId: receipt.sessionId, ...defaultModel });
    assert.equal(f.calls.prompt.length, 1);
  } finally { await f.close(); }
});

test('missing selected default model gives an actionable setup state before preparation', async () => {
  const f = await fixture(); try {
    f.services.defaultModel.currentSelection = () => null;
    await assert.rejects(f.launcher.start(f.input), /PAIR_DEFAULT_MODEL_UNAVAILABLE/);
    assert.equal(f.launcher.startAvailable(), false); assert.equal(f.launcher.startIssue(), 'PAIR_DEFAULT_MODEL_UNAVAILABLE');
    assert.equal(f.calls.prepare, 0); assert.equal(f.calls.create.length, 0); assert.equal(f.calls.prompt.length, 0);
  } finally { await f.close(); }
});

test('inspect rederives workflow from the task read API, not a writable cache', async () => {
  const f = await fixture(); try {
    await f.launcher.start(f.input);
    const workspace = join(f.root, 'task-workspaces', commandId);
    await mkdir(workspace, { recursive: true });
    const forged = { ...taskWorkflowState(f.input.sourceSha256), ready_to_review: true,
      stages: taskWorkflowState(f.input.sourceSha256).stages.map(stage => ({ ...stage, status: 'SAVED', evidence: [{ kind: 'FORGED', source_sha256: f.input.sourceSha256 }] })) };
    await writeFile(join(workspace, 'workflow-status.json'), JSON.stringify(forged));
    const base = taskWorkflowState(f.input.sourceSha256);
    f.workflowState = { ...base, next_stage: 'ITEMS_CLASSIFICATION',
      stages: base.stages.map(stage => stage.id === 'SOURCE_MAP'
        ? { ...stage, status: 'SAVED', evidence: [{ kind: 'DOCUMENT_MAP', source_sha256: f.input.sourceSha256 }] } : stage) };
    const observed = await f.launcher.inspect(commandId);
    assert.equal(observed.workflowState.next_stage, 'ITEMS_CLASSIFICATION');
    assert.equal(observed.workflowState.ready_to_review, false);
    assert.equal(observed.workflowState.stages[1].status, 'SAVED');
  } finally { await f.close(); }
});

test('forged all-saved, inconsistent readiness, stale basis and malformed read state stay unconfirmed', async () => {
  const f = await fixture(); try {
    await f.launcher.start(f.input);
    const base = taskWorkflowState(f.input.sourceSha256);
    const cases = [
      { ...base, ready_to_review: true, stages: base.stages.map(stage => ({ ...stage, status: 'SAVED' })) },
      { ...base, ready_to_review: true, stages: base.stages.map(stage => ({ ...stage, status: 'STALLED' })) },
      { ...base, source_sha256: 'e'.repeat(64) },
      { ...base, workflow: { ...base.workflow, brief_sha256: '0'.repeat(64) } },
      { ...base, stages: base.stages.slice(0, 2) },
      { ...base, stages: base.stages.map(stage => stage.id === 'EVIDENCE_ANALYSIS'
        ? { ...stage, status: 'SAVED', evidence: [{ kind: 'FORGED', source_sha256: f.input.sourceSha256 }] } : stage) },
      { ...base, ready_to_review: true, open_items: ['ITEMS_REQUIRED'],
        stages: base.stages.map(stage => ({ ...stage, status: 'SAVED', evidence: [{ kind: 'FORGED', source_sha256: f.input.sourceSha256 }] })) },
    ];
    for (const value of cases) {
      f.workflowState = value;
      const observed = await f.launcher.inspect(commandId);
      assert.equal(observed.workflowState, null);
      assert.equal(typeof observed.workflowUnconfirmed.reason, 'string');
      assert.equal(observed.workflowUnconfirmed.reason.length > 0, true);
    }
    f.workflowState = base;
    const restored = await f.launcher.inspect(commandId);
    assert.equal(restored.workflowUnconfirmed, undefined);
    assert.equal(restored.workflowState.stages[1].status, 'PENDING');
  } finally { await f.close(); }
});

test('legacy v1 receipts remain readable without a fixed workflow projection', async () => {
  const f = await fixture(); try {
    const fingerprint = createHash('sha256').update(JSON.stringify([f.input.projectId, f.input.documentId, f.input.sourceSha256, f.input.goal, false])).digest('hex');
    await mkdir(join(f.root, 'task-launches'), { recursive: true });
    await writeFile(join(f.root, 'task-launches', commandId + '.json'), JSON.stringify({
      schema: 'reqpilot-pair-launch-record/v1', commandId, projectId: 'p', documentId: 'd', sourceSha256: f.input.sourceSha256,
      goal: f.input.goal, requireMapConfirmation: false, fingerprint, applicationId: 'reqpilotx-pair:' + commandId,
      serviceOrigin: 'http://127.0.0.1:1234', serviceInstanceId: null, sessionId: 'session-legacy',
      requestId: 'reqpilotx-' + commandId, phase: 'ACCEPTED', bundleSha: 'b'.repeat(64), capabilityRoot: f.root,
      python: '/installed/python', workspaceRelative: 'task-workspaces/' + commandId,
      agentPreset: 'standard', toolTransport: 'CLI', prepared: { task_id: 'task-one', work_ref: 'plugin-' + 'a'.repeat(32),
        workspace: join(f.root, 'task-workspaces', commandId), tools: { schema: 'reqpilot-host-tools/v1', mcp: {} } } }));
    const observed = await f.launcher.inspect(commandId);
    assert.equal(observed.schema, 'reqpilot-pair-launch/v1');
    assert.equal(observed.workflowState, undefined);
    assert.equal(observed.workflowUnconfirmed, undefined);
  } finally { await f.close(); }
});
