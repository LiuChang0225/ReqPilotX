/** ReqPilotX application host: owned runtime by default; explicit readonly connection for development probes. */
import discovery from './application-discovery.cjs';
import { installTaskMcp } from './task-mcp.js';
import { TaskLauncher } from './task-launch.js';
import { ManagedRuntime } from './managed-runtime.js';
import { KnowledgeBridge, KnowledgeError } from './knowledge.js';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
export const name = 'reqpilotx-readonly-host';
export const inject = ['webServer', 'connection', 'subprocess'];
export const PREFIX = '/reqpilotx';
export const APPLICATION_VERSION = '0.1.0';
export const applicationMetadata = Object.freeze({
  id: 'reqpilotx',
  name: { en: 'ReqPilotX', zh: 'ReqPilotX' },
  description: {
    en: 'Evidence-driven customer requirement intake, source structure, requirement items, self-check and reviewed delivery.',
    zh: '面向客户文档的证据驱动需求整理、原文结构、需求条目、自查与可核对交付。',
  },
  domain: 'GEN', stage: 'requirement-analysis', version: APPLICATION_VERSION,
  owner: 'PAIR Team', integrationAuthor: 'PAIR Team', authors: [], panelId: 'reqpilotx',
});
/** Availability only, never model or business acceptance. */
export function applicationReadStatus(runtime) {
  const status = runtime?.snapshot?.status;
  return status === 'READY' ? 'ready' : status === 'ERROR' || status === 'STOPPED' ? 'unavailable' : 'starting';
}
/** Optional Host discovery consumer; a raw DSH without the service keeps its own sidebar. */
export function registerPairApplication(ctx, runtime) {
  const center = typeof ctx.get === 'function' ? ctx.get('pairSkillCenter') : ctx.pairSkillCenter;
  if (!center || typeof center.registerApplication !== 'function') return null;
  ctx.effect(() => center.registerApplication(applicationMetadata, () => applicationReadStatus(runtime)),
    'reqpilotx: application discovery');
  return applicationMetadata;
}

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

