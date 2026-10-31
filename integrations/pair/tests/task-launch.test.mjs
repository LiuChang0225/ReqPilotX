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
  const calls = { prepare: 0, create: [], prompt: [], workspace: [], workspaceTitle: [], model: [], feedbackReads: 0 };
  const workRef = 'plugin-' + 'a'.repeat(32);
  let taskWorkflow = taskWorkflowState(hash);
  let workbench = { schema_version: 'reqpilot-plugin-workbench/v1', session_id: workRef, work_ref: workRef,
    project_id: 'p', document_id: 'd', source_sha256: hash, revision_id: 'c'.repeat(64), revision_number: 1,
    versions: [{ revision_id: 'c'.repeat(64), number: 1, stages: [], created_at: '2026-11-01T00:00:00.000Z' }],
    feedback: [], feedback_resolutions: {}, map_confirmation_required: false, confirmed_map_sha256: null };
  const runtime = { dataRoot: root, python: '/installed/python', ownerId: commandId, snapshot: { status: 'READY', mode: 'managed', serviceOrigin: 'http://127.0.0.1:1234' }, async command(args) {
    if (args.some(value => value.endsWith('task-workflow.py'))) return JSON.stringify({ status: 'READY', task_id: 'task-one', workflow: taskWorkflow });
    calls.prepare++; const input = JSON.parse(await readFile(args[2], 'utf8')); await mkdir(input.workspace, { recursive: true });
    return JSON.stringify({ schema: 'reqpilot-host-launch/v1', workspace: input.workspace, task_id: 'task-one', work_ref: 'plugin-' + 'a'.repeat(32), source_sha256: hash, bundle_sha256: 'b'.repeat(64), prompt: 'Read request.md', workflow: workflowState(hash), tools: { schema: 'reqpilot-host-tools/v1', mcp: { command: runtime.python, args: [join(input.workspace, '.reqpilot/scripts/mcp_server.py'), '--working-root', input.workspace, '--source-root', input.workspace], cwd: input.workspace } } });
  } };
  const services = { defaultModel: { currentSelection: () => ({ ...defaultModel }) },
    workspaces: { async create(value) { calls.workspace.push(value); return { workspace: { workspaceId: 'workspace-one' } }; },
      async rename(request) {
        if (!request || typeof request !== 'object' || Array.isArray(request)
          || Object.keys(request).some(key => !['workspaceId', 'title'].includes(key))
          || typeof request.workspaceId !== 'string' || typeof request.title !== 'string'
          || !request.workspaceId || !request.title) throw new TypeError('workspaceController.rename expects one WorkspaceRenameRequest');
        calls.workspaceTitle.push(request);
        return { workspace: { workspaceId: request.workspaceId, title: request.title } };
      } },
    sessions: { async create(value) { calls.create.push(value); return { sessionId: value.sessionId }; },
      async inspect() { const error = new Error('session not found'); error.code = 'session/not-found'; throw error; },
      async selectSessionModel(value) { calls.model.push(value); const { sessionId, ...selected } = value; return { selected }; },
      async prompt(value) { calls.prompt.push(value); return { accepted: true }; } } };
  const transport = async url => {
    if (url.includes('/agent-structure/delivery/plugin-workbench/')) {
      calls.feedbackReads++;
      return new Response(JSON.stringify(workbench));
    }
    return url.endsWith('/file') ? new Response(source) : new Response(JSON.stringify([{ id: 'd', file_name: 'source.pdf', source_sha256: hash }]));
  };
  const launcher = new TaskLauncher(runtime, config, () => services, transport); launcher.ready = true; launcher.capabilityRoot = root; launcher.bundleSha = 'b'.repeat(64);
  launcher.workflow = workflowSpec; launcher.workflowDescriptor = workflow;
  const input = { commandId, projectId: 'p', documentId: 'd', sourceSha256: hash, goal: 'User goal' };
  return { root, calls, runtime, services, launcher, input, get workflowState() { return taskWorkflow; }, set workflowState(value) { taskWorkflow = value; },
    get workbench() { return workbench; }, set workbench(value) { workbench = value; },
    async close() { await launcher.close(); await rm(root, { recursive: true, force: true }); } };
}
const codexModel = { provider: 'openai-codex', model: 'gpt-6.1-sol', reasoningEffort: 'xhigh' };
test('native tasks bind the configured model before admission and retain it across acknowledgement retries', async () => {
  const f = await fixture({ agentModel: codexModel, taskMcp: true }); try {
    f.services.sessions.selectSessionModel = async request => {
      f.calls.model.push(request);
      if (f.calls.model.length === 1) assert.equal(f.calls.prompt.length, 0);
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
      assert.equal(f.calls.model.length, 2);
      assert.deepEqual(f.calls.model[1], f.calls.model[0]);
      assert.equal(f.calls.prompt[0].requestId, f.calls.prompt[1].requestId);
      assert.equal((await retry.inspect(commandId)).selectedAgentModel.reasoningEffort, 'xhigh');
      assert.ok(await retry.toolBinding(receipt.sessionId));
      assert.equal(f.calls.workspaceTitle.length, 1);
      assert.equal(f.calls.workspaceTitle[0].workspaceId, 'workspace-one');
      assert.match(f.calls.workspaceTitle[0].title, /ReqPilotX · source\.pdf/);
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
test('v3 first user prompt directly carries bounded user notes with a legitimate operator pause', async () => {
  const f = await fixture(); try {
    const notes = '先 resume 并等待我完成资料导入。导入完成、我明确说继续后，再按固定流程完成全部阶段；不要因为这句等待说明取消后续条目、分析、自查和交付。';
    const receipt = await f.launcher.start({ ...f.input, goal: notes });
    assert.equal(receipt.status, 'ACCEPTED');
    const prompt = f.calls.prompt.at(-1);
    assert.equal(prompt.requestId, 'reqpilotx-' + commandId);
    const text = prompt.content[0].text;
    assert.match(text, /Read request\.md/);
    assert.ok(text.includes(notes));
    assert.match(text, /用户补充说明/);
    assert.match(text, /不是来源原文、第三方文档数据、审批或符合性结论/);
    assert.match(text, /不需要为了获取用户要求而读取 request\.md/);
    assert.match(text, /固定工作流版本：1\.0\.0/);
    assert.match(text, /该暂停只暂缓后续阶段/);
    assert.match(text, /不代表取消后续原本要求的阶段/);
    assert.match(text, /操作者确认后仍须完成固定工作流全部阶段和交付/);
    const record = JSON.parse(await readFile(join(f.root, 'task-launches', commandId + '.json'), 'utf8'));
    assert.equal(record.promptText, text);
    assert.equal(record.goal, notes);
    await f.launcher.start({ ...f.input, goal: notes });
    assert.equal(f.calls.prompt.length, 1, 'accepted replay must not dispatch another message');
  } finally { await f.close(); }
});
test('v3 lost-ack replay reuses the exact frozen notes prompt under the same request id', async () => {
  const f = await fixture(); try {
    const notes = '先恢复任务并等待我完成导入；我确认后继续完成全部固定阶段。';
    f.services.sessions.prompt = async value => { f.calls.prompt.push(value); if (f.calls.prompt.length === 1) throw Error('ack lost'); return { accepted: true }; };
    await assert.rejects(f.launcher.start({ ...f.input, goal: notes }), /ack lost/);
    const file = join(f.root, 'task-launches', commandId + '.json');
    const record = JSON.parse(await readFile(file, 'utf8'));
    assert.equal(record.phase, 'START_REQUESTED');
    assert.equal(typeof record.promptText, 'string');
    record.prepared.prompt = 'Read request.md changed after dispatch';
    await writeFile(file, JSON.stringify(record));
    assert.equal((await f.launcher.start({ ...f.input, goal: notes })).status, 'ACCEPTED');
    assert.equal(f.calls.prompt.length, 2);
    assert.equal(f.calls.prompt[0].requestId, f.calls.prompt[1].requestId);
    assert.equal(f.calls.prompt[0].content[0].text, f.calls.prompt[1].content[0].text);
    assert.doesNotMatch(f.calls.prompt[1].content[0].text, /changed after dispatch/);
    assert.ok(f.calls.prompt[1].content[0].text.includes(notes));
    assert.equal(f.calls.prepare, 1);
  } finally { await f.close(); }
});
test('legacy v1 start keeps its original prompt without v3 supplementary notes', async () => {
  const f = await fixture(); try {
    const fingerprint = createHash('sha256').update(JSON.stringify([f.input.projectId, f.input.documentId, f.input.sourceSha256, f.input.goal, false])).digest('hex');
    await mkdir(join(f.root, 'task-launches'), { recursive: true });
    await writeFile(join(f.root, 'task-launches', commandId + '.json'), JSON.stringify({
      schema: 'reqpilot-pair-launch-record/v1', commandId, projectId: 'p', documentId: 'd', sourceSha256: f.input.sourceSha256,
      goal: f.input.goal, requireMapConfirmation: false, fingerprint, applicationId: 'reqpilotx-pair:' + commandId,
      serviceOrigin: 'http://127.0.0.1:1234', serviceInstanceId: null, sessionId: 'session-legacy',
      requestId: 'reqpilotx-' + commandId, phase: 'RECORDED', bundleSha: 'b'.repeat(64), capabilityRoot: f.root,
      python: '/installed/python', workspaceRelative: 'task-workspaces/' + commandId,
      agentPreset: 'standard', toolTransport: 'CLI' }));
    assert.equal((await f.launcher.start(f.input)).status, 'ACCEPTED');
    const text = f.calls.prompt[0].content[0].text;
    assert.equal(text, 'Read request.md\n宿主展示入口是 Pair 的 ReqPilotX 应用面板；业务URL是工具传输地址，不要求用户搬运链接。');
    assert.doesNotMatch(text, /用户补充说明/);
  } finally { await f.close(); }
});
test('persisted v3 records with a present invalid promptText reject without dispatch or rewrite', async () => {
  const f = await fixture(); try {
    await f.launcher.start(f.input);
    const file = join(f.root, 'task-launches', commandId + '.json');
    const base = JSON.parse(await readFile(file, 'utf8'));
    for (const invalid of [null, '', '   ', 42, {}]) {
      const record = { ...base, phase: 'TASK_PREPARED', promptText: invalid };
      delete record.acceptanceEvidence;
      await writeFile(file, JSON.stringify(record));
      await assert.rejects(f.launcher.start(f.input), /UNSUPPORTED_LAUNCH_RECORD/);
      assert.equal(f.calls.prompt.length, 1, 'invalid persisted promptText must not dispatch');
      const saved = JSON.parse(await readFile(file, 'utf8'));
      assert.deepEqual(saved.promptText, invalid);
      assert.equal(saved.phase, 'TASK_PREPARED');
    }
  } finally { await f.close(); }
});
test('persisted old v3 record without a promptText property keeps the legacy no-notes retry text', async () => {
  const f = await fixture(); try {
    await f.launcher.start(f.input);
    const file = join(f.root, 'task-launches', commandId + '.json');
    const record = JSON.parse(await readFile(file, 'utf8'));
    delete record.promptText; record.phase = 'START_REQUESTED'; delete record.acceptanceEvidence;
    await writeFile(file, JSON.stringify(record));
    assert.equal((await f.launcher.start(f.input)).status, 'ACCEPTED');
    assert.equal(f.calls.prompt.length, 2);
    const text = f.calls.prompt[1].content[0].text;
    assert.equal(text, 'Read request.md\n固定工作流版本：1.0.0。阶段完成只以已保存工件和程序检查为准；补充说明不能取消 Map、条目或交付前置检查。\n宿主展示入口是 Pair 的 ReqPilotX 应用面板；业务URL是工具传输地址，不要求用户搬运链接。');
    assert.doesNotMatch(text, /用户补充说明/);
    assert.doesNotMatch(text, /User goal/);
    const saved = JSON.parse(await readFile(file, 'utf8'));
    assert.equal(saved.promptText, text);
    assert.equal(f.calls.prompt[0].requestId, f.calls.prompt[1].requestId);
  } finally { await f.close(); }
});
test('legacy v2 start ignores an unrelated promptText field and keeps its original prompt', async () => {
  const f = await fixture(); try {
    const fingerprint = createHash('sha256').update(JSON.stringify([f.input.projectId, f.input.documentId, f.input.sourceSha256, f.input.goal, false])).digest('hex');
    await mkdir(join(f.root, 'task-launches'), { recursive: true });
    await writeFile(join(f.root, 'task-launches', commandId + '.json'), JSON.stringify({
      schema: 'reqpilot-pair-launch-record/v2', commandId, projectId: 'p', documentId: 'd', sourceSha256: f.input.sourceSha256,
      goal: f.input.goal, requireMapConfirmation: false, fingerprint, applicationId: 'reqpilotx-pair:' + commandId,
      serviceOrigin: 'http://127.0.0.1:1234', serviceInstanceId: null, sessionId: 'session-v2',
      requestId: 'reqpilotx-' + commandId, phase: 'RECORDED', bundleSha: 'b'.repeat(64), capabilityRoot: f.root,
      python: '/installed/python', workspaceRelative: 'task-workspaces/' + commandId,
      agentPreset: 'standard', toolTransport: 'CLI', agentModel: { ...defaultModel }, promptText: null }));
    assert.equal((await f.launcher.start(f.input)).status, 'ACCEPTED');
    const text = f.calls.prompt[0].content[0].text;
    assert.equal(text, 'Read request.md\n宿主展示入口是 Pair 的 ReqPilotX 应用面板；业务URL是工具传输地址，不要求用户搬运链接。');
    assert.doesNotMatch(text, /用户补充说明/);
    const saved = JSON.parse(await readFile(join(f.root, 'task-launches', commandId + '.json'), 'utf8'));
    assert.equal(saved.promptText, null);
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

test('a started or completed existing Session is restored without minting another identity', async () => {
  for (const state of ['started', 'completed']) {
    const f = await fixture(); try {
      const started = await f.launcher.start(f.input);
      const file = join(f.root, 'task-launches', commandId + '.json');
      const record = JSON.parse(await readFile(file, 'utf8'));
      record.phase = 'TASK_PREPARED'; delete record.acceptanceEvidence;
      await writeFile(file, JSON.stringify(record));
      const events = [{ type: 'turn/start', seq: 1, data: { turn: 1 } },
        { type: 'turn/end', seq: 2, data: { reason: { kind: state === 'started' ? 'interrupted' : 'completed' } } }];
      f.services.sessions.inspect = async id => { assert.equal(id, started.sessionId);
        return { meta: { id: started.sessionId, cwd: join(f.root, 'task-workspaces', commandId), workspaceId: 'workspace-one' }, events }; };
      const receipt = await f.launcher.start(f.input);
      assert.equal(receipt.status, 'ACCEPTED');
      assert.equal(f.calls.create.length, 1, 'existing started Session must not call create again');
      assert.equal(f.calls.prompt.length, 2);
      assert.equal(f.calls.prompt[1].sessionId, started.sessionId);
      assert.equal(f.calls.prompt[1].requestId, f.calls.prompt[0].requestId);
    } finally { await f.close(); }
  }
});

test('a busy or model-unconfirmed existing Session refuses recovery without create or prompt', async () => {
  const busy = await fixture(); try {
    const started = await busy.launcher.start(busy.input);
    const file = join(busy.root, 'task-launches', commandId + '.json');
    const record = JSON.parse(await readFile(file, 'utf8'));
    record.phase = 'TASK_PREPARED'; delete record.acceptanceEvidence;
    await writeFile(file, JSON.stringify(record));
    busy.services.sessions.inspect = async () => ({ meta: { id: started.sessionId, cwd: join(busy.root, 'task-workspaces', commandId) },
      events: [{ type: 'turn/start', seq: 1, data: {} }] });
    await assert.rejects(busy.launcher.start(busy.input), /PAIR_SESSION_BUSY/);
    assert.equal(busy.calls.create.length, 1); assert.equal(busy.calls.prompt.length, 1);
  } finally { await busy.close(); }
  const unconfirmed = await fixture(); try {
    const started = await unconfirmed.launcher.start(unconfirmed.input);
    const file = join(unconfirmed.root, 'task-launches', commandId + '.json');
    const record = JSON.parse(await readFile(file, 'utf8'));
    record.phase = 'TASK_PREPARED'; delete record.acceptanceEvidence; delete record.selectedAgentModel;
    await writeFile(file, JSON.stringify(record));
    unconfirmed.services.sessions.inspect = async () => ({ meta: { id: started.sessionId,
      cwd: join(unconfirmed.root, 'task-workspaces', commandId) }, events: [{ type: 'turn/start', seq: 1, data: {} },
        { type: 'turn/end', seq: 2, data: {} }] });
    await assert.rejects(unconfirmed.launcher.start(unconfirmed.input), /PAIR_SESSION_EXISTING_MODEL_UNCONFIRMED/);
    assert.equal(unconfirmed.calls.create.length, 1); assert.equal(unconfirmed.calls.prompt.length, 1);
  } finally { await unconfirmed.close(); }
});

test('existing Session recovery refuses a different cwd, workspace or model', async () => {
  for (const [label, meta, events, error] of [
    ['cwd', { cwd: '/different', workspaceId: 'workspace-one' }, [], /TASK_SESSION_WORKSPACE_MISMATCH/],
    ['workspace', { workspaceId: 'other-workspace' }, [], /TASK_SESSION_WORKSPACE_MISMATCH/],
    ['model', { workspaceId: 'workspace-one' }, [{ type: 'request/header', data: { header: { config: {
      provider: 'other-provider', model: 'other-model', reasoningEffort: 'max' } } } }], /PAIR_SESSION_MODEL_CONFLICT/],
  ]) {
    const f = await fixture(); try {
      const started = await f.launcher.start(f.input);
      const file = join(f.root, 'task-launches', commandId + '.json');
      const record = JSON.parse(await readFile(file, 'utf8'));
      record.phase = 'TASK_PREPARED'; delete record.acceptanceEvidence;
      await writeFile(file, JSON.stringify(record));
      f.services.sessions.inspect = async () => ({ meta: { id: started.sessionId,
        cwd: join(f.root, 'task-workspaces', commandId), ...meta }, events });
      await assert.rejects(f.launcher.start(f.input), error, label);
      assert.equal(f.calls.create.length, 1); assert.equal(f.calls.prompt.length, 1);
    } finally { await f.close(); }
  }
});

test('a retried prepared record with another source/bundle is refused before Session or model work', async () => {
  const f = await fixture(); try {
    await f.launcher.start(f.input);
    const file = join(f.root, 'task-launches', commandId + '.json');
    const record = JSON.parse(await readFile(file, 'utf8'));
    record.phase = 'TASK_PREPARED'; delete record.acceptanceEvidence;
    record.prepared.source_sha256 = 'f'.repeat(64);
    await writeFile(file, JSON.stringify(record));
    await assert.rejects(f.launcher.start(f.input), /TASK_PREPARED_SOURCE_MISMATCH/);
    assert.equal(f.calls.create.length, 1); assert.equal(f.calls.prompt.length, 1);
  } finally { await f.close(); }
});

test('master {meta,events} reconnect uses the same observation both times and rejects concurrent change', async () => {
  const f = await fixture({ taskMcp: true }); try {
    const started = await f.launcher.start(f.input);
    const file = join(f.root, 'task-launches', commandId + '.json');
    const record = JSON.parse(await readFile(file, 'utf8'));
    f.runtime.snapshot.serviceOrigin = 'http://127.0.0.1:7777';
    f.runtime.verifyEndpoint = async () => ({ ownerId: f.runtime.ownerId, instanceId: 'instance-new',
      serviceOrigin: f.runtime.snapshot.serviceOrigin });
    const originalCommand = f.runtime.command;
    f.runtime.command = async (argv, env, options) => {
      if (argv.some(value => String(value).endsWith('task-reconnect.py'))) {
        const payload = JSON.parse(await readFile(argv[argv.length - 1], 'utf8'));
        return JSON.stringify({ schema: 'reqpilot-pair-reconnect/v1', workspace: payload.workspace,
          task_id: payload.task_id, work_ref: payload.work_ref, source_sha256: payload.source_sha256,
          bundle_sha256: payload.bundle_sha256, service_origin: payload.to_origin, mutation_replayed: false });
      }
      return originalCommand(argv, env, options);
    };
    const meta = { id: started.sessionId, cwd: join(f.root, 'task-workspaces', commandId), workspaceId: 'workspace-one' };
    const events = [{ type: 'user/message', seq: 1, data: { source: { kind: 'user', rpcId: record.requestId } } },
      { type: 'turn/start', seq: 2, data: {} }, { type: 'turn/end', seq: 3, data: {} }];
    let reads = 0;
    f.services.sessions.inspect = async () => ({ meta, events: reads++ === 0 ? events : events.map(event => ({ ...event })) });
    await f.launcher.reconnect(record);
    const saved = JSON.parse(await readFile(file, 'utf8'));
    assert.equal(saved.serviceOrigin, 'http://127.0.0.1:7777');
    assert.equal(saved.reconnectEvidence.instanceId, 'instance-new');
    assert.equal(reads, 2);

    const changed = JSON.parse(await readFile(file, 'utf8'));
    changed.serviceOrigin = 'http://127.0.0.1:1234'; changed.serviceInstanceId = null;
    await writeFile(file, JSON.stringify(changed));
    f.runtime.snapshot.serviceOrigin = 'http://127.0.0.1:7778';
    f.runtime.verifyEndpoint = async () => ({ ownerId: f.runtime.ownerId, instanceId: 'instance-later',
      serviceOrigin: f.runtime.snapshot.serviceOrigin });
    reads = 0;
    f.services.sessions.inspect = async () => ({ meta, events: reads++ === 0 ? events : [...events, { type: 'assistant/message', seq: 4, data: { text: 'concurrent' } }] });
    await assert.rejects(f.launcher.reconnect(changed), /TASK_RECONNECT_SESSION_CHANGED/);
    const after = JSON.parse(await readFile(file, 'utf8'));
    assert.ok(after.reconnectIntent, 'failed reconnect keeps the frozen intent for explicit recovery');
    assert.equal(after.serviceOrigin, 'http://127.0.0.1:1234');
  } finally { await f.close(); }
});

test('a recreated Session reinstalls the frozen model before the retried prompt', async () => {
  const f = await fixture(); try {
    const started = await f.launcher.start(f.input);
    assert.equal(f.calls.model.length, 1);
    let liveSelection = null;
    f.services.sessions.selectSessionModel = async ({ sessionId, ...selected }) => {
      f.calls.model.push({ sessionId, ...selected });
      liveSelection = selected;
      return { selected };
    };
    f.services.sessions.prompt = async value => {
      assert.deepEqual(liveSelection, defaultModel);
      f.calls.prompt.push(value);
      return { accepted: true };
    };
    const file = join(f.root, 'task-launches', commandId + '.json');
    const record = JSON.parse(await readFile(file, 'utf8'));
    record.phase = 'TASK_PREPARED'; delete record.acceptanceEvidence;
    await writeFile(file, JSON.stringify(record));
    const receipt = await f.launcher.start(f.input);
    assert.equal(receipt.status, 'ACCEPTED');
    assert.equal(f.calls.create.length, 2);
    assert.equal(f.calls.create[1].sessionId, started.sessionId);
    assert.equal(f.calls.model.length, 2);
    assert.deepEqual(f.calls.model[1], { sessionId: started.sessionId, ...defaultModel });
    assert.deepEqual(liveSelection, defaultModel);
    assert.deepEqual(receipt.selectedAgentModel, defaultModel);
    assert.equal(f.calls.prompt.length, 2);
    assert.equal(f.calls.prompt[1].requestId, f.calls.prompt[0].requestId);
  } finally { await f.close(); }
});

test('a recreated Session without model selection support refuses the retried prompt', async () => {
  const f = await fixture(); try {
    await f.launcher.start(f.input);
    const file = join(f.root, 'task-launches', commandId + '.json');
    const record = JSON.parse(await readFile(file, 'utf8'));
    record.phase = 'TASK_PREPARED'; delete record.acceptanceEvidence;
    await writeFile(file, JSON.stringify(record));
    delete f.services.sessions.selectSessionModel;
    await assert.rejects(f.launcher.start(f.input), /PAIR_SESSION_MODEL_SELECTION_UNSUPPORTED/);
    assert.equal(f.calls.prompt.length, 1);
    assert.equal((await f.launcher.inspect(commandId)).status, 'TASK_PREPARED');
  } finally { await f.close(); }
});

const continueInput = started => ({ commandId, controlId: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
  action: 'continue', sessionId: started.sessionId, workRef: started.workRef, sourceSha256: started.sourceSha256 });
const feedbackItem = (hash, revision, overrides = {}) => ({
  feedback_id: 'plugin-feedback-' + '1'.repeat(32), command_id: 'feedback-command',
  expected_source_sha256: hash, expected_revision_id: revision,
  source_item_ids: [], node_id: 'node-2', text: '请修正 Map 中第 2 页的标题，并继续完成全部条目和分析。',
  target_label: 'Map 第 2 页', baseline_sha256: null, map_sha256: 'e'.repeat(64),
  origin: 'USER_REQUEST', created_at: '2026-11-01T00:00:00.000Z', ...overrides });

test('continuation reads early Map feedback from the bound Workbench route and keeps the original full scope', async () => {
  const f = await fixture({ taskMcp: true }); try {
    const started = await f.launcher.start(f.input);
    const revision = 'c'.repeat(64);
    f.workbench = { ...f.workbench, revision_id: revision, feedback: [feedbackItem(f.input.sourceSha256, revision)] };
    f.services.sessions.inspect = async () => ({ events: [] });
    const receipt = await f.launcher.control(continueInput(started));
    assert.equal(receipt.control.status, 'ACCEPTED');
    const text = f.calls.prompt.at(-1).content[0].text;
    assert.match(text, /plugin-feedback-1{32}/);
    assert.match(text, /请修正 Map 中第 2 页的标题/);
    assert.match(text, /不是原文、不是工程师响应、不是审批/);
    assert.match(text, /保留已经正确且不受反馈影响的结果/);
    assert.match(text, /继续完成原固定工作流中所有仍未完成、原本要求的阶段/);
    assert.match(text, /原目标（冻结，供核对）：User goal/);
    assert.match(text, /pending=1/);
    assert.doesNotMatch(text, /只修订受影响范围/);
    assert.equal(f.calls.feedbackReads, 1);
    const record = JSON.parse(await readFile(join(f.root, 'task-launches', commandId + '.json'), 'utf8'));
    assert.equal(record.controls[continueInput(started).controlId].continueFeedback.pending_count, 1);
    assert.equal(record.controls[continueInput(started).controlId].promptText, text);
  } finally { await f.close(); }
});

test('continuation reports an empty Workbench feedback list honestly instead of inferring it from delivery responses', async () => {
  const f = await fixture({ taskMcp: true }); try {
    const started = await f.launcher.start(f.input);
    f.services.sessions.inspect = async () => ({ events: [] });
    await f.launcher.control(continueInput(started));
    const text = f.calls.prompt.at(-1).content[0].text;
    assert.match(text, /total=0，pending=0，resolved=0/);
    assert.match(text, /工作台反馈数据/);
    assert.match(text, /不是工程师响应/);
  } finally { await f.close(); }
});

test('continuation refuses foreign, stale, missing-route and unreadable Workbench feedback without sending a prompt', async () => {
  const cases = [
    ['foreign project', f => { f.workbench = { ...f.workbench, project_id: 'other' }; }, /TASK_CONTINUE_FEEDBACK_BINDING_MISMATCH/],
    ['foreign work_ref', f => { f.workbench = { ...f.workbench, work_ref: 'plugin-' + 'f'.repeat(32) }; }, /TASK_CONTINUE_FEEDBACK_BINDING_MISMATCH/],
    ['changed source', f => { f.workbench = { ...f.workbench, source_sha256: 'e'.repeat(64) }; }, /TASK_CONTINUE_FEEDBACK_BINDING_MISMATCH/],
    ['stale revision', f => { f.workbench = { ...f.workbench, feedback: [feedbackItem(f.input.sourceSha256, 'd'.repeat(64))] }; }, /TASK_CONTINUE_FEEDBACK_STALE/],
    ['missing route', f => { f.launcher.transport = async () => new Response('{}', { status: 404 }); }, /TASK_CONTINUE_FEEDBACK_ROUTE_MISSING/],
    ['unreadable route', f => { f.launcher.transport = async () => { throw Error('network down'); }; }, /TASK_CONTINUE_FEEDBACK_UNREADABLE/],
  ];
  for (const [label, mutate, expected] of cases) {
    const f = await fixture({ taskMcp: true }); try {
      const started = await f.launcher.start(f.input); mutate(f);
      f.services.sessions.inspect = async () => ({ events: [] });
      await assert.rejects(f.launcher.control(continueInput(started)), expected, label);
      assert.equal(f.calls.prompt.length, 1, label);
    } finally { await f.close(); }
  }
});

test('same continuation control reuses one frozen feedback prompt and request identity after a lost acknowledgement', async () => {
  const f = await fixture({ taskMcp: true }); try {
    const started = await f.launcher.start(f.input);
    const revision = 'c'.repeat(64);
    f.workbench = { ...f.workbench, revision_id: revision, feedback: [feedbackItem(f.input.sourceSha256, revision)] };
    f.services.sessions.inspect = async () => ({ events: [] });
    let attempts = 0;
    f.services.sessions.prompt = async value => { f.calls.prompt.push(value); if (++attempts === 1) throw Error('ack lost'); return { accepted: true }; };
    const input = continueInput(started);
    await assert.rejects(f.launcher.control(input), /ack lost/);
    f.workbench = { ...f.workbench, feedback: [feedbackItem(f.input.sourceSha256, revision,
      { feedback_id: 'plugin-feedback-' + '2'.repeat(32), text: 'Later feedback must not replace the frozen prompt.' })] };
    assert.equal((await f.launcher.control(input)).control.status, 'ACCEPTED');
    assert.equal(f.calls.feedbackReads, 1);
    assert.equal(f.calls.prompt.length, 3);
    assert.equal(f.calls.prompt[1].content[0].text, f.calls.prompt[2].content[0].text);
    assert.equal(f.calls.prompt[1].requestId, f.calls.prompt[2].requestId);
    assert.match(f.calls.prompt[2].content[0].text, /plugin-feedback-1{32}/);
    assert.doesNotMatch(f.calls.prompt[2].content[0].text, /Later feedback/);
    assert.equal((await f.launcher.control(input)).control.status, 'ACCEPTED');
    assert.equal(f.calls.prompt.length, 3);
  } finally { await f.close(); }
});

test('a durable continuation journal entry is reconciled before any feedback read or prompt resend', async () => {
  const f = await fixture({ taskMcp: true }); try {
    const started = await f.launcher.start(f.input);
    const input = continueInput(started);
    f.services.sessions.prompt = async value => { f.calls.prompt.push(value); throw Error('ack lost'); };
    await assert.rejects(f.launcher.control(input), /ack lost/);
    const requestId = f.calls.prompt.at(-1).requestId;
    f.services.sessions.inspect = async () => ({ events: [{ type: 'user/message', seq: 21,
      data: { source: { kind: 'user', rpcId: requestId } } }] });
    f.workbench = { ...f.workbench, project_id: 'foreign' };
    assert.equal((await f.launcher.control(input)).control.status, 'ACCEPTED');
    assert.equal(f.calls.prompt.length, 2);
    assert.equal(f.calls.feedbackReads, 1);
  } finally { await f.close(); }
});

test('continuation respects a requested Map confirmation and never turns feedback into approval', async () => {
  const f = await fixture({ taskMcp: true }); try {
    const started = await f.launcher.start({ ...f.input, requireMapConfirmation: true });
    f.services.sessions.inspect = async () => ({ events: [] });
    f.workbench = { ...f.workbench, map_confirmation_required: true, confirmed_map_sha256: null };
    await f.launcher.control(continueInput(started));
    let text = f.calls.prompt.at(-1).content[0].text;
    assert.match(text, /当前尚未确认/);
    assert.match(text, /不构成 Map 确认/);
    assert.match(text, /不要自行继续条目\/分析或伪造确认/);

    f.services.sessions.prompt = async value => { f.calls.prompt.push(value); return { accepted: true }; };
    f.services.sessions.inspect = async () => ({ events: [] });
    f.workbench = { ...f.workbench, map_confirmation_required: false, confirmed_map_sha256: 'e'.repeat(64),
      stages: [{ stage: 'MAP', artifact_sha256: 'e'.repeat(64), count: 1 }] };
    const second = { ...continueInput(started), controlId: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee' };
    await f.launcher.control(second);
    text = f.calls.prompt.at(-1).content[0].text;
    assert.match(text, /已在 PAIR 工作台确认当前 Map/);
    assert.match(text, /e{64}/);
  } finally { await f.close(); }
});

test('continuation accepts a real no-artifact Workbench state with null revision and empty feedback', async () => {
  const f = await fixture({ taskMcp: true }); try {
    const started = await f.launcher.start(f.input);
    f.workbench = { ...f.workbench, revision_id: null, revision_number: 0, versions: [], feedback: [],
      feedback_resolutions: {}, confirmed_map_sha256: null };
    f.services.sessions.inspect = async () => ({ events: [] });
    await f.launcher.control(continueInput(started));
    const text = f.calls.prompt.at(-1).content[0].text;
    assert.match(text, /current_revision=NONE/);
    assert.match(text, /total=0，pending=0/);
  } finally { await f.close(); }
});

test('continuation keeps large Workbench feedback lists honest and bounded', async () => {
  const f = await fixture({ taskMcp: true }); try {
    const started = await f.launcher.start(f.input);
    const revision = 'c'.repeat(64);
    const feedback = Array.from({ length: 30 }, (_, index) => feedbackItem(f.input.sourceSha256, revision, {
      feedback_id: 'plugin-feedback-' + String(index).padStart(32, '0'),
      text: '反馈 ' + index + '：请逐条处理并继续原任务全部阶段。' }));
    f.workbench = { ...f.workbench, revision_id: revision, feedback };
    f.services.sessions.inspect = async () => ({ events: [] });
    await f.launcher.control(continueInput(started));
    const text = f.calls.prompt.at(-1).content[0].text;
    assert.match(text, /pending=30/);
    assert.match(text, /另有 6 条仅保留身份/);
    assert.match(text, /plugin-feedback-00000000000000000000000000000024/);
    const record = JSON.parse(await readFile(join(f.root, 'task-launches', commandId + '.json'), 'utf8'));
    const saved = record.controls[continueInput(started).controlId].continueFeedback;
    assert.equal(saved.items.length, 24);
    assert.equal(saved.omitted.length, 6);
    assert.equal(saved.included_count + saved.omitted_count, saved.pending_count);
  } finally { await f.close(); }
});

test('continuation does not treat a retained old Map confirmation as current approval', async () => {
  const f = await fixture({ taskMcp: true }); try {
    const started = await f.launcher.start({ ...f.input, requireMapConfirmation: true });
    const mapV1 = '1'.repeat(64), mapV2 = '2'.repeat(64), revisionV1 = 'c'.repeat(64), revisionV2 = 'd'.repeat(64);
    f.workbench = { ...f.workbench, revision_id: revisionV1, revision_number: 1,
      versions: [{ revision_id: revisionV1, number: 1, stages: [{ stage: 'MAP', artifact_sha256: mapV1, count: 1 }],
        created_at: '2026-11-01T00:00:00.000Z' }],
      stages: [{ stage: 'MAP', artifact_sha256: mapV1, count: 1 }],
      confirmed_map_sha256: mapV1, map_confirmation_required: false };
    f.services.sessions.inspect = async () => ({ events: [] });
    await f.launcher.control(continueInput(started));
    let text = f.calls.prompt.at(-1).content[0].text;
    assert.match(text, /已在 PAIR 工作台确认当前 Map/);
    assert.match(text, new RegExp(mapV1));

    f.workbench = { ...f.workbench, revision_id: revisionV2, revision_number: 2,
      versions: [...f.workbench.versions, { revision_id: revisionV2, number: 2,
        stages: [{ stage: 'MAP', artifact_sha256: mapV2, count: 2 }], created_at: '2026-11-01T00:01:00.000Z' }],
      stages: [{ stage: 'MAP', artifact_sha256: mapV2, count: 2 }],
      confirmed_map_sha256: mapV1, map_confirmation_required: true };
    const second = { ...continueInput(started), controlId: 'ffffffff-ffff-4fff-8fff-ffffffffffff' };
    await f.launcher.control(second);
    text = f.calls.prompt.at(-1).content[0].text;
    assert.match(text, /当前尚未确认/);
    assert.match(text, /不构成 Map 确认/);
    assert.doesNotMatch(text, /已在 PAIR 工作台确认当前 Map/);
    const record = JSON.parse(await readFile(join(f.root, 'task-launches', commandId + '.json'), 'utf8'));
    const saved = record.controls[second.controlId].continueFeedback;
    assert.equal(saved.map_confirmation_required, true);
    assert.equal(saved.map_confirmation_pending, true);
    assert.equal(saved.confirmed_map_sha256, null);
    assert.equal(saved.current_map_sha256, mapV2);
  } finally { await f.close(); }
});
