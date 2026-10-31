/** Current sources in the existing React18 host; no release bundle or model run. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';
import { JSDOM } from 'jsdom';
import { rolldown } from 'rolldown';

test('Pair source UI recovers creation and PDF import after loss, reload and service-port change', async () => {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://127.0.0.1:3080/' });
  const prior = new Map(['window', 'document', 'IS_REACT_ACT_ENVIRONMENT'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  Object.defineProperty(globalThis, 'window', { configurable: true, value: dom.window });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: dom.window.document });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const fixture = process.env.PAIR_REACT_ROOT || fileURLToPath(new URL('./react-host', import.meta.url));
  const hostRequire = createRequire(fixture + '/package.json');
  const React = hostRequire('react'), { createRoot } = hostRequire('react-dom/client'), { Simulate } = hostRequire('react-dom/test-utils');
  const id = 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc';
  const config = { schema: 'reqpilot-application-host/v1', mode: 'managed', status: 'READY', applicationId: id,
    serviceOrigin: 'http://127.0.0.1:5555', capabilities: Object.fromEntries(['projects.read', 'projects.create', 'documents.read', 'documents.import', 'results.read', 'results.feedback', 'results.confirm_map', 'results.engineer_responses', 'results.save_version', 'agent.start', 'agent.resume', 'agent.reconnect', 'agent.cancel', 'events.agent'].map(key => [key, ['projects.read', 'projects.create', 'documents.read', 'documents.import', 'results.read'].includes(key)])) };
  const creates = [], imports = [], queries = [], projects = [], documents = [], effects = [], timers = new Set();
  let definitions, application, plugin, reachable = false, listReachable = true;
  const savedProjects = new Map(), savedDocuments = new Map();
  const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
  async function fetcher(url, init = {}) {
    if (url === '/reqpilotx/config') return json(config);
    if (url.endsWith('/api/projects') && init.method === 'POST') {
      const body = JSON.parse(init.body); creates.push(body);
      const saved = { id: 'p', name: body.name, customer_name: body.customer_name };
      savedProjects.set(body.command_id, saved); projects.push(saved);
      throw Error('creation reply lost');
    }
    if (url.includes('/creation-commands/')) { queries.push(url); return reachable ? json(savedProjects.get(url.split('/').at(-1)), 200) : json({ detail: 'unavailable' }, 503); }
    if (url.includes('/import-commands/')) { queries.push(url); return reachable ? json(savedDocuments.get(url.split('/').at(-1)), 200) : json({ detail: 'unavailable' }, 503); }
    if (url.endsWith('/api/projects')) return listReachable ? json(projects) : json({ detail: 'list unavailable' }, 503);
    if (url.endsWith('/sor-documents') && init.method === 'POST') {
      const form = init.body, file = form.get('file');
      assert.ok(form.get('command_id')); assert.equal(init.headers.has('idempotency-key'), false);
      const sha = Buffer.from(await webcrypto.subtle.digest('SHA-256', await file.arrayBuffer())).toString('hex');
      imports.push({ command: form.get('command_id'), sha });
      const saved = { id: 'd', project_id: 'p', file_name: file.name, source_size: file.size, source_sha256: sha };
      savedDocuments.set(form.get('command_id'), saved); documents.push(saved);
      throw Error('import reply lost');
    }
    if (url.endsWith('/sor-documents')) return listReachable ? json(documents) : json({ detail: 'list unavailable' }, 503);
    if (url.endsWith('/sessions')) return json({ sessions: [] });
    throw Error('Unexpected source UI request ' + url);
  }
  const sandbox = { window: dom.window, document: dom.window.document, localStorage: dom.window.localStorage,
    fetch: fetcher, Headers, FormData, File, FileReader: dom.window.FileReader, URL, AbortController, crypto: webcrypto,
    setTimeout: (fn, ms) => { const timer = setTimeout(fn, ms); timers.add(timer); return timer; }, clearTimeout,
    setInterval: (fn, ms) => { const timer = setInterval(fn, ms); timers.add(timer); return timer; }, clearInterval, console };
  const context = vm.createContext(sandbox);
  async function sourceModule(path) {
    const module = { exports: {} };
    // Generate only these small API modules in memory, never a release artifact.
    const build = await rolldown({ input: path });
    const { output } = await build.generate({ format: 'cjs' }); await build.close();
    const wrapper = vm.runInContext('(function(module,exports){' + output.find(item => item.type === 'chunk').code + '\n})', context, { filename: path });
    wrapper(module, module.exports);
    return module.exports;
  }
  const apiRoot = fileURLToPath(new URL('../../../packages/workbench-ui/src/shared/api/', import.meta.url));
  const projectApi = await sourceModule(resolve(apiRoot, 'projectDocuments.ts'));
  const requestApi = await sourceModule(resolve(apiRoot, 'client.ts'));
  const discovery = hostRequire(fileURLToPath(new URL('../host/src/application-discovery.cjs', import.meta.url)));
  sandbox.panelModule = { exports: { ...projectApi, requestWithTransport: requestApi.requestWithTransport,
    ReqPilotXReadonlyPanel: () => null, createReadonlyHttpClient: () => ({}), validWorkSelection: () => true } };
  sandbox.require = name => hostRequire(name);
  sandbox.module = { exports: {} }; sandbox.panelCss = ''; Object.assign(sandbox, discovery);
  vm.runInContext(readFileSync(new URL('../client/src/client.cjs', import.meta.url), 'utf8') + '\n' + readFileSync(new URL('../client/src/catalog.cjs', import.meta.url), 'utf8'), context);
  plugin = sandbox.module.exports;
  plugin.apply({ effect(fn) { effects.push(fn()); }, locale: { register(_name, copy) { definitions = copy.en; return () => {}; } },
    layout: { beginNavigation() { return new AbortController().signal; }, selectPanel() {} }, sessions: { async open() {} },
    slots: { inject(_name, fn) { effects.push(fn()); }, register(options, Component) { if (options.name === 'main') application = { options, Component }; return () => {}; } } });
  let root;
  const mount = async () => { root = createRoot(dom.window.document.getElementById('root'));
    await React.act(async () => root.render(React.createElement(application.Component, { t: key => definitions[key], ...application.options.inject() }))); };
  const button = text => [...dom.window.document.querySelectorAll('button')].find(item => item.textContent === text);
  try {
    await mount();
    await React.act(async () => Simulate.click(button('New project')));
    const fields = dom.window.document.querySelectorAll('form input');
    await React.act(async () => { Simulate.change(fields[0], { target: { value: 'Synthetic' } }); Simulate.change(fields[1], { target: { value: 'Customer' } }); });
    const form = fields[0].closest('form');
    await React.act(async () => { Simulate.submit(form); Simulate.submit(form); });
    assert.equal(creates.length, 1, dom.window.document.body.textContent); assert.match(dom.window.document.body.textContent, /creation reply lost/);
    const command = creates[0].command_id; assert.ok(command);
    await React.act(async () => root.unmount());
    config.serviceOrigin = 'http://127.0.0.1:6666'; reachable = true; listReachable = false;
    await mount();
    assert.equal(creates.length, 1); assert.ok(queries.some(url => url.endsWith('/creation-commands/' + command)));
    assert.equal(dom.window.document.querySelector('form'), null, 'saved creation closes the form even when list refresh fails');
    // Reload a committed import whose receipt query was unavailable. No second
    // command is posted and the recovered document becomes the selection.
    listReachable = true; reachable = false;
    const input = dom.window.document.querySelector('input[type=file]'); assert.ok(input);
    const file = new File(['%PDF-synthetic'], 'source.pdf', { type: 'application/pdf' });
    Object.defineProperty(input, 'files', { configurable: true, value: [file] });
    await React.act(async () => {
      Simulate.change(input); Simulate.change(input);
      for (let i = 0; i < 200 && !imports.length; i++) await new Promise(done => setTimeout(done, 5));
    });
    assert.equal(imports.length, 1, dom.window.document.body.textContent); assert.match(dom.window.document.body.textContent, /import reply lost/);
    await React.act(async () => root.unmount()); reachable = true;
    await mount();
    assert.equal(imports.length, 1); assert.ok(queries.some(url => url.endsWith('/import-commands/' + imports[0].command)));
    assert.match(dom.window.document.body.textContent, /source.pdf/);
    assert.ok(dom.window.document.querySelector('iframe'), 'saved PDF is selected after reload');
    assert.ok(![...Array(dom.window.localStorage.length)].some((_, i) => dom.window.localStorage.key(i).startsWith('reqpilotx:entry:v1:')));
    // Once receipts are resolved, a later ordinary reload must still restore
    // the saved document selection rather than overwriting it with an empty ID.
    await React.act(async () => root.unmount());
    const queryCount = queries.length;
    await mount();
    assert.equal(queries.length, queryCount, 'no pending command remains');
    assert.ok(dom.window.document.querySelector('iframe'), 'ordinary reload restores the selected PDF');
    await React.act(async () => Simulate.click(button('Back to documents')));
    await React.act(async () => root.unmount());
    await mount();
    assert.equal(dom.window.document.querySelector('iframe'), null, 'explicitly returning to the source list remains a cleared selection');
  } finally {
    if (root) await React.act(async () => root.unmount());
    for (const effect of effects.reverse()) if (typeof effect === 'function') effect();
    for (const timer of timers) { clearTimeout(timer); clearInterval(timer); }
    dom.window.close();
    for (const [key, descriptor] of prior) if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
  }
});
