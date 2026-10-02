import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import vm from 'node:vm';
const pair = process.env.PAIR_REPO;
const fixture = process.env.PAIR_REACT_ROOT;
if (!pair && !fixture) throw Error('Set PAIR_REPO or PAIR_REACT_ROOT to an explicit React 18 host fixture.');
const hostRequire = createRequire(pair ? pair + '/packages/client/web/package.json' : fixture + '/package.json');
test('built client is lazy and uses only the shared host React modules; registers/disposes panel effects', async () => {
  const code = await readFile(new URL('../client/lib/client.js', import.meta.url), 'utf8');
  const calls = []; const styles = []; const disposers = []; const registrations = []; let entry;
  const document = { createElement() { return { dataset: {}, textContent: '', remove() { styles.splice(styles.indexOf(this), 1); } }; }, head: { append(style) { styles.push(style); } } };
  vm.runInNewContext(code, { window: { __ModuleLoader__: { load(value) { entry = value; } } }, document, URL, console });
  assert.equal(entry.id, '@reqpilotx/dsh-pair-client'); assert.equal(calls.length, 0); assert.equal(styles.length, 0);
  const plugin = entry.factory(id => { calls.push(id); assert.ok(['react', 'react/jsx-runtime', 'react-dom'].includes(id)); return hostRequire(id); });
  plugin.apply({ effect(fn) { disposers.push(fn()); }, locale: { register() { return () => {}; } }, slots: { inject(_name, fn) { disposers.push(fn()); }, register(options, Component) { registrations.push({ options, Component }); return () => {}; } } });
  assert.equal(styles.length, 1);
  assert.deepEqual(registrations.map(x => [x.options.name, x.options.key || x.options.id]), [['main', 'reqpilotx'], ['sidebar.panellist', 'reqpilotx']]);
  for (const dispose of disposers.reverse()) if (typeof dispose === 'function') dispose();
  assert.equal(styles.length, 0); assert.equal(hostRequire('react/package.json').version, '18.3.1');
});

test('selected source context is inserted with draft revision guard, never auto-submitted or silently dropped', async () => {
  let entry, main, input = { phase: 'plain', draft: 'Existing user text', draftRev: 7 }; const insertions = [], navigations = [];
  vm.runInNewContext(await readFile(new URL('../client/lib/client.js', import.meta.url), 'utf8'), {
    window: { __ModuleLoader__: { load(value) { entry = value; } } },
    document: { createElement() { return { dataset: {}, remove() {} }; }, head: { append() {} } }, URL,
  });
  const plugin = entry.factory(hostRequire);
  const scope = { bail(_scope, event, payload) { insertions.push({ event, payload }); return true; } };
  plugin.apply({ effect(fn) { fn(); }, locale: { register() { return () => {}; } },
    slots: { inject(_name, fn) { fn(); }, register(options) { if (options.name === 'main') main = options; return () => {}; } },
    layout: { beginNavigation() { return new AbortController().signal; }, selectPanel(value) { navigations.push(value); } },
    sessions: { async open(id) { assert.equal(id, 'session-bound'); }, scope() { return scope; } },
    conversation: { input: { for() { return { state: { getSnapshot() { return input; } } }; } } },
  });
  const context = { projectId: 'p', documentId: 'd', sourceSha256: 'a'.repeat(64), workRef: 'plugin-work', revisionId: 'b'.repeat(64), scope: 'selected', sourceItemId: 'source-item', ignoredPrivateField: '/private/path' };
  await main.inject().openSession('session-bound', context);
  assert.equal(insertions.length, 1); assert.equal(insertions[0].event, 'slash/input-insert-text');
  assert.equal(insertions[0].payload.span.draftRev, 7); assert.equal(insertions[0].payload.span.end, 0);
  assert.match(insertions[0].payload.text, /source-item/); assert.match(insertions[0].payload.text, /revisionId/); assert.doesNotMatch(insertions[0].payload.text, /private/);
  assert.equal(input.draft, 'Existing user text'); assert.deepEqual(navigations, [null]);
  input = { ...input, phase: 'submitting' };
  await assert.rejects(main.inject().openSession('session-bound', context), /稍后/);
  assert.equal(insertions.length, 1);
});

