import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import discovery from '../host/src/application-discovery.cjs';
import { ManagedRuntime } from '../host/src/managed-runtime.js';
const ownerId = '12345678-1234-4234-8234-123456789abc';
const ready = { mode: 'managed', status: 'READY', ownerId, instanceId: 'process-one', serviceOrigin: 'http://127.0.0.1:19001' };

test('application identity survives real lease release/reclaim and changes of port/process', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-discovery-'));
  try {
    const first = new ManagedRuntime({}, { dataRoot: root }); await first.claim();
    const before = discovery.publicConfiguration({ ...ready, ownerId: first.ownerId }); await first.release();
    const second = new ManagedRuntime({}, { dataRoot: root }); await second.claim();
    const after = discovery.publicConfiguration({ ...ready, ownerId: second.ownerId, instanceId: 'process-two', serviceOrigin: 'http://127.0.0.1:19002' }); await second.release();
    assert.equal(before.applicationId, after.applicationId); assert.notEqual(before.instanceId, after.instanceId);
    const a = discovery.connectionIdentity(before.applicationId, before.serviceOrigin), b = discovery.connectionIdentity(after.applicationId, after.serviceOrigin);
    assert.equal(a.hostId, b.hostId); assert.notEqual(a.clientId, b.clientId);
  } finally { await rm(root, { recursive: true, force: true }); }
});
test('public config never includes runtime credentials, paths, package descriptors or private failure text', () => {
  const publicValue = discovery.publicConfiguration({ ...ready, secret: 'sentinel-secret', dataRoot: '/private/sentinel-root', installation: { path: '/private/sentinel-env' }, capabilities: { 'agent.start': true } });
  assert.equal(publicValue.capabilities['agent.start'], false);
  assert.doesNotMatch(JSON.stringify(publicValue), /sentinel/);
  const failed = discovery.publicConfiguration({ ...ready, status: 'ERROR', message: 'sentinel-secret in failure' });
  assert.doesNotMatch(JSON.stringify(failed), /sentinel|19001/);
  assert.ok(Object.values(failed.capabilities).every(value => value === false));
});
test('transport availability is not agent execution or semantic acceptance', () => {
  const available = discovery.capabilities(ready);
  assert.equal(available['projects.create'], true); assert.equal(available['documents.import'], true);
  assert.equal(available['results.feedback'], true); assert.equal(available['results.confirm_map'], true);
  for (const key of ['agent.start', 'agent.resume', 'agent.cancel', 'events.agent']) assert.equal(available[key], false);
  const connected = discovery.publicConfiguration({ ...ready, mode: 'readonly-connect' });
  assert.equal(connected.applicationId, null); assert.equal(connected.capabilities['projects.create'], false);
  assert.equal(connected.capabilities['results.feedback'], false);
  const work = '/reqpilotx/service/api/projects/p/sor-documents/d/agent-structure/delivery/plugin-workbench/plugin-' + 'a'.repeat(32);
  assert.equal(discovery.allowsWrite(ready, 'POST', work + '/feedback'), true);
  assert.equal(discovery.allowsWrite(ready, 'POST', work + '/confirm-map'), true);
  assert.equal(discovery.allowsWrite({ ...ready, mode: 'readonly-connect' }, 'POST', work + '/feedback'), false);
  assert.equal(discovery.allowsWrite(ready, 'POST', work + '/export'), false);
  assert.equal(discovery.allowsWrite({ ...ready, status: 'STARTING' }, 'POST', '/reqpilotx/service/api/projects'), true); // handler reports 503, not a false method denial
  assert.equal(discovery.allowsWrite(ready, 'POST', '/reqpilotx/service/api/projects/p/sor-documents/d/tasks'), false);
});
test('client fails closed on incompatible protocol, missing capabilities/identity and credential-bearing endpoints', () => {
  const value = discovery.publicConfiguration(ready); assert.equal(discovery.assertCompatibleConfiguration(value), value);
  assert.throws(() => discovery.assertCompatibleConfiguration({ ...value, schema: 'future/v2' }), /INCOMPATIBLE/);
  assert.throws(() => discovery.assertCompatibleConfiguration({ ...value, capabilities: {} }), /INCOMPATIBLE/);
  assert.throws(() => discovery.assertCompatibleConfiguration({ ...value, applicationId: null }), /IDENTITY/);
  assert.throws(() => discovery.assertCompatibleConfiguration({ ...value, serviceOrigin: 'https://user:secret@127.0.0.1' }), /ENDPOINT/);
});
test('human responses and immutable saves are limited to managed session-bound routes', () => {
  const work = '/reqpilotx/service/api/projects/p/sor-documents/d/agent-structure/delivery/plugin-workbench/plugin-' + 'a'.repeat(32);
  const capabilities = discovery.capabilities(ready);
  assert.equal(capabilities['results.engineer_responses'], true);
  assert.equal(capabilities['results.save_version'], true);
  for (const suffix of ['/engineer-responses', '/versions']) {
    assert.equal(discovery.allowsWrite(ready, 'POST', work + suffix), true);
    assert.equal(discovery.allowsWrite({ ...ready, mode: 'readonly-connect' }, 'POST', work + suffix), false);
    assert.equal(discovery.allowsWrite(ready, 'DELETE', work + suffix), false);
    assert.equal(discovery.allowsWrite(ready, 'POST', work.replace(/a{32}/, 'wrong-session') + suffix), false);
  }
  assert.equal(discovery.allowsWrite(ready, 'POST', work + '/versions/1'), false);
  assert.equal(discovery.allowsWrite(ready, 'POST', work + '/versions/preview'), false);
});


test('Client requires every human-operation flag and embeds the same exact-path policy as Host', () => {
  const value = discovery.publicConfiguration(ready);
  for (const operation of ['results.engineer_responses', 'results.save_version']) {
    const flags = { ...value.capabilities }; delete flags[operation];
    assert.throws(() => discovery.assertCompatibleConfiguration({ ...value, capabilities: flags }), /INCOMPATIBLE/);
    assert.throws(() => discovery.assertCompatibleConfiguration({ ...value, capabilities: { ...value.capabilities, [operation]: 'true' } }), /INCOMPATIBLE/);
  }
  const root = '/api/projects/p/sor-documents/d/agent-structure/delivery/plugin-workbench/plugin-' + 'a'.repeat(32);
  for (const [suffix, operation] of Object.entries({ feedback:'results.feedback', 'confirm-map':'results.confirm_map', 'engineer-responses':'results.engineer_responses', versions:'results.save_version' })) {
    assert.equal(discovery.resultWriteOperation('POST', root + '/' + suffix), operation);
    assert.equal(discovery.resultWriteOperation('DELETE', root + '/' + suffix), null);
    assert.equal(discovery.resultWriteOperation('POST', root + '/' + suffix + '/extra'), null);
    assert.equal(discovery.resultWriteOperation('POST', root + '/' + suffix + '?revision=older'), null);
  }
});
