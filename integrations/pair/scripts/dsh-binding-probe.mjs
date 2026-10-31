/** Loaded only by the isolated DSH smoke profile. Calls real Workspace/Session create; never calls Session.prompt. */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createHash, randomUUID } from 'node:crypto';
import { installTaskMcp } from '../host/src/task-mcp.js';
import { TaskLauncher } from '../host/src/task-launch.js';
export const name = 'reqpilot-dsh-binding-probe';
export const inject = ['sessionController', 'workspaceController', 'agentPresets', 'subprocess', 'llm'];
export function apply(ctx) {
  const exit = ctx.get('appExit');
  void run(ctx).then(() => exit(0)).catch(async error => {
    await writeFile(join(process.env.REQPILOT_PROBE_OUTPUT, 'failure.json'), JSON.stringify({ status: 'FAIL', message: error.message, stack: error.stack }, null, 2));
    exit(1);
  });
}
async function run(ctx) {
  const output = process.env.REQPILOT_PROBE_OUTPUT;
  const python = process.env.REQPILOT_PROBE_PYTHON;
  const capabilityRoot = process.env.REQPILOT_PROBE_CAPABILITY;
  const bytes = await readFile(join(output, 'synthetic.pdf'));
  const sourceSha = createHash('sha256').update(bytes).digest('hex');
  const admission = process.env.REQPILOT_PROBE_ADMISSION === '1';
  const recover = process.env.REQPILOT_PROBE_RECOVER === '1';
  const mcp = process.env.REQPILOT_PROBE_MCP === '1';
  const control = process.env.REQPILOT_PROBE_CONTROL === '1';
  const cancel = process.env.REQPILOT_PROBE_CANCEL === '1';
  let mcpTools = [], activeInput;
  let scriptedCalls = 0;
  if (admission) {
    const { LlmAdapter } = await import(pathToFileURL(join(process.env.REQPILOT_PROBE_PAIR, 'packages/llm/llm/src/index.ts')).href);
    class ProbeAdapter extends LlmAdapter {
      async *stream(options) {
        if (options.provider !== 'reqpilot-probe' || options.model !== 'noop') throw Error('Unexpected scripted route');
        scriptedCalls++;
        mcpTools = (options.tools || []).map(t => t.name).filter(n => n.startsWith('mcp__reqpilot__'));
        if (mcp && !mcpTools.includes('mcp__reqpilot__task_open')) throw Error('Task MCP tools missing before the first model request: ' + JSON.stringify(mcpTools));
        if (mcp && scriptedCalls === 1) {
          const record = JSON.parse(await readFile(join(runtime.dataRoot, 'task-launches', activeInput.commandId + '.json'), 'utf8'));
          const block = { type: 'tool-call', id: 'probe-task-open', name: 'mcp__reqpilot__task_open', arguments: JSON.stringify({ mode: 'resume', task_id: record.prepared.task_id }) };
          yield { type: 'block-start', index: 0, blockType: 'tool-call' };
          yield { type: 'block-end', index: 0, block };
          yield { type: 'finish', reason: { kind: 'tool-calls' } }; return;
        }
        if (cancel && scriptedCalls === 3) {
          await new Promise(resolve => {
            if (options.signal?.aborted) resolve();
            else options.signal?.addEventListener('abort', resolve, { once: true });
          });
          return;
        }
        const text = 'PROBE_ONLY: no document extraction performed.';
        yield { type: 'block-start', index: 0, blockType: 'text' };
        yield { type: 'text-delta', index: 0, text };
        yield { type: 'block-end', index: 0, block: { type: 'text', text } };
        yield { type: 'usage', usage: { inputTokens: 1, outputTokens: 1 } };
        yield { type: 'finish', reason: { kind: 'stop' } };
      }
    }
    ctx.llm.registerAdapter(['reqpilot-probe'], new ProbeAdapter());
  }
  const works = new Map();
  const server = createServer(async (req, res) => {
    const send = value => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
    if (req.url === '/api/projects/p/sor-documents') { send([{ id: 'd', file_name: 'synthetic.pdf', source_sha256: sourceSha }]); return; }
    if (req.url.endsWith('/file')) { res.writeHead(200, { 'content-type': 'application/pdf' }); res.end(bytes); return; }
    if (req.method === 'POST' && req.url.endsWith('/plugin-workbench')) {
      req.setEncoding('utf8'); let body = ''; for await (const part of req) body += part; const payload = JSON.parse(body);
      if (!works.has(payload.session_id)) works.set(payload.session_id, { session_id: payload.session_id, source_sha256: sourceSha,
        project_id: 'p', document_id: 'd', revision_id: null, revision_number: 0, map_confirmation_required: false, stages: [], versions: [], feedback: [] });
      send(works.get(payload.session_id)); return;
    }
    const value = works.get(req.url.split('/').at(-1)); if (value) send(value); else { res.writeHead(404); res.end(); }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const runtime = { dataRoot: join(output, 'data'), python, ownerId: randomUUID(), snapshot: { status: 'READY', mode: 'managed', serviceOrigin: 'http://127.0.0.1:' + server.address().port }, async command(argv, env = {}) {
    await mkdir(this.dataRoot, { recursive: true });
    const child = ctx.subprocess.spawn({ argv, cwd: this.dataRoot, env, stdio: { stdin: 'ignore', stdout: { maxBytes: 65536 }, stderr: { maxBytes: 65536 } }, graceMs: 5000 });
    const result = await child.done;
    if (result.exitCode !== 0) throw Error(child.collected.stdout.readFrom(0).text + child.collected.stderr.readFrom(0).text);
    return child.collected.stdout.readFrom(0).text;
  } };
  const sessions = ctx.get('sessionController'), workspaces = ctx.get('workspaceController');
  const createRequests = [], blockedPrompts = []; let realPromptCalls = 0;
  const launcher = new TaskLauncher(runtime, { capabilityRoot, agentPreset: 'standard', taskMcp: mcp }, () => ({ workspaces, sessions: {
    ...(recover ? { inspect: id => sessions.inspect(id) } : {}),
    ...(control ? { inspect: id => sessions.inspect(id) } : {}),
    ...(cancel ? { cancel: request => sessions.cancel(request) } : {}),
    async create(request) {
      createRequests.push(request); const value = await sessions.create(request);
      if (admission) await sessions.selectModel({ sessionId: value.sessionId, provider: 'reqpilot-probe', model: 'noop' });
      return value;
    },
    async prompt(request, signal) {
      blockedPrompts.push({ sessionId: request.sessionId, requestId: request.requestId });
      if (!admission) throw Error('MODEL_REQUEST_BLOCKED_BY_PROBE');
      realPromptCalls++; const value = await sessions.prompt(request, signal);
      if (realPromptCalls === 1) throw Error('ADMISSION_ACK_LOST_BY_PROBE');
      return value;
    },
  } }));
  if (mcp) installTaskMcp(ctx, launcher);
  try {
    await launcher.initialize();
    const input = { commandId: randomUUID(), projectId: 'p', documentId: 'd', sourceSha256: sourceSha, goal: 'Prepare this synthetic document task; do not run a model in the probe.' };
    activeInput = input;
    const awaitTurnEnd = async (id, count = 1) => {
      for (let i = 0; i < 600; i++) {
        const inspection = await sessions.inspect(id);
        const ended = inspection.events.filter(event => event.type === 'turn/end')[count - 1];
        if (ended) { if (ended.data.reason.kind !== 'completed') throw Error('Scripted turn failed: ' + (ended.data.reason.error?.message || ended.data.reason.kind)); return inspection; }
        await new Promise(ok => setTimeout(ok, 50));
      }
      throw Error('Scripted DSH turn did not finish');
    };
    let inspection;
    for (let i = 0; i < 2; i++) {
      if (recover && i === 1) runtime.snapshot.serviceOrigin = 'http://127.0.0.1:1';
      try {
        await launcher.start(input);
        if (!admission || i === 0) throw Error('Probe unexpectedly confirmed an intercepted request');
      } catch (error) {
        const expected = admission ? 'ADMISSION_ACK_LOST_BY_PROBE' : 'MODEL_REQUEST_BLOCKED_BY_PROBE';
        if (error.message !== expected || (admission && i !== 0)) throw error;
      }
      if (admission) inspection = await awaitTurnEnd(createRequests[0].sessionId);
    }
    const receipt = await launcher.inspect(input.commandId);
    let continuation = null, cancelled = null, continuedMessages = 0, cancelledTurnReason = null;
    if (control) {
      continuation = await launcher.control({ commandId: input.commandId, controlId: randomUUID(), action: 'continue',
        sessionId: receipt.sessionId, workRef: receipt.workRef, sourceSha256: sourceSha });
      if (cancel) {
        for (let i = 0; i < 200 && scriptedCalls < 3; i++) await new Promise(ok => setTimeout(ok, 25));
        if (scriptedCalls !== 3) throw Error('Scripted continuation did not enter its active model turn');
        cancelled = await launcher.control({ commandId: input.commandId, controlId: randomUUID(), action: 'cancel',
          sessionId: receipt.sessionId, workRef: receipt.workRef, sourceSha256: sourceSha });
        for (let i = 0; i < 400; i++) {
          inspection = await sessions.inspect(receipt.sessionId);
          const ended = inspection.events.filter(event => event.type === 'turn/end')[1];
          if (ended) { cancelledTurnReason = ended.data.reason.kind; break; }
          await new Promise(ok => setTimeout(ok, 25));
        }
        if (cancelled.control.status !== 'ACCEPTED' || !cancelledTurnReason || cancelledTurnReason === 'completed') throw Error('DSH did not cancel the active continuation turn');
      } else inspection = await awaitTurnEnd(receipt.sessionId, 2);
      continuedMessages = inspection.events.filter(event => event.type === 'user/message' && event.data?.source?.rpcId?.startsWith('reqpilotx-control-')).length;
      if (continuation.control.status !== 'ACCEPTED' || continuedMessages !== 1) throw Error('Same-task DSH continuation was not durably admitted');
    }
    if (receipt.status !== (admission ? 'ACCEPTED' : 'START_REQUESTED') || createRequests.length !== (recover || control ? 1 : 2) || blockedPrompts.length !== (recover || control ? 1 : 2) + (control ? 1 : 0)
      || (!recover && !control && (blockedPrompts[0].sessionId !== blockedPrompts[1].sessionId || blockedPrompts[0].requestId !== blockedPrompts[1].requestId))) throw Error('Native binding identity did not survive replay');
    const userMessages = inspection?.events.filter(event => event.type === 'user/message' && event.data?.source?.rpcId === blockedPrompts[0]?.requestId).length || 0;
    if (admission && (scriptedCalls !== (mcp ? control ? 3 : 2 : 1) || userMessages !== 1)) throw Error('Real DSH admission replay created a duplicate user message or turn');
    const mcpResults = inspection?.events.filter(e => e.type === 'tool/result').flatMap(e => e.data.message.content).filter(b => b.type === 'tool-result' && b.toolCallId === 'probe-task-open') || [];
    if (mcp && (mcpResults.length !== 1 || mcpResults[0].isError || !mcpResults[0].content.some(b => b.type === 'text' && b.text.includes('RESUMED')))) throw Error('MCP task_open did not actually resume its prepared task');
    await writeFile(join(output, 'receipt.json'), JSON.stringify({ status: 'PASS', source_sha256: sourceSha, result: receipt,
      real_services: ['workspaceController.create', 'sessionController.create', 'subprocess', ...(admission ? ['sessionController.selectModel', 'sessionController.prompt', 'sessionController.inspect'] : [])],
      mcp_tools_before_first_request: mcpTools, mcp_task_open_resumed: mcpResults.length === 1, source_service: 'synthetic HTTP', acceptance_recovered_after_endpoint_change: recover, session_create_calls: createRequests.length, unique_sessions: new Set(createRequests.map(x => x.sessionId)).size,
      intercepted_or_forwarded_prompt_calls: blockedPrompts.length, real_prompt_calls: realPromptCalls, remote_model_calls: 0, scripted_adapter_calls: scriptedCalls, durable_user_messages: userMessages,
      continuation_accepted: continuation?.control.status === 'ACCEPTED', continuation_user_messages: continuedMessages,
      cancel_accepted: cancelled?.control.status === 'ACCEPTED', cancelled_turn_reason: cancelledTurnReason,
      scope: admission ? 'real DSH create/prompt/durable dedup with a scripted LLM adapter; no remote model, document extraction or external Pair UI acceptance' : 'real DSH workspace/session adoption; prompt blocked; no remote model or external Pair UI acceptance' }, null, 2));
  } finally { await launcher.close(); await new Promise(ok => server.close(ok)); }
}
