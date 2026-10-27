import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { randomUUID } from 'node:crypto';

async function fixture(overrideTransport) {
  const source = await readFile(new URL('../client/src/task-controller.cjs', import.meta.url), 'utf8');
  const data = new Map(), requests = [], scope = { projectId: 'p', documentId: 'd', sourceSha256: 'a'.repeat(64) };
  const intent = { ...scope, commandId: randomUUID(), goal: 'original task', requireMapConfirmation: false };
  const receipt = { schema: 'reqpilot-pair-launch/v1', ...intent, status: 'ACCEPTED', applicationId: 'app',
    sessionId: 'session-one', workspaceId: 'workspace-one', workRef: 'plugin-' + 'b'.repeat(32) };
  const module = { exports: {} };
  runInNewContext(source, { module, crypto: { randomUUID }, URLSearchParams, TextEncoder,
    localStorage: { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) } });
  const transport = async (url, options) => {
    requests.push({ url, options });
    if (overrideTransport) return overrideTransport(url, options);
    return new Response(JSON.stringify(url.includes('/tasks?') ? { tasks: [{ ...receipt, request: intent }] } : receipt));
  };
  const controller = module.exports.createPairTaskController({ applicationId: 'app', workbench: { taskLookupAvailable: true }, capabilities: { 'agent.start': true } }, async () => {}, transport);
  return { controller, scope, intent, receipt, requests, data };
}

test('a new browser origin recovers the persisted task instead of submitting another model prompt', async () => {
  const f = await fixture();
  const value = await f.controller.startTask({ ...f.scope, goal: 'original task' });
  assert.equal(value.commandId, f.intent.commandId);
  assert.equal(f.requests.filter(request => request.options?.method === 'POST').length, 0);
  assert.equal(f.data.size, 1);
});
test('work selection looks up its own persisted Session rather than borrowing the previous work binding', async () => {
  const f = await fixture();
  await f.controller.restoreTask(f.scope);
  await f.controller.restoreTask({ ...f.scope, workRef: f.receipt.workRef });
  assert.ok(f.requests.some(request => request.url.includes('workRef=' + f.receipt.workRef)));
});
test('a stored intention for another source refuses both recovery and new task submission', async () => {
  const f = await fixture();
  await f.controller.restoreTask(f.scope);
  const [key] = f.data.keys();
  f.data.set(key, JSON.stringify({ ...f.intent, documentId: 'another-document' }));
  const before = f.requests.length;
  await assert.rejects(f.controller.startTask({ ...f.scope, goal: 'original task' }), /当前文档不一致/);
  assert.equal(f.requests.length, before);
});
test('an oversized multibyte goal refuses submission before retaining a new command', async () => {
  const f = await fixture();
  await assert.rejects(f.controller.startTask({ ...f.scope, goal: '需求'.repeat(6000) }), /任务目标过长/);
  assert.equal(f.requests.filter(request => request.options?.method === 'POST').length, 0);
  const stored = JSON.parse([...f.data.values()][0]); assert.equal(stored.commandId, f.intent.commandId);
});

test('an invalidated observation cannot replace the current durable intention or read the old Session', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const f = await fixture(async url => url.includes('/tasks?') ? pending : Response.json(f.receipt));
  const abort = new AbortController();
  const observed = f.controller.restoreTask({ ...f.scope, workRef: f.receipt.workRef }, { signal: abort.signal });
  const rejected = assert.rejects(observed, { name: 'AbortError' });
  abort.abort();
  const current = { ...f.intent, commandId: randomUUID(), goal: 'the newly selected task' };
  const key = 'reqpilotx:intent:app:p:d:' + f.scope.sourceSha256;
  f.data.set(key, JSON.stringify(current));
  release(Response.json({ tasks: [{ ...f.receipt, request: f.intent }] }));
  await rejected;
  assert.deepEqual(JSON.parse(f.data.get(key)), current);
  assert.equal(f.requests.length, 1);
});

test('an already invalidated observation performs no request', async () => {
  const f = await fixture(), abort = new AbortController(); abort.abort();
  await assert.rejects(f.controller.restoreTask(f.scope, { signal: abort.signal }), { name: 'AbortError' });
  assert.equal(f.requests.length, 0); assert.equal(f.data.size, 0);
});
