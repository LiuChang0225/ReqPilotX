import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, mkdir, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash } from 'node:crypto';
import { TaskLauncher } from '../host/src/task-launch.js';
const commandId = '12345678-1234-4234-8234-123456789abc';
async function fixture() {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-launch-')); const source = Buffer.from('original'); const hash = createHash('sha256').update(source).digest('hex');
  const calls = { prepare: 0, create: [], prompt: [], workspace: [] };
  const runtime = { dataRoot: root, python: '/installed/python', ownerId: commandId, snapshot: { status: 'READY', mode: 'managed', serviceOrigin: 'http://127.0.0.1:1234' }, async command(args) {
    calls.prepare++; const input = JSON.parse(await readFile(args[2], 'utf8')); await mkdir(input.workspace, { recursive: true });
    return JSON.stringify({ schema: 'reqpilot-host-launch/v1', workspace: input.workspace, task_id: 'task-one', work_ref: 'plugin-' + 'a'.repeat(32), source_sha256: hash, bundle_sha256: 'b'.repeat(64), prompt: 'Read request.md', tools: { schema: 'reqpilot-host-tools/v1', mcp: { command: runtime.python, args: [join(input.workspace, '.reqpilot/scripts/mcp_server.py'), '--working-root', input.workspace, '--source-root', input.workspace], cwd: input.workspace } } });
  } };
  const services = { workspaces: { async create(value) { calls.workspace.push(value); return { workspace: { workspaceId: 'workspace-one' } }; } }, sessions: { async create(value) { calls.create.push(value); return { sessionId: value.sessionId }; }, async prompt(value) { calls.prompt.push(value); return { accepted: true }; } } };
  const transport = async url => url.endsWith('/file') ? new Response(source) : new Response(JSON.stringify([{ id: 'd', file_name: 'source.pdf', source_sha256: hash }]));
  const launcher = new TaskLauncher(runtime, {}, () => services, transport); launcher.ready = true; launcher.capabilityRoot = root; launcher.bundleSha = 'b'.repeat(64);
  const input = { commandId, projectId: 'p', documentId: 'd', sourceSha256: hash, goal: 'User goal' };
  return { root, calls, runtime, services, launcher, input, async close() { await launcher.close(); await rm(root, { recursive: true, force: true }); } };
}
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
