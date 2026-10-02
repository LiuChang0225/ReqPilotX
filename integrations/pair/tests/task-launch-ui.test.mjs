/** Render the built Client in a React18 module host; no Pair server or model is started. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import { randomUUID } from 'node:crypto';
import { JSDOM } from 'jsdom';

test('native task form preserves command across retry and opens only its returned Pair session', async () => {
  const fixture = process.env.PAIR_REACT_ROOT;
  assert.ok(fixture, 'PAIR_REACT_ROOT is required');
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://127.0.0.1:3080/' });
  const prior = new Map(['window', 'document', 'IS_REACT_ACT_ENVIRONMENT'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  Object.defineProperty(globalThis, 'window', { configurable: true, value: dom.window });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: dom.window.document });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const hostRequire = createRequire(fixture + '/package.json');
  const React = hostRequire('react'), { createRoot } = hostRequire('react-dom/client'), { Simulate } = hostRequire('react-dom/test-utils');
  const id = 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc', hash = 'a'.repeat(64);
  dom.window.localStorage.setItem('reqpilotx:view:' + id, JSON.stringify({ projectId: 'p', documentId: 'd' }));
  const calls = [], controls = [], opened = [], panels = [], effects = [], timers = new Set(); let definitions, application, plugin;
  const config = { schema: 'reqpilot-application-host/v1', mode: 'managed', status: 'READY', applicationId: id, instanceId: 'one', serviceOrigin: 'http://127.0.0.1:5555', capabilities: {
    'projects.read': true, 'projects.create': true, 'documents.read': true, 'documents.import': true, 'results.read': true, 'results.feedback': true, 'results.confirm_map': true, 'results.engineer_responses': true, 'results.save_version': true, 'agent.start': true, 'agent.resume': true, 'agent.cancel': true, 'events.agent': false,
  } };
  const json = value => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } });
  async function fetcher(url, init = {}) {
    if (url === '/reqpilotx/config') return json(config);
    if (url.endsWith('/api/projects')) return json([{ id: 'p', name: 'Synthetic' }]);
    if (url.endsWith('/sor-documents')) return json([{ id: 'd', file_name: 'example.pdf', source_sha256: hash }]);
    if (url.endsWith('/sessions')) return json({ sessions: [] });
    if (url === '/reqpilotx/tasks/start') {
      const request = JSON.parse(init.body); calls.push(request);
      if (calls.length === 1) throw Error('temporary transport loss');
      return json({ schema: 'reqpilot-pair-launch/v1', applicationId: id, commandId: request.commandId, projectId: 'p', documentId: 'd', sourceSha256: hash, status: 'ACCEPTED', sessionId: 'session-owned', workspaceId: 'workspace-owned', workRef: 'plugin-' + 'b'.repeat(32), controlCapabilities: {cancel:true,continue:true} });
    }
    if (url.endsWith('/control')) { const value = JSON.parse(init.body); controls.push(value); return json({ commandId: value.commandId, sessionId: value.sessionId,
      workRef: value.workRef, sourceSha256: value.sourceSha256, control: { controlId: value.controlId, status: 'ACCEPTED' } }); }
    if (url.startsWith('/reqpilotx/tasks/')) return json({ applicationId: id, commandId: calls.at(-1).commandId, projectId: 'p', documentId: 'd', sourceSha256: hash, sessionId: 'session-owned', workRef: 'plugin-' + 'b'.repeat(32), controlCapabilities: {cancel:true,continue:true}, execution: { runId: 'real-turn', status: 'SUCCEEDED', connected: true, observedAt: new Date().toISOString(), activity: 'TURN_ENDED', step: 7, toolCalls: 9, retries: 1, toolFailures: 0 } });
    throw Error('Unexpected UI request ' + url);
  }
  const timer = (fn, ms) => { const value = setTimeout(fn, ms); timers.add(value); return value; };
  const sandbox = { window: { __ModuleLoader__: { load(entry) { plugin = entry.factory(hostRequire); } }, location: dom.window.location }, document: dom.window.document,
    localStorage: dom.window.localStorage, fetch: fetcher, Headers, FormData, URL, AbortController, crypto: { randomUUID },
    setTimeout: timer, clearTimeout, setInterval, clearInterval, console };
  const root = createRoot(dom.window.document.getElementById('root'));
  try {
    vm.runInNewContext(await readFile(new URL('../client/lib/client.js', import.meta.url), 'utf8'), sandbox);
    plugin.apply({ effect(fn) { effects.push(fn()); }, locale: { register(_name, copy) { definitions = copy.en; return () => {}; } },
      layout: { beginNavigation() { return new AbortController().signal; }, selectPanel(value) { panels.push(value); } },
      sessions: { async open(value) { opened.push(value); } },
      slots: { inject(_name, fn) { effects.push(fn()); }, register(options, Component) { if (options.name === 'main') application = { options, Component }; return () => {}; } } });
    await React.act(async () => { root.render(React.createElement(application.Component, { t: key => definitions[key], ...application.options.inject() })); });
    const area = dom.window.document.querySelector('textarea'); assert.ok(area, dom.window.document.body.textContent);
    await React.act(async () => Simulate.change(area, { target: { value: 'Organize this source.' } }));
    const form = area.closest('form');
    await React.act(async () => Simulate.submit(form));
    assert.match(dom.window.document.body.textContent, /temporary transport loss/);
    await React.act(async () => Simulate.submit(form));
    assert.equal(calls.length, 2); assert.equal(calls[0].commandId, calls[1].commandId); assert.equal(calls[0].requireMapConfirmation, false);
    assert.equal(calls[0].goal, 'Organize this source.'); assert.equal(calls[0].sourceSha256, hash);
    const button = [...dom.window.document.querySelectorAll('button')].find(el => el.textContent === 'Open task conversation'); assert.ok(button);
    assert.match(dom.window.document.body.textContent, /Turn ended; results still require review/);
    assert.match(dom.window.document.body.textContent, /Model step 7 · Tool calls 9 · Retries 1/);
    assert.equal(opened.length, 0);
    await React.act(async () => Simulate.click(button)); assert.deepEqual(opened, ['session-owned']); assert.deepEqual(panels, [null]);
    const continuation = [...dom.window.document.querySelectorAll('button')].find(el => el.textContent === 'Continue this task'); assert.ok(continuation);
    await React.act(async () => Simulate.click(continuation));
    assert.equal(controls.length, 1); assert.deepEqual([controls[0].action, controls[0].sessionId, controls[0].workRef], ['continue', 'session-owned', 'plugin-' + 'b'.repeat(32)]);
    assert.match(dom.window.document.body.textContent, /Submitted to the original Pair session/);
  } finally {
    await React.act(async () => root.unmount()); for (const effect of effects.reverse()) if (typeof effect === 'function') effect();
    for (const value of timers) clearTimeout(value); dom.window.close();
    for (const [key, descriptor] of prior) if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
  }
});
