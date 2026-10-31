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

function expectedHostServices(overrides = {}) {
  const navigations = [], insertions = [], using = []; let input = { phase: 'plain', draft: 'Existing user text', draftRev: 7 };
  const scope = { bail(_scope, event, payload) { insertions.push({ event, payload }); return true; } };
  const services = {
    uiWorkspace: { openSession(id) { navigations.push(id); } },
    layout: { beginNavigation() { return new AbortController().signal; }, selectPanel() {} },
    sessions: { async using(id, options, operation) {
      using.push({ id, options });
      return operation({ ready: Promise.resolve(), binding: { ctx: scope } });
    } },
    conversation: { input: { for() { return { state: { getSnapshot() { return input; } } }; } } },
    ...overrides,
  };
  return { services, navigations, insertions, using, scope, get input() { return input; }, set input(value) { input = value; } };
}

test('view-only navigation opens the same native Session without touching the composer', async () => {
  let entry, main;
  vm.runInNewContext(await readFile(new URL('../client/lib/client.js', import.meta.url), 'utf8'), {
    window: { __ModuleLoader__: { load(value) { entry = value; } } },
    document: { createElement() { return { dataset: {}, remove() {} }; }, head: { append() {} } }, URL,
  });
  let touched = false;
  const fake = expectedHostServices({ conversation: { input: { for() { touched = true; throw Error('must not prepare'); } } } });
  const plugin = entry.factory(hostRequire);
  plugin.apply({ effect(fn) { fn(); }, locale: { register() { return () => {}; } },
    slots: { inject(_name, fn) { fn(); }, register(options) { if (options.name === 'main') main = options; return () => {}; } },
    ...fake.services,
  });
  await main.inject().openSession('session-bound');
  assert.deepEqual(fake.navigations, ['session-bound']);
  assert.deepEqual(fake.insertions, []); assert.deepEqual(fake.using, []); assert.equal(touched, false);
});

test('selected discussion context uses documented scope retention, appends without replacing the draft, and never auto-submits', async () => {
  let entry, main;
  vm.runInNewContext(await readFile(new URL('../client/lib/client.js', import.meta.url), 'utf8'), {
    window: { __ModuleLoader__: { load(value) { entry = value; } } },
    document: { createElement() { return { dataset: {}, remove() {} }; }, head: { append() {} } }, URL,
  });
  const fake = expectedHostServices();
  const plugin = entry.factory(hostRequire);
  plugin.apply({ effect(fn) { fn(); }, locale: { register() { return () => {}; } },
    slots: { inject(_name, fn) { fn(); }, register(options) { if (options.name === 'main') main = options; return () => {}; } },
    ...fake.services,
  });
  const context = { projectId: 'p', documentId: 'd', sourceSha256: 'a'.repeat(64), workRef: 'plugin-work', revisionId: 'b'.repeat(64), scope: 'selected', sourceItemId: 'source-item', ignoredPrivateField: '/private/path' };
  await main.inject().openSession('session-bound', context);
  assert.deepEqual(fake.navigations, ['session-bound']);
  assert.equal(fake.using.length, 1); assert.equal(fake.using[0].id, 'session-bound'); assert.equal(fake.using[0].options.source, 'controllerOperation');
  assert.equal(fake.insertions.length, 1); assert.equal(fake.insertions[0].event, 'slash/input-insert-text');
  assert.equal(fake.insertions[0].payload.span.draftRev, 7); assert.equal(fake.insertions[0].payload.span.start, 'Existing user text'.length);
  assert.equal(fake.insertions[0].payload.span.end, 'Existing user text'.length); assert.match(fake.insertions[0].payload.text, /^\n\n讨论当前选中的原文/);
  assert.match(fake.insertions[0].payload.text, /source-item/); assert.match(fake.insertions[0].payload.text, /revisionId/); assert.doesNotMatch(fake.insertions[0].payload.text, /private/);
  assert.equal(fake.input.draft, 'Existing user text');
  fake.input = { ...fake.input, phase: 'submitting' };
  await assert.rejects(main.inject().openSession('session-bound', context), /稍后/);
  assert.equal(fake.insertions.length, 1);
});

test('a first native view retains the prepared discussion draft across operation release', async () => {
  let entry, main, references = 0, shell, displayed; const navigation = new AbortController();
  vm.runInNewContext(await readFile(new URL('../client/lib/client.js', import.meta.url), 'utf8'), {
    window: { __ModuleLoader__: { load(value) { entry = value; } } },
    document: { createElement() { return { dataset: {}, remove() {} }; }, head: { append() {} } }, URL,
  });
  const scope = { bail(_scope, _event, payload) {
    shell.draft = shell.draft.slice(0, payload.span.start) + payload.text + shell.draft.slice(payload.span.end);
    return true;
  } };
  const plugin = entry.factory(hostRequire);
  plugin.apply({ effect(fn) { fn(); }, locale: { register() { return () => {}; } },
    slots: { inject(_name, fn) { fn(); }, register(options) { if (options.name === 'main') main = options; return () => {}; } },
    layout: { beginNavigation() { return navigation.signal; } },
    sessions: { async using(_id, _options, operation) {
      references++; shell = { phase: 'plain', draft: 'Existing user text', draftRev: 1 };
      try { return await operation({ ready: Promise.resolve(), binding: { ctx: scope } }); }
      finally { if (--references === 0) shell = undefined; }
    } },
    conversation: { input: { for() { return { state: { getSnapshot() { return shell; } } }; } } },
    uiWorkspace: { openSession() {
      references++;
      displayed = shell ?? { phase: 'plain', draft: '', draftRev: 1 };
      navigation.abort();
    } },
  });
  await main.inject().openSession('session-bound', { projectId: 'p', documentId: 'd', sourceSha256: 'a'.repeat(64), workRef: 'plugin-work', revisionId: 'b'.repeat(64), sourceItemId: 'selected-item' });
  assert.equal(references, 1);
  assert.match(displayed.draft, /^Existing user text\n\n讨论当前选中的原文/);
  assert.match(displayed.draft, /selected-item/);
});

test('a later navigation supersedes discussion preparation before any draft insertion', async () => {
  let entry, main; const controller = new AbortController(); const insertions = [], navigations = [];
  vm.runInNewContext(await readFile(new URL('../client/lib/client.js', import.meta.url), 'utf8'), {
    window: { __ModuleLoader__: { load(value) { entry = value; } } },
    document: { createElement() { return { dataset: {}, remove() {} }; }, head: { append() {} } }, URL,
  });
  const scope = { bail(_scope, event, payload) { insertions.push({ event, payload }); return true; } };
  const plugin = entry.factory(hostRequire);
  plugin.apply({ effect(fn) { fn(); }, locale: { register() { return () => {}; } },
    slots: { inject(_name, fn) { fn(); }, register(options) { if (options.name === 'main') main = options; return () => {}; } },
    uiWorkspace: { openSession(id) { navigations.push(id); } },
    layout: { beginNavigation() { return controller.signal; }, selectPanel() {} },
    sessions: { async using(_id, _options, operation) { controller.abort(); return operation({ ready: Promise.resolve(), binding: { ctx: scope } }); } },
    conversation: { input: { for() { return { state: { getSnapshot() { return { phase: 'plain', draft: '', draftRev: 1 }; } } }; } } },
  });
  await assert.rejects(main.inject().openSession('session-bound', { projectId: 'p', documentId: 'd', sourceSha256: 'a'.repeat(64), workRef: 'plugin-work', scope: 'document' }), /另一项工作/);
  assert.deepEqual(navigations, []); assert.deepEqual(insertions, []);
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
