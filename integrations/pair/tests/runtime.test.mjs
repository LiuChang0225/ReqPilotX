import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ManagedRuntime } from '../host/src/managed-runtime.js';

test('installed runtime configuration rejects checkout fallback and absent paths before starting processes', async () => {
  for (const config of [{}, {appRoot:'/checkout'}, {python:'python'}, {python:'/installed/python'}, {python:'/missing/python',frontendRoot:'/missing/frontend'}]) {
    const runtime=new ManagedRuntime({spawn(){assert.fail('must not spawn');}},config);
    await runtime.start();assert.equal(runtime.snapshot.status,'ERROR');assert.equal(runtime.handles.length,0);
  }
});
test('relative data roots and symlink data roots are rejected without claiming them', async () => {
  await assert.rejects(new ManagedRuntime({}, {dataRoot:'relative'}).claim(), /absolute/);
  const root=await mkdtemp(join(tmpdir(),'reqpilot-invalid-root-'));
  try {const alias=root+'-link';await symlink(root,alias);try {await assert.rejects(new ManagedRuntime({}, {dataRoot:alias}).claim(),/symlink/);}finally{await rm(alias);}}
  finally{await rm(root,{recursive:true,force:true});}
});

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
