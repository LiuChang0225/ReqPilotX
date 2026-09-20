/** ReqPilotX application host: owned runtime by default; explicit readonly connection for development probes. */
import { ManagedRuntime } from './managed-runtime.js';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
export const name = 'reqpilotx-readonly-host';
export const inject = ['webServer', 'connection', 'subprocess'];
export const PREFIX = '/reqpilotx';

/** Validate the explicit service origin; credentials and path routing are not accepted. */
export function serviceOrigin(value) {
  if (typeof value !== 'string' || !value.trim()) throw new Error('ReqPilotX serviceOrigin is required.');
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('ReqPilotX serviceOrigin must be an HTTP(S) origin without credentials or a path.');
  }
  return url.origin;
}

/** Map a same-origin proxy request to the fixed business service, never a caller-selected host. */
export function proxyTarget(requestUrl, origin) {
  const url = new URL(requestUrl, 'http://pair.invalid');
  const marker = PREFIX + '/service';
  if (!url.pathname.startsWith(marker + '/api/')) return null;
  const path = url.pathname.slice(marker.length);
  return new URL(path + url.search, origin);
}

/** A read-only proxy handler; forwards Range but no Pair cookies or credentials upstream. */
export function createHandler(origin, reject, transport = fetch, runtimeSnapshot) {
  return async (req, res) => {
    const denied = reject(req);
    if (denied !== undefined) { res.writeHead(denied); res.end(); return; }
    const url = new URL(req.url, 'http://pair.invalid');
    const state = runtimeSnapshot ? runtimeSnapshot() : { serviceOrigin: origin, mode: 'readonly-connect', status: 'READY' };
    const canCreate = state.mode === 'managed' && req.method === 'POST' && /^\/reqpilotx\/service\/api\/projects(?:\/[^/]+\/sor-documents)?$/.test(url.pathname);
    if (!['GET', 'HEAD'].includes(req.method) && !canCreate) { res.writeHead(405, { Allow: 'GET, HEAD' }); res.end(); return; }
    if (url.pathname === PREFIX + '/config') {
      res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(req.method === 'HEAD' ? undefined : JSON.stringify(state));
      return;
    }
    if (state.status !== 'READY') { res.writeHead(503, { 'content-type': 'application/json' }); res.end(JSON.stringify({ detail: state.message || 'ReqPilotX is starting.' })); return; }
    const target = proxyTarget(req.url, state.serviceOrigin);
    if (!target) { res.writeHead(404); res.end(); return; }
    const abort = new AbortController();
    const closed = () => { if (!res.writableFinished) abort.abort(); };
    res.on('close', closed);
    try {
      const headers = {};
      for (const key of ['range', 'if-range', 'accept', 'content-type']) if (typeof req.headers[key] === 'string') headers[key] = req.headers[key];
      const upstream = await transport(target, { method: req.method, headers, redirect: 'manual', signal: abort.signal, ...(req.method === 'POST' ? { body: req, duplex: 'half' } : {}) });
      // Never redirect browser credentials to another endpoint, including an upstream redirect.
      if (upstream.status >= 300 && upstream.status < 400) {
        await upstream.body?.cancel(); res.writeHead(502); res.end('Unexpected service redirect.'); return;
      }
      const responseHeaders = { 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' };
      for (const key of ['content-type', 'content-disposition', 'content-range', 'accept-ranges', 'etag', 'x-reqpilot-revision', 'x-reqpilot-snapshot']) {
        const value = upstream.headers.get(key); if (value !== null) responseHeaders[key] = value;
      }
      res.writeHead(upstream.status, responseHeaders);
      if (req.method === 'HEAD' || !upstream.body) { await upstream.body?.cancel(); res.end(); }
      else await pipeline(Readable.fromWeb(upstream.body), res);
    } catch (error) {
      if (!abort.signal.aborted) {
        if (!res.headersSent) { res.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' }); res.end('ReqPilotX service is unavailable.'); }
        else res.destroy(error);
      }
    } finally { res.off('close', closed); }
  };
}

/** Mount the route and own runtime lifecycle through the Cordis effect lifetime. */
export function apply(ctx, config = {}) {
  const connect = config.mode === 'connect' || (!config.mode && config.serviceOrigin);
  const runtime = connect ? null : new ManagedRuntime(ctx.subprocess, config);
  const origin = connect ? serviceOrigin(config.serviceOrigin) : null;
  if (runtime) ctx.effect(() => () => runtime.stop(), 'reqpilotx: managed runtime');
  ctx.effect(() => ctx.webServer.register({ kind: 'prefix', path: PREFIX,
    handler: createHandler(origin, req => ctx.connection.requestRejection(req), fetch, runtime ? () => runtime.snapshot : undefined),
  }), 'reqpilotx: application route');
  if (runtime) void runtime.start();
}
