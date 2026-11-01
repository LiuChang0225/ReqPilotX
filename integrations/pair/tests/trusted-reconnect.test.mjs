/** Synthetic HTTP listeners + real owned lease + fixed portable task. No PG, DSH or model calls. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createHash, createHmac, randomUUID } from 'node:crypto';
import { TaskLauncher } from '../host/src/task-launch.js';
import { ManagedRuntime } from '../host/src/managed-runtime.js';
import { nativeSubprocess } from './native-subprocess.mjs';
const python = process.env.PAIR_RECONNECT_PYTHON, capabilityRoot = process.env.PAIR_RECONNECT_BUNDLE;
const enabled = Boolean(python && capabilityRoot);
const sha = value => createHash('sha256').update(value).digest('hex');
async function fixture(populated = false) {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-trusted-reconnect-'));
  let runtime = new ManagedRuntime(nativeSubprocess, { dataRoot: root, python }); await runtime.claim();
  let source = Buffer.from('synthetic source, no extraction'), sourceInfo;
  if (populated) {
    const script = "import sys,json;from pathlib import Path;sys.path[:0]=[sys.argv[1]+'/runtime/prototype',sys.argv[2]];from portable_fixtures import make_fixture;source,capture,mapping=make_fixture(Path(sys.argv[3]));file=source.parent/'mapping.json';file.write_text(json.dumps(mapping));print(json.dumps({'source':str(source),'capture':str(capture),'mapping':str(file)}))";
    sourceInfo = JSON.parse((await runtime.command([python, '-c', script, capabilityRoot, fileURLToPath(new URL('../../../plugins/reqpilot/tests', import.meta.url)), join(root, 'synthetic-fixture')], { PYTHONDONTWRITEBYTECODE: '1' })).trim().split('\n').at(-1));
    source = await readFile(sourceInfo.source);
  }
  const sourceSha = sha(source), commandId = randomUUID();
  const bundleSha = JSON.parse(await readFile(join(capabilityRoot, 'build-receipt.json'), 'utf8')).bundle_sha256;
  const works = new Map(), events = [], calls = { prompt: [], create: 0, workspace: 0, resume: [], writes: 0 };
  let server, binding, changedSource = false, foreignProof = false, foreignWork = false, lostAck = false;
  const sessions = {
    async create(input) { calls.create++; binding = { sessionId: input.sessionId, workspaceId: input.workspaceId }; return { sessionId: input.sessionId }; },
    async inspect(id) { assert.equal(id, binding.sessionId); return { session: binding, events }; },
    async prompt(input) {
      calls.prompt.push(input);
      events.push({ type: 'turn/start', data: { turn: calls.prompt.length } },
        { type: 'user/message', seq: events.length + 1, data: { source: { kind: 'user', rpcId: input.requestId } } },
        { type: 'turn/end', data: { reason: { kind: 'completed' } } });
      if (calls.prompt.length > 1) {
        const record = await saved();
        const tools = await launcher.toolBinding(binding.sessionId);
        const inputFile = join(root, 'resume.json');
        await writeFile(inputFile, JSON.stringify({ mode: 'resume', task_id: record.prepared.task_id }));
        const resume = JSON.parse(await runtime.command([python, join(tools.cwd, '.reqpilot/scripts/reqpilot.py'), 'task', 'open', '--working-root', tools.cwd, '--input', inputFile], { PYTHONDONTWRITEBYTECODE: '1' }));
        await writeFile(inputFile, JSON.stringify({ task_id: record.prepared.task_id, direction: 'pull' }));
        const feedback = JSON.parse(await runtime.command([python, join(tools.cwd, '.reqpilot/scripts/reqpilot.py'), 'task', 'sync', '--working-root', tools.cwd, '--input', inputFile], { PYTHONDONTWRITEBYTECODE: '1' }));
        calls.resume.push({ resume, feedback, tools });
      }
      if (lostAck) throw Error('ack lost');
      return { accepted: true };
    },
  };
  const services = { sessions, workspaces: { async create() { calls.workspace++; return { workspace: { workspaceId: 'workspace-owned' } }; } } };
  const listen = async (port = 0) => {
    server = createServer(async (req, res) => {
      const url = new URL(req.url, 'http://localhost');
      const send = value => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
      if (url.pathname === '/api/pair-managed-identity') {
        const challenge = url.searchParams.get('challenge');
        const proof = createHmac('sha256', Buffer.from(foreignProof ? '0'.repeat(64) : runtime.endpointProofKey, 'hex'))
          .update(JSON.stringify([runtime.ownerId, runtime.token, runtime.snapshot.serviceOrigin, challenge])).digest('hex');
        send({ ownerId: runtime.ownerId, instanceId: runtime.token, challenge, proof }); return;
      }
      if (req.url === '/api/projects/p/sor-documents') { send([{ id: 'd', file_name: 'source.pdf', source_sha256: changedSource ? 'e'.repeat(64) : sourceSha }]); return; }
      if (req.url.endsWith('/file')) { res.writeHead(200); res.end(source); return; }
      if (req.method === 'POST' && req.url.endsWith('/plugin-workbench')) {
        calls.writes++; let body = ''; for await (const chunk of req) body += chunk;
        const input = JSON.parse(body);
        if (!works.has(input.session_id)) works.set(input.session_id, { session_id: input.session_id, source_sha256: sourceSha,
          project_id: 'p', document_id: 'd', revision_id: null, revision_number: 0,
          map_confirmation_required: false, stages: [], versions: [], feedback: [] });
        const work = works.get(input.session_id);
        if (input.artifacts?.['document-map.json']) {
          work.revision_id = sha(JSON.stringify(input.artifacts)); work.revision_number++;
          work.stages = [{ stage: 'MAP', artifact_sha256: input.artifacts['document-map.json'].artifact_sha256 }];
        }
        send(work); return;
      }
      const work = works.get(req.url.split('/').at(-1));
      if (work) send({ ...work, ...(foreignWork ? { session_id: 'plugin-' + 'f'.repeat(32) } : {}) });
      else { res.writeHead(404); res.end(); }
    });
    server.listen(port, '127.0.0.1'); await once(server, 'listening');
    runtime.snapshot = { status: 'READY', mode: 'managed', owned: true, instanceId: runtime.token,
      serviceOrigin: 'http://127.0.0.1:' + server.address().port };
    await writeFile(join(root, 'api-instance.json'), JSON.stringify({ instanceId: runtime.token, pid: process.pid, origin: runtime.snapshot.serviceOrigin }));
  };
  await listen();
  let launcher = new TaskLauncher(runtime, { taskMcp: true }, () => services); launcher.ready = true; launcher.capabilityRoot = capabilityRoot; launcher.bundleSha = bundleSha;
  const input = { commandId, projectId: 'p', documentId: 'd', sourceSha256: sourceSha, goal: 'Use this synthetic source only.' };
  const saved = async () => JSON.parse(await readFile(join(root, 'task-launches', commandId + '.json'), 'utf8'));
  const started = await launcher.start(input);
  let publication;
  if (populated) {
    const record = await saved();
    const script = `import sys,json
from pathlib import Path
from argparse import Namespace
sys.path.insert(0,sys.argv[1]+'/.reqpilot/scripts')
import reqpilot;reqpilot.load_runtime()
from reqpilotx.application.agent_workflow import task_workspace as core
import artifact_tools,workspace_tools
root=Path(sys.argv[2]);input=Path(sys.argv[3]);source_ir=Path(sys.argv[4])/'source_ir.json'
state=core.workspace_status(root,refresh=False);source=root/state['context']['source']['path']
operation=core.begin_operation(root,'compile-map',{'source_ir':source_ir,'input':input})
output=Path(operation['output_dir'])
result=artifact_tools.compile_map(Namespace(source=source,source_ir=source_ir,input=Path(operation['resolved_inputs']['input']),output_dir=output))
core.commit_operation(root,operation['operation_id'],result=result)
published=workspace_tools.publish_committed(root,operation['operation_id'],Namespace(command='compile-map',workspace=root,output_dir=output,open=False),result)
print(json.dumps({'operation_id':operation['operation_id'],'publication':published['workbench_sync']}))`;
    publication = JSON.parse((await runtime.command([python, '-c', script, record.prepared.workspace, record.prepared.task_root, sourceInfo.mapping, sourceInfo.capture], { PYTHONDONTWRITEBYTECODE: '1' })).trim().split('\n').at(-1));
    assert.equal(publication.publication.status, 'WORKBENCH_SYNCHRONIZED');
  }
  const control = (action, controlId = randomUUID()) => ({ commandId, controlId, action, sessionId: started.sessionId, workRef: started.workRef, sourceSha256: started.sourceSha256 });
  const transition = async (reusePort = false) => {
    const prior = runtime.snapshot.serviceOrigin;
    await new Promise(ok => server.close(ok)); await launcher.close(); await runtime.release();
    runtime = new ManagedRuntime(nativeSubprocess, { dataRoot: root, python }); await runtime.claim(); await listen(reusePort ? new URL(prior).port : 0);
    launcher = new TaskLauncher(runtime, { taskMcp: true }, () => services); launcher.ready = true; launcher.capabilityRoot = capabilityRoot; launcher.bundleSha = bundleSha;
    if (!reusePort) assert.notEqual(runtime.snapshot.serviceOrigin, prior);
    return prior;
  };
  return { root, sourceSha, started, events, works, calls, saved, control, transition, publication,
    get launcher() { return launcher; }, get runtime() { return runtime; },
    set changedSource(value) { changedSource = value; }, set foreignProof(value) { foreignProof = value; },
    set foreignWork(value) { foreignWork = value; }, set lostAck(value) { lostAck = value; },
    async close() { await launcher.close(); await new Promise(ok => server.close(ok)); await runtime.stop(); await rm(root, { recursive: true, force: true }); },
  };
}

test('owned lease/port transition reconnects one frozen task and a NEW continuation reads its feedback', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    const before = await f.saved(), writes = f.calls.writes;
    const prior = await f.transition();
    assert.equal((await f.launcher.inspect(before.commandId)).controlCapabilities.continue, false);
    assert.equal((await f.launcher.inspect(before.commandId)).controlCapabilities.reconnect, true);
    await assert.rejects(f.launcher.toolBinding(f.started.sessionId), /RECONNECT_REQUIRED/);
    await assert.rejects(f.launcher.control(f.control('continue')), /INTERRUPTED/);
    f.works.get(f.started.workRef).feedback.push({ feedback_id: 'human-feedback', text: 'Check the changed condition.', baseline_sha256: null });
    const reconnected = await f.launcher.control(f.control('reconnect'));
    assert.equal(reconnected.controlCapabilities.continue, true);
    const after = await f.saved();
    assert.equal(after.reconnectEvidence.fromOrigin, prior); assert.equal(after.serviceOrigin, f.runtime.snapshot.serviceOrigin);
    for (const key of ['applicationId', 'sessionId', 'workspaceId', 'bundleSha', 'sourceSha256', 'workspaceRelative']) assert.equal(after[key], before[key]);
    assert.deepEqual(after.prepared, before.prepared);
    const control = f.control('continue'); await f.launcher.control(control); await f.launcher.control(control);
    assert.equal(f.calls.prompt.length, 2); assert.equal(f.calls.create, 1); assert.equal(f.calls.workspace, 1); assert.equal(f.calls.writes, writes);
    assert.equal(f.calls.resume[0].resume.status, 'RESUMED'); assert.equal(f.calls.resume[0].resume.task_id, before.prepared.task_id);
    assert.equal(new URL(f.calls.resume[0].resume.workbench_url).origin, f.runtime.snapshot.serviceOrigin);
    assert.equal(f.calls.resume[0].feedback.sync.feedback[0].feedback_id, 'human-feedback');
    assert.deepEqual(await f.launcher.toolBinding(f.started.sessionId), before.prepared.tools.mcp);
  } finally { await f.close(); }
});

test('lost-ack continuation reconnects from durable proof without replay, then accepts another NEW continuation', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    const pending = f.control('continue'); f.lostAck = true;
    await assert.rejects(f.launcher.control(pending), /ack lost/); assert.equal(f.calls.prompt.length, 2);
    await f.transition(); f.lostAck = false;
    await f.launcher.control(f.control('reconnect'));
    assert.equal((await f.launcher.control(pending)).control.status, 'ACCEPTED'); assert.equal(f.calls.prompt.length, 2);
    await f.launcher.control(f.control('continue')); assert.equal(f.calls.prompt.length, 3);
  } finally { await f.close(); }
});

test('unknown prompt outcome prevents reconnect and new dispatch', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    const pending = f.control('continue'); f.lostAck = true;
    await assert.rejects(f.launcher.control(pending), /ack lost/);
    const requestId = f.calls.prompt.at(-1).requestId;
    f.events.splice(f.events.findIndex(value => value.data?.source?.rpcId === requestId), 1);
    const origin = await f.transition();
    await assert.rejects(f.launcher.control(f.control('reconnect')), /MUTATION_OUTCOME_UNKNOWN/);
    assert.equal((await f.saved()).serviceOrigin, origin); assert.equal(f.calls.prompt.length, 2);
  } finally { await f.close(); }
});

for (const [name, sabotage, expected] of [
  ['foreign listener even when it copies public owner and instance', async f => { f.foreignProof = true; }, /ENDPOINT_UNTRUSTED/],
  ['replaced service source', async f => { f.changedSource = true; }, /SOURCE_CHANGED/],
  ['unknown remote save outcome with divergent revision', async f => { f.works.get(f.started.workRef).revision_id = 'e'.repeat(64); }, /MUTATION_OUTCOME_UNKNOWN/],
  ['foreign work receipt', async f => { f.foreignWork = true; }, /NOT_CONFIRMED/],
  ['missing Pair Session metadata', async f => { const inspect = f.launcher.services().sessions.inspect; f.launcher.services().sessions.inspect = async id => ({ events: (await inspect(id)).events }); }, /SESSION_MISMATCH/],
  ['foreign Pair Session identity', async f => { const inspect = f.launcher.services().sessions.inspect; f.launcher.services().sessions.inspect = async id => ({ ...await inspect(id), session: { sessionId: 'foreign', workspaceId: f.started.workspaceId } }); }, /SESSION_MISMATCH/],
  ['tampered runtime lease', async f => { await writeFile(join(f.root, 'runtime.lock'), JSON.stringify({ pid: process.pid, token: randomUUID() })); }, /ENDPOINT_UNTRUSTED/],
  ['tampered API descriptor', async f => { await writeFile(join(f.root, 'api-instance.json'), JSON.stringify({ pid: process.pid, instanceId: randomUUID(), origin: f.runtime.snapshot.serviceOrigin })); }, /ENDPOINT_UNTRUSTED/],
  ['foreign Pair Session workspace', async f => { const inspect = f.launcher.services().sessions.inspect; f.launcher.services().sessions.inspect = async id => ({ ...await inspect(id), session: { sessionId: id, workspaceId: 'foreign' } }); }, /SESSION_MISMATCH/],
  ['shadow package with otherwise unchanged declared files', async f => { const record = await f.saved(); const shadow = join(record.prepared.workspace, '.reqpilot/scripts/reqpilot'); await mkdir(shadow); await writeFile(join(shadow, '__init__.py'), `raise RuntimeError('UNTRUSTED_SHADOW_EXECUTED')`); }, /PACKAGE_MISMATCH/],
  ['sourceless shadow package', async f => { const record = await f.saved(); const shadow = join(record.prepared.workspace, '.reqpilot/scripts/reqpilot'); await mkdir(shadow); const poison = join(f.root, 'sourceless.py'); await writeFile(poison, `raise RuntimeError('UNTRUSTED_SOURCELESS_EXECUTED')`); await f.runtime.command([python, '-c', 'import py_compile,sys;py_compile.compile(sys.argv[1],cfile=sys.argv[2])', poison, join(shadow, '__init__.pyc')]); }, /PACKAGE_MISMATCH/],
  ['tampered fixed package', async f => { const record = await f.saved(); await writeFile(join(record.prepared.workspace, '.reqpilot/scripts/reqpilot.py'), '# tampered'); }, /PACKAGE_MISMATCH/],
  ['tampered local original', async f => { const record = await f.saved(); const inputs = JSON.parse(await readFile(join(record.prepared.workspace, 'runtime-data/input-manifest.json'), 'utf8')); await writeFile(join(record.prepared.workspace, inputs.inputs[0].path), 'changed'); }, /SOURCE_CHANGED/],
  ['unknown local business mutation', async f => { const record = await f.saved(); await writeFile(join(record.prepared.task_root, 'operations/op-' + 'a'.repeat(32) + '.json'), JSON.stringify({ state: 'STARTED' })); }, /MUTATION_OUTCOME_UNKNOWN/],
]) test('reconnect rejects ' + name, { skip: !enabled }, async () => {
  const f = await fixture(); try {
    const origin = await f.transition(); await sabotage(f);
    await assert.rejects(f.launcher.control(f.control('reconnect')), expected);
    assert.equal((await f.saved()).serviceOrigin, origin); assert.equal(f.calls.prompt.length, 1); assert.equal(f.calls.writes, 1);
  } finally { await f.close(); }
});


test('same-port replacement still needs generation proof and a second reconnect has a fresh identity', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    const origin = await f.transition(true);
    assert.equal(f.runtime.snapshot.serviceOrigin, origin);
    assert.equal((await f.launcher.inspect(f.started.commandId)).controlCapabilities.continue, false);
    await assert.rejects(f.launcher.toolBinding(f.started.sessionId), /RECONNECT_REQUIRED/);
    const first = f.control('reconnect'); await f.launcher.control(first);
    const firstInstance = (await f.saved()).serviceInstanceId;
    await f.transition(true);
    assert.notEqual(f.runtime.snapshot.instanceId, firstInstance);
    await assert.rejects(f.launcher.control(first), /GENERATION_CONFLICT/);
    await f.launcher.control(f.control('reconnect'));
    await f.launcher.control(f.control('continue'));
    assert.equal(f.calls.prompt.length, 2); assert.equal((await f.saved()).serviceInstanceId, f.runtime.snapshot.instanceId);
  } finally { await f.close(); }
});

for (const kind of ['running', 'retry', 'unobserved']) test('reconnect refuses an open ' + kind + ' turn', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    await f.transition();
    f.events.push({ type: 'turn/start', data: { turn: 2 } });
    if (kind === 'retry') f.events.push({ type: 'llm/retry', data: { delayMs: 50 } });
    if (kind !== 'unobserved') f.launcher.services().liveStatus = () => 'running';
    await assert.rejects(f.launcher.control(f.control('reconnect')), /SESSION_RUNNING/);
    assert.equal(f.calls.prompt.length, 1);
  } finally { await f.close(); }
});


test('reconnect ignores poisoned bytecode and imports only verified source', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    await f.transition(); const record = await f.saved();
    const source = join(record.prepared.workspace, '.reqpilot/scripts/reqpilot.py');
    const poison = join(f.root, 'poison.py');
    await writeFile(poison, "raise RuntimeError('POISONED_CACHE_EXECUTED')\n");
    await f.runtime.command([python, '-c', "import importlib.util,py_compile,sys; from pathlib import Path; source=Path(sys.argv[1]); target=importlib.util.cache_from_source(str(source)); py_compile.compile(sys.argv[2],cfile=target,invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)", source, poison]);
    await f.launcher.control(f.control('reconnect'));
    assert.equal((await f.saved()).serviceOrigin, f.runtime.snapshot.serviceOrigin); assert.equal(f.calls.prompt.length, 1);
  } finally { await f.close(); }
});


test('crash after helper binding commit recovers the exact trusted intermediate on another lease', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    const original = await f.transition(); const intermediate = f.runtime.snapshot.serviceOrigin;
    const save = f.launcher.save.bind(f.launcher);
    f.launcher.save = async record => { if (record.serviceOrigin === intermediate) throw Error('HOST_CRASH_AFTER_BINDING_COMMIT'); return save(record); };
    await assert.rejects(f.launcher.control(f.control('reconnect')), /HOST_CRASH_AFTER_BINDING_COMMIT/);
    const interrupted = await f.saved(); assert.equal(interrupted.serviceOrigin, original);
    assert.equal(interrupted.reconnectIntent.targetOrigin, intermediate);
    const context = JSON.parse(await readFile(join(interrupted.prepared.task_root, 'task-context.json'), 'utf8'));
    const binding = JSON.parse(await readFile(join(interrupted.prepared.task_root, context.binding_file), 'utf8'));
    assert.equal(binding.base_url, intermediate);
    await f.transition(); await f.launcher.control(f.control('reconnect'));
    assert.equal((await f.saved()).reconnectIntent, undefined);
    await f.launcher.control(f.control('continue'));
    assert.equal(f.calls.prompt.length, 2); assert.equal(f.calls.writes, 1); assert.equal(f.calls.create, 1);
    assert.equal(f.calls.resume[0].resume.task_id, interrupted.prepared.task_id);
  } finally { await f.close(); }
});


test('owned loopback reconnect ignores hostile inherited HTTP proxy configuration', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    await f.transition(); const command = f.runtime.command.bind(f.runtime);
    f.runtime.command = (argv, env, options) => command(argv, { ...env, HTTP_PROXY: 'http://192.0.2.1:9', http_proxy: 'http://192.0.2.1:9', NO_PROXY: '', no_proxy: '' }, options);
    await f.launcher.control(f.control('reconnect'));
    assert.equal((await f.saved()).serviceOrigin, f.runtime.snapshot.serviceOrigin); assert.equal(f.calls.writes, 1);
  } finally { await f.close(); }
});


test('populated saved revision and genuine committed publication receipts survive trusted reconnect', { skip: !enabled }, async () => {
  const f = await fixture(true); try {
    const record = await f.saved(), work = structuredClone(f.works.get(f.started.workRef)), writes = f.calls.writes;
    assert.match(work.revision_id, /^[a-f0-9]{64}$/); assert.equal(work.stages[0].stage, 'MAP');
    await f.transition(); await f.launcher.control(f.control('reconnect')); await f.launcher.control(f.control('continue'));
    assert.equal(f.calls.resume[0].resume.task_id, record.prepared.task_id);
    assert.equal(f.calls.resume[0].resume.workbench_link.result_revision, work.revision_id);
    assert.deepEqual(f.works.get(f.started.workRef), work); assert.equal(f.calls.writes, writes);
  } finally { await f.close(); }
});

for (const status of ['NOT_SYNCHRONIZED', 'UNRECOGNIZED_FUTURE_STATUS']) test('populated task refuses unresolved publication receipt ' + status, { skip: !enabled }, async () => {
  const f = await fixture(true); try {
    const record = await f.saved();
    const script = "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]+'/.reqpilot/scripts');import reqpilot;reqpilot.load_runtime();from reqpilotx.application.agent_workflow import task_workspace as core;core.record_host_receipt(Path(sys.argv[2]),sys.argv[3],{'status':sys.argv[4]})";
    await f.runtime.command([python, '-c', script, record.prepared.workspace, record.prepared.task_root, f.publication.operation_id, status], { PYTHONDONTWRITEBYTECODE: '1' });
    const origin = await f.transition();
    await assert.rejects(f.launcher.control(f.control('reconnect')), /MUTATION_OUTCOME_UNKNOWN/);
    assert.equal((await f.saved()).serviceOrigin, origin); assert.equal(f.calls.prompt.length, 1);
  } finally { await f.close(); }
});


test('a Session that starts during reconnect proof cannot commit host binding or dispatch continuation', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    const origin = await f.transition();
    const inspect = f.launcher.services().sessions.inspect; let count = 0;
    f.launcher.services().sessions.inspect = async id => {
      count++; if (count > 1) f.events.push({ type: 'turn/start', data: { turn: 2 } });
      return inspect(id);
    };
    await assert.rejects(f.launcher.control(f.control('reconnect')), /SESSION_CHANGED/);
    assert.equal((await f.saved()).serviceOrigin, origin); assert.ok((await f.saved()).reconnectIntent);
    assert.equal(f.calls.prompt.length, 1); assert.equal(f.calls.writes, 1);
  } finally { await f.close(); }
});


test('an unknown cancel outcome prevents reconnect', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    f.launcher.services().sessions.cancel = async () => { throw Error('cancel ack lost'); };
    await assert.rejects(f.launcher.control(f.control('cancel')), /cancel ack lost/); await f.transition();
    await assert.rejects(f.launcher.control(f.control('reconnect')), /MUTATION_OUTCOME_UNKNOWN/);
    assert.equal(f.calls.prompt.length, 1);
  } finally { await f.close(); }
});

test('concurrent retries share one reconnect operation and never dispatch a model turn', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    await f.transition(); const input = f.control('reconnect');
    const [a, b] = await Promise.all([f.launcher.control(input), f.launcher.control(input)]);
    assert.deepEqual(a, b); assert.equal(f.calls.prompt.length, 1); assert.equal(f.calls.writes, 1);
  } finally { await f.close(); }
});

test('binding-to-task lock order allows an overlapping durable pull to finish', { skip: !enabled }, async () => {
  const f = await fixture(); try {
    await f.transition(); const record = await f.saved(), gate = join(f.root, 'allow-task-lock');
    const script = `import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1]+'/.reqpilot/scripts');import reqpilot;reqpilot.load_runtime()
from reqpilotx.application.agent_workflow import task_workspace as core
from revision_tools import locked
root=Path(sys.argv[2]);state=core.workspace_status(root,refresh=False)
with locked(Path(state['binding_file']).parent):
    print('HOLDING_BINDING',flush=True)
    while not Path(sys.argv[3]).exists():time.sleep(.01)
    with core._locked(root):print('ACQUIRED_TASK',flush=True)
`;
    const holder = f.runtime.spawn([python, '-c', script, record.prepared.workspace, record.prepared.task_root, gate]);
    for (let i = 0; i < 100 && !holder.collected.stdout.readFrom(0).text.includes('HOLDING_BINDING'); i++) await new Promise(ok => setTimeout(ok, 20));
    assert.match(holder.collected.stdout.readFrom(0).text, /HOLDING_BINDING/);
    const reconnecting = f.launcher.control(f.control('reconnect'));
    await new Promise(ok => setTimeout(ok, 1000)); await writeFile(gate, 'go');
    assert.equal(await holder.waitForExit(AbortSignal.timeout(5000)), true);
    assert.match(holder.collected.stdout.readFrom(0).text, /ACQUIRED_TASK/);
    await reconnecting; assert.equal(f.calls.prompt.length, 1);
  } finally { await f.close(); }
});
