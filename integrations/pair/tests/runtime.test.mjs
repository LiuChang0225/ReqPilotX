import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ManagedRuntime } from '../host/src/managed-runtime.js';

test('exclusive owned directory refuses a second live owner and preserves data on release', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-runtime-test-'));
  const first = new ManagedRuntime({}, { dataRoot: root });
  const second = new ManagedRuntime({}, { dataRoot: root });
  try {
    await first.claim(); await writeFile(join(root, 'customer-data'), 'keep');
    await assert.rejects(second.claim(), /already in use/);
    await first.release(); assert.equal(await readFile(join(root, 'customer-data'), 'utf8'), 'keep');
    await second.claim(); await second.release();
  } finally { await rm(root, { recursive: true, force: true }); }
});
test('unknown nonempty directory is never appropriated', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-runtime-test-'));
  try { await writeFile(join(root, 'user-file'), 'keep'); const runtime = new ManagedRuntime({}, { dataRoot: root }); await assert.rejects(runtime.claim(), /not an empty/); assert.equal(await readFile(join(root, 'user-file'), 'utf8'), 'keep'); }
  finally { await rm(root, { recursive: true, force: true }); }
});
test('ready shutdown stops API before database and retains lock if quiescence fails', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-runtime-test-'));
  try {
    const runtime = new ManagedRuntime({}, { dataRoot: root }); await runtime.claim(); runtime.snapshot.status = 'READY'; const events = [];
    runtime.handles = ['database', 'api'].map(name => ({ terminate() { events.push(name); }, async waitForExit() { return true; } }));
    await runtime.stop(); assert.deepEqual(events, ['api', 'database']); assert.equal(runtime.claimed, false);
    const failing = new ManagedRuntime({}, { dataRoot: root }); await failing.claim(); failing.snapshot.status = 'READY'; failing.handles = [{ terminate() {}, async waitForExit() { return false; } }];
    await assert.rejects(failing.stop(), /lock retained/); assert.equal(JSON.parse(await readFile(join(root, 'runtime.lock'), 'utf8')).token, failing.token); await failing.release();
  } finally { await rm(root, { recursive: true, force: true }); }
});
