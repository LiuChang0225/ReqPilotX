import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createHandler, serviceOrigin, proxyTarget, apply } from '../host/src/index.js';

async function serve(handler) {
  const server = createServer(handler); server.listen(0, '127.0.0.1'); await once(server, 'listening');
  return { url: 'http://127.0.0.1:' + server.address().port, close: () => new Promise(resolve => server.close(resolve)) };
}
test('fixed-origin validation and route normalization reject caller-selected destinations', () => {
  assert.equal(serviceOrigin('http://localhost:9000'), 'http://localhost:9000');
  for (const value of [undefined, '', 'file:///tmp', 'https://u:p@example.com', 'https://example.com/path', 'https://example.com/?x=1']) assert.throws(() => serviceOrigin(value));
  assert.equal(proxyTarget('/reqpilotx/service/api/projects?p=1', 'https://service.invalid').href, 'https://service.invalid/api/projects?p=1');
  assert.equal(proxyTarget('/reqpilotx/service/api/../../outside', 'https://service.invalid'), null);
  assert.equal(proxyTarget('/reqpilotx/service/https://other.invalid', 'https://service.invalid'), null);
});
test('authenticated readonly proxy preserves ranges, revision and file bytes; never forwards Pair credentials', async () => {
  const observed = [];
  const backend = await serve((req, res) => {
    observed.push({ method: req.method, cookie: req.headers.cookie, authorization: req.headers.authorization, range: req.headers.range });
    if (req.url.endsWith('/redirect')) { res.writeHead(302, { Location: 'https://other.invalid/' }); res.end(); return; }
    res.writeHead(206, { 'content-type': 'application/pdf', 'content-range': 'bytes 0-2/6', 'accept-ranges': 'bytes', 'x-reqpilot-revision': 'rev1' }); res.end(Buffer.from([0, 1, 2]));
  });
  const proxy = await serve(createHandler(backend.url, req => req.headers.cookie === 'pair=ok' ? undefined : 401));
  try {
    assert.equal((await fetch(proxy.url + '/reqpilotx/config')).status, 401);
    assert.equal((await fetch(proxy.url + '/reqpilotx/service/api/test', { method: 'POST', headers: { cookie: 'pair=ok' } })).status, 405);
    const r = await fetch(proxy.url + '/reqpilotx/service/api/test', { headers: { cookie: 'pair=ok', authorization: 'not-an-upstream-credential', range: 'bytes=0-2' } });
    assert.equal(r.status, 206); assert.equal(r.headers.get('content-range'), 'bytes 0-2/6'); assert.equal(r.headers.get('x-reqpilot-revision'), 'rev1');
    assert.deepEqual([...new Uint8Array(await r.arrayBuffer())], [0, 1, 2]);
    assert.deepEqual(observed, [{ method: 'GET', cookie: undefined, authorization: undefined, range: 'bytes=0-2' }]);
    assert.equal((await fetch(proxy.url + '/reqpilotx/service/api/redirect', { headers: { cookie: 'pair=ok' } })).status, 502);
  } finally { await proxy.close(); await backend.close(); }
});
test('Cordis disposal unregisters owned route and owns no backend process', () => {
  let disposed = false; let stop;
  apply({ connection: { requestRejection() {} }, webServer: { register(row) { assert.equal(row.path, '/reqpilotx'); return () => { disposed = true; }; } }, effect(fn) { stop = fn(); } }, { serviceOrigin: 'http://localhost:9000' });
  assert.equal(disposed, false); stop(); assert.equal(disposed, true);
});

test('owned application permits only project creation/import writes and reports starting state', async () => {
  let state = { mode: 'managed', status: 'STARTING', message: 'Preparing' };
  const seen = [];
  const backend = await serve(async (req, res) => { let body = ''; for await (const part of req) body += part; seen.push({ url: req.url, body, type: req.headers['content-type'] }); res.writeHead(200, { 'content-type': 'application/json' }); res.end('{"id":"created"}'); });
  const proxy = await serve(createHandler(null, () => undefined, fetch, () => state));
  try {
    assert.equal((await fetch(proxy.url + '/reqpilotx/service/api/projects')).status, 503);
    assert.equal((await (await fetch(proxy.url + '/reqpilotx/config')).json()).status, 'STARTING');
    state = { mode: 'managed', status: 'READY', serviceOrigin: backend.url };
    const r = await fetch(proxy.url + '/reqpilotx/service/api/projects', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{"name":"demo"}' }); assert.equal(r.status, 200);
    const form = new FormData(); form.append('file', new Blob(['source']), 'demo.pdf');
    assert.equal((await fetch(proxy.url + '/reqpilotx/service/api/projects/p/sor-documents', { method: 'POST', body: form })).status, 200);
    assert.equal((await fetch(proxy.url + '/reqpilotx/service/api/projects/p/sor-documents/d', { method: 'DELETE' })).status, 405);
    assert.equal((await fetch(proxy.url + '/reqpilotx/service/api/runtime', { method: 'POST' })).status, 405);
    assert.equal(seen[0].body, '{"name":"demo"}'); assert.match(seen[1].body, /source/); assert.match(seen[1].type, /multipart\/form-data; boundary=/);
  } finally { await proxy.close(); await backend.close(); }
});
