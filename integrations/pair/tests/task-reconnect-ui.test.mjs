/** Actual built Client in a synthetic React DOM host; no installed Pair or model. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import { randomUUID } from 'node:crypto';
import { JSDOM } from 'jsdom';

test('built reconnect button adopts refreshed capabilities and scopes retries to each owned generation', async () => {
  const fixture = process.env.PAIR_UI_REACT_ROOT || process.env.PAIR_REACT_ROOT;
  assert.ok(fixture, 'PAIR_UI_REACT_ROOT or PAIR_REACT_ROOT is required');
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://127.0.0.1:3080/' });
  const prior = new Map(['window', 'document', 'IS_REACT_ACT_ENVIRONMENT'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  Object.defineProperty(globalThis, 'window', { configurable: true, value: dom.window });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: dom.window.document });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const hostRequire = createRequire(fixture + '/package.json');
  const React = hostRequire('react'), { createRoot } = hostRequire('react-dom/client');
  const applicationId = 'reqpilotx-pair:' + randomUUID(), commandId = randomUUID(), hash = 'a'.repeat(64), workRef = 'plugin-' + 'b'.repeat(32);
  let reconnect = true, instanceId = 'two', application, definitions, plugin;
  const controls = [], effects = [], timers = new Map(); let timerId = 0;
  const caps = { 'projects.read': true, 'projects.create': true, 'documents.read': true, 'documents.import': true, 'results.read': true, 'results.feedback': true,
    'results.confirm_map': true, 'results.engineer_responses': true, 'results.save_version': true, 'agent.start': true, 'agent.resume': true, 'agent.cancel': true, 'agent.reconnect': true, 'events.agent': false };
  const receipt = () => ({ schema: 'reqpilot-pair-launch/v1', commandId, applicationId, projectId: 'p', documentId: 'd', sourceSha256: hash,
    status: 'ACCEPTED', sessionId: 'session-owned', workspaceId: 'workspace-owned', workRef,
    controlCapabilities: { cancel: true, continue: !reconnect, reconnect },
    execution: { runId: 'same-finished-turn', status: 'SUCCEEDED', connected: true, activity: 'TURN_ENDED', step: 1, toolCalls: 1, retries: 0, toolFailures: 0 } });
  dom.window.localStorage.setItem('reqpilotx:view:' + applicationId, JSON.stringify({ projectId: 'p', documentId: 'd' }));
  dom.window.localStorage.setItem('reqpilotx:intent:' + applicationId + ':p:d:' + hash, JSON.stringify({ commandId, projectId: 'p', documentId: 'd', sourceSha256: hash, goal: 'Synthetic task' }));
  const json = value => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } });
  const fetcher = async (url, init = {}) => {
    if (url === '/reqpilotx/config') return json({ schema: 'reqpilot-application-host/v1', mode: 'managed', status: 'READY', applicationId, instanceId,
      serviceOrigin: 'http://127.0.0.1:' + (instanceId === 'two' ? '5556' : '5557'), capabilities: caps });
    if (url.endsWith('/api/projects')) return json([{ id: 'p', name: 'Synthetic' }]);
    if (url.endsWith('/sor-documents')) return json([{ id: 'd', project_id: 'p', file_name: 'source.pdf', source_sha256: hash }]);
    if (url.endsWith('/sessions')) return json({ sessions: [] });
    if (url.endsWith('/control')) { const input = JSON.parse(init.body); controls.push(input); reconnect = false; return json({ ...receipt(), control: { controlId: input.controlId, status: 'ACCEPTED' } }); }
    if (url.startsWith('/reqpilotx/tasks/')) return json(receipt());
    throw Error('Unexpected UI request ' + url);
  };
  const sandbox = { window: { __ModuleLoader__: { load(entry) { plugin = entry.factory(hostRequire); } }, location: dom.window.location }, document: dom.window.document,
    localStorage: dom.window.localStorage, fetch: fetcher, Headers, FormData, URL, AbortController, crypto: { randomUUID },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); },
    setInterval() { return ++timerId; }, clearInterval() {}, console };
  const root = createRoot(dom.window.document.getElementById('root'));
  try {
    vm.runInNewContext(await readFile(new URL('../client/lib/client.js', import.meta.url), 'utf8'), sandbox);
    plugin.apply({ effect(fn) { effects.push(fn()); }, locale: { register(_name, copy) { definitions = copy.en; return () => {}; } },
      slots: { inject(_name, fn) { effects.push(fn()); }, register(options, Component) { if (options.name === 'main') application = { options, Component }; return () => {}; } } });
    await React.act(async () => root.render(React.createElement(application.Component, { t: key => definitions[key], ...application.options.inject() })));
    const button = () => [...dom.window.document.querySelectorAll('button')].find(el => el.textContent === 'Verify and reconnect this task');
    assert.ok(button(), dom.window.document.body.textContent);
    await React.act(async () => button().click());
    assert.equal(controls[0].action, 'reconnect'); assert.equal(controls[0].sessionId, 'session-owned');
    assert.ok([...dom.window.document.querySelectorAll('button')].some(el => el.textContent === 'Continue this task'));
    instanceId = 'three'; reconnect = true;
    const poll = [...timers.entries()].find(([_id, value]) => value.ms === 2000); assert.ok(poll);
    timers.delete(poll[0]); await React.act(async () => poll[1].fn());
    assert.ok(button(), dom.window.document.body.textContent);
    await React.act(async () => button().click());
    assert.equal(controls.length, 2); assert.notEqual(controls[0].controlId, controls[1].controlId);
    for (const value of controls) assert.deepEqual([value.commandId, value.workRef, value.sourceSha256], [commandId, workRef, hash]);
  } finally {
    await React.act(async () => root.unmount()); for (const effect of effects.reverse()) if (typeof effect === 'function') effect();
    dom.window.close(); for (const [key, descriptor] of prior) if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
  }
});