/** Business-facing knowledge routes. Original bytes and search stay owned by PAIR. */
async function handleKnowledge(knowledge, url, req, res) {
  const send = (status, value) => { res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' }); res.end(value === undefined ? undefined : JSON.stringify(value)); };
  if (req.method === 'GET' && url.pathname === PREFIX + '/knowledge/inventory') {
    const projectId = url.searchParams.get('projectId') || '';
    if (!projectId) throw new KnowledgeError('INVALID_PROJECT', 422);
    send(200, await knowledge.inventory(projectId)); return true;
  }
  const upload = /^\/reqpilotx\/knowledge\/projects\/([^/]+)\/references$/.exec(url.pathname);
  if (upload && req.method === 'POST') {
    const projectId = decodeURIComponent(upload[1]);
    const name = String(url.searchParams.get('name') || '');
    const kind = String(url.searchParams.get('kind') || 'REFERENCE');
    const chunks = []; let size = 0;
    for await (const chunk of req) { size += chunk.length; if (size > 128 * 1024 * 1024) throw new KnowledgeError('REFERENCE_TOO_LARGE', 413); chunks.push(chunk); }
    send(201, await knowledge.uploadProjectReference(projectId, name, Buffer.concat(chunks), kind)); return true;
  }
  const retry = /^\/reqpilotx\/knowledge\/projects\/([^/]+)\/references\/([^/]+)\/retry$/.exec(url.pathname);
  if (retry && req.method === 'POST') {
    send(200, await knowledge.retryProjectReference(decodeURIComponent(retry[1]), decodeURIComponent(retry[2]))); return true;
  }
  const topics = /^\/reqpilotx\/knowledge\/team\/([^/]+)\/topics$/.exec(url.pathname);
  if (topics && req.method === 'GET') {
    send(200, { versions: await knowledge.teamTopics(decodeURIComponent(topics[1])) }); return true;
  }
  if (req.method === 'GET' && url.pathname === PREFIX + '/knowledge/team') {
    send(200, { libraries: await knowledge.teamInventory() }); return true;
  }
  const task = /^\/reqpilotx\/knowledge\/tasks\/([^/]+)$/.exec(url.pathname);
  if (task && req.method === 'GET') {
    const commandId = decodeURIComponent(task[1]);
    const binding = await knowledge.loadBinding(commandId);
    send(200, { bindingSha256: binding.binding_sha256, mode: binding.mode,
      status: await knowledge.checkBinding(commandId) }); return true;
  }
  const original = /^\/reqpilotx\/knowledge\/tasks\/([^/]+)\/documents\/([^/]+)$/.exec(url.pathname);
  if (original && req.method === 'GET') {
    const commandId = decodeURIComponent(original[1]);
    const documentId = decodeURIComponent(original[2]);
    const value = await knowledge.original(commandId, documentId, url.searchParams.get('version'));
    const bytes = Buffer.from(value.bytes || value);
    res.writeHead(200, { 'content-type': value.document?.mime || 'application/octet-stream',
      'content-disposition': 'inline; filename="' + encodeURIComponent(value.document?.name || documentId) + '"',
      'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
    res.end(bytes); return true;
  }
  return false;
}

/** Authenticated resource broker with an explicit write policy; no Pair credentials go upstream. */
export function createHandler(origin, reject, transport = fetch, runtimeSnapshot, taskLauncher, knowledge = null) {
  return async (req, res) => {
    const denied = reject(req);
    if (denied !== undefined) { res.writeHead(denied); res.end(); return; }
    const url = new URL(req.url, 'http://pair.invalid');
    const state = runtimeSnapshot ? runtimeSnapshot() : { serviceOrigin: origin, mode: 'readonly-connect', status: 'READY' };
    if (knowledge && url.pathname.startsWith(PREFIX + '/knowledge/')) {
      try {
        const result = await handleKnowledge(knowledge, url, req, res);
        if (result !== false) return;
      } catch (error) {
        const status = error instanceof KnowledgeError ? error.status : (error.status || 502);
        res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
        res.end(JSON.stringify({ error: error.code || 'KNOWLEDGE_REQUEST_FAILED', message: error.message }));
        return;
      }
    }
    if (url.pathname === PREFIX + '/tasks' || url.pathname === PREFIX + '/tasks/start' || url.pathname.startsWith(PREFIX + '/tasks/')) {
      try {
        if (!taskLauncher) { res.writeHead(503); res.end(JSON.stringify({ error: 'TASK_LAUNCH_NOT_CONFIGURED' })); return; }
        let result;
        const control = /^\/reqpilotx\/tasks\/([^/]+)\/control$/.exec(url.pathname);
        if ((url.pathname === PREFIX + '/tasks/start' || control) && req.method === 'POST') {
          req.setEncoding('utf8');
          let text = ''; for await (const chunk of req) { text += chunk; if (Buffer.byteLength(text) > 32768) { res.writeHead(413); res.end(); return; } }
          const input = JSON.parse(text);
          if (control) {
            if (input.commandId !== control[1]) { res.writeHead(422); res.end(); return; }
            result = await taskLauncher.control(input);
          } else result = await taskLauncher.start(input);
        } else if (req.method === 'GET' && url.pathname === PREFIX + '/tasks') {
          result = await taskLauncher.list(Object.fromEntries(url.searchParams));
        } else if (req.method === 'GET' && url.pathname !== PREFIX + '/tasks/start' && !control) {
          result = await taskLauncher.inspect(url.pathname.slice((PREFIX + '/tasks/').length));
        } else { res.writeHead(405); res.end(); return; }
        res.writeHead(req.method === 'POST' ? 202 : 200, { 'content-type': 'application/json', 'cache-control': 'no-store' }); res.end(JSON.stringify(result));
      } catch (error) {
        const status = error.status || (error.code === 'ENOENT' ? 404 : error instanceof SyntaxError ? 422 : 502);
        res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
        res.end(JSON.stringify({ error: error.status ? error.message : 'TASK_LAUNCH_NOT_CONFIRMED', retry: 'SAME_COMMAND_ID' }));
      }
      return;
    }
    const canCreate = discovery.allowsWrite(state, req.method, url.pathname);
    if (!['GET', 'HEAD'].includes(req.method) && !canCreate) { res.writeHead(405, { Allow: 'GET, HEAD' }); res.end(); return; }
    if (url.pathname === PREFIX + '/config') {
      res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(req.method === 'HEAD' ? undefined : JSON.stringify(discovery.publicConfiguration(state)));
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
      for (const key of ['content-type', 'content-disposition', 'content-range', 'accept-ranges', 'etag', 'x-reqpilot-revision', 'x-reqpilot-snapshot', 'x-reqpilotx-delivery-sha256', 'x-reqpilotx-export-sha256']) {
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
export function apply(ctx, config = {}, makeRuntime = (scope, settings) => new ManagedRuntime(scope.subprocess, settings)) {
  const connect = config.mode === 'connect' || (!config.mode && config.serviceOrigin);
  const runtime = connect ? null : makeRuntime(ctx, config);
  const knowledgeService = !connect && typeof ctx.get === 'function' ? ctx.get('knowledge') : undefined;
  const applicationId = () => {
    if (!runtime || !runtime.ownerId) return null;
    try { return discovery.publicConfiguration({ ...runtime.snapshot, ownerId: runtime.ownerId }).applicationId; }
    catch { return null; }
  };
  const knowledge = knowledgeService && runtime
    ? new KnowledgeBridge({ service: knowledgeService, runtime, dataRoot: runtime.dataRoot, applicationId }) : null;
  const taskLauncher = runtime && config.capabilityRoot ? new TaskLauncher(runtime, config, () => ({ sessions: ctx.get('sessionController'), workspaces: ctx.get('workspaceController'), defaultModel: ctx.get('agentDefaultModel'), liveStatus: id => ctx.get('agents')?.get(id)?.status }), fetch, knowledge) : null;
  if (taskLauncher && config.taskMcp === true) installTaskMcp(ctx, taskLauncher);
  if (knowledge) ctx.effect(() => () => knowledge.close(), 'reqpilotx: knowledge evidence lifetime');
  const origin = connect ? serviceOrigin(config.serviceOrigin) : null;
  if (runtime) {
    if (typeof ctx.inject === 'function') ctx.inject(['pairSkillCenter'], scope => { registerPairApplication(scope, runtime); });
    else registerPairApplication(ctx, runtime);
    ctx.effect(() => () => runtime.stop(), 'reqpilotx: managed runtime');
  }
  if (taskLauncher) ctx.effect(() => () => taskLauncher.close(), 'reqpilotx: task launch lifetime');
  ctx.effect(() => ctx.webServer.register({ kind: 'prefix', path: PREFIX,
    handler: createHandler(origin, req => ctx.connection.requestRejection(req), fetch, runtime ? () => ({ ...runtime.snapshot, ownerId: runtime.ownerId,
      workbenchCapture: config.workbenchCapture === true,
      taskModel: taskLauncher?.currentModel() || config.agentModel,
      taskWorkflow: taskLauncher?.workflowDescriptor,
      taskStartIssue: taskLauncher?.startIssue() || null,
      taskLookupAvailable: Boolean(taskLauncher),
      taskLaunchReady: taskLauncher?.startAvailable() === true, taskCancelReady: taskLauncher?.canCancel() === true,
      taskContinueReady: taskLauncher?.canContinue() === true, taskReconnectReady: taskLauncher?.canReconnect() === true,
      knowledgeAvailable: knowledge?.available() === true }) : undefined, taskLauncher, knowledge),
  }), 'reqpilotx: application route');
  if (runtime) void runtime.start().then(async () => { if (runtime.snapshot.status === 'READY') await taskLauncher?.initialize(); }).catch(() => { if (taskLauncher) taskLauncher.ready = false; });
}