test('managed workbench writes only explicitly discovered session-bound operations', async () => {
  let entry; const calls = [];
  vm.runInNewContext(await readFile(new URL('../client/lib/client.js', import.meta.url), 'utf8'), {
    window: { __ModuleLoader__: { load(value) { entry = value; } }, location: { origin: 'http://pair.local' } },
    URL, Headers, FormData, fetch: async (url, init) => { calls.push({ url, method: init.method, credentials: init.credentials }); return new Response('{}', { headers: { 'content-type': 'application/json' } }); },
  });
  const plugin = entry.factory(hostRequire), origin = 'http://127.0.0.1:9000';
  const host = plugin.makePairWorkbenchHost(origin, () => {}, 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc', { 'results.feedback': true, 'results.confirm_map': true, 'results.engineer_responses': true, 'results.save_version': true });
  const path = '/api/projects/p/sor-documents/d/agent-structure/delivery/plugin-workbench/plugin-' + 'a'.repeat(32);
  assert.equal(host.feedbackWrites, true);
  await host.client.request(path + '/feedback', { method: 'POST', body: '{"text":"review"}' });
  await host.client.request(path + '/confirm-map', { method: 'POST', body: '{"confirmed":true}' });
  await host.client.request(path + '/engineer-responses', { method: 'POST', body: '{"expected_version":"cas"}' });
  await host.client.request(path + '/versions', { method: 'POST', body: '{"expected_version":"cas"}' });
  assert.deepEqual(calls.map(value => [value.url, value.method, value.credentials]), [
    ['/reqpilotx/service' + path + '/feedback', 'POST', 'same-origin'],
    ['/reqpilotx/service' + path + '/confirm-map', 'POST', 'same-origin'],
    ['/reqpilotx/service' + path + '/engineer-responses', 'POST', 'same-origin'],
    ['/reqpilotx/service' + path + '/versions', 'POST', 'same-origin'],
  ]);
  assert.throws(() => host.client.request(path + '/export', { method: 'POST' }), /不允许/);
  assert.throws(() => host.client.request(path + '/feedback', { method: 'DELETE' }), /不允许/);
  const connected = plugin.makePairWorkbenchHost(origin, () => {}, null, false);
  await assert.rejects(connected.client.request(path + '/feedback', { method: 'POST' }), /只读/);
  assert.equal(calls.length, 4);
});


test('managed operation flags cannot grant one another or enable a connect-mode host', async () => {
  let entry; const calls = [];
  vm.runInNewContext(await readFile(new URL('../client/lib/client.js', import.meta.url), 'utf8'), {
    window: { __ModuleLoader__: { load(value) { entry = value; } }, location: { origin: 'http://pair.local' } }, URL, Headers, FormData,
    fetch: async (url) => { calls.push(url); return new Response('{}', { headers: { 'content-type': 'application/json' } }); },
  });
  const plugin = entry.factory(hostRequire), origin = 'http://127.0.0.1:9000', id = 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc';
  const path = '/api/projects/p/sor-documents/d/agent-structure/delivery/plugin-workbench/plugin-' + 'a'.repeat(32);
  const flags = { 'results.feedback': true, 'results.engineer_responses': false, 'results.save_version': true };
  const host = plugin.makePairWorkbenchHost(origin, () => {}, id, flags);
  assert.equal(host.workCapabilities.save_engineer_response, false);
  await host.client.request(path + '/feedback', { method: 'POST' });
  await host.client.request(path + '/versions', { method: 'POST' });
  for (const suffix of ['/engineer-responses', '/confirm-map', '/versions/1', '/versions/preview', '/versions/1/export'])
    assert.throws(() => host.client.request(path + suffix, { method: 'POST' }), /不允许/);
  flags['results.engineer_responses'] = true;
  assert.throws(() => host.client.request(path + '/engineer-responses', { method: 'POST' }), /不允许/);
  const legacy = plugin.makePairWorkbenchHost(origin, () => {}, id, true);
  assert.throws(() => legacy.client.request(path + '/versions', { method: 'POST' }), /不允许/);
  const connect = plugin.makePairWorkbenchHost(origin, () => {}, null, flags);
  await assert.rejects(connect.client.request(path + '/versions', { method: 'POST' }), /只读/);
  assert.equal(calls.length, 2);
});
