/** Real DSH two-task MCP isolation probe with one unrelated control Session. */
import { appendFileSync, mkdirSync } from 'node:fs';
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createHash, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { installTaskMcp } from '../host/src/task-mcp.js';
import { TaskLauncher } from '../host/src/task-launch.js';

export const name = 'reqpilot-dsh-mcp-isolation-probe';
export const inject = ['sessionController', 'workspaceController', 'agentPresets', 'subprocess', 'llm'];

export function apply(ctx) {
  const output = process.env.REQPILOT_PROBE_OUTPUT;
  const exit = ctx.get('appExit');
  const disposalLog = join(output, 'agent-context-disposals.jsonl');
  ctx.on('agent/created', ({ agent }) => {
    agent.ctx.effect(() => () => {
      appendFileSync(disposalLog, JSON.stringify({ session_id: agent.id, disposed: true }) + '\n');
    }, 'reqpilot isolation probe context witness');
  });
  void run(ctx).then(() => exit(0)).catch(async error => {
    await writeFile(join(output, 'failure.json'), JSON.stringify({
      status: 'FAIL', message: error.message, stack: error.stack,
    }, null, 2));
    exit(1);
  });
}

const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const sleep = ms => new Promise(resolvePromise => setTimeout(resolvePromise, ms));

function finishText(text) {
  return [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text },
    { type: 'block-end', index: 0, block: { type: 'text', text } },
    { type: 'usage', usage: { inputTokens: 1, outputTokens: 1 } },
    { type: 'finish', reason: { kind: 'stop' } },
  ];
}

function toolCall(id, name, args) {
  const block = { type: 'tool-call', id, name, arguments: JSON.stringify(args) };
  return [
    { type: 'block-start', index: 0, blockType: 'tool-call' },
    { type: 'block-end', index: 0, block },
    { type: 'finish', reason: { kind: 'tool-calls' } },
  ];
}

function activeMcpProcesses(output) {
  const lines = execFileSync('ps', ['-axo', 'pid=,ppid=,command='], { encoding: 'utf8' }).split('\n');
  return lines.filter(line => line.includes(output) && line.includes('mcp_server.py')).map(line => {
    const match = line.trim().match(/^(\d+)\s+(\d+)\s+/);
    if (!match) throw new Error(`Could not parse MCP process row: ${line}`);
    const workspace = line.match(/--working-root\s+(\S+)/)?.[1];
    if (!workspace) throw new Error(`MCP process row has no working root: ${line}`);
    return { pid: Number(match[1]), ppid: Number(match[2]), workspace };
  });
}

function toolResult(inspection, callId) {
  const blocks = inspection.events
    .filter(event => event.type === 'tool/result')
    .flatMap(event => event.data.message.content)
    .filter(block => block.type === 'tool-result' && block.toolCallId === callId);
  if (blocks.length !== 1 || blocks[0].isError) {
    throw new Error(`Expected one successful ${callId} result, got ${blocks.length}`);
  }
  const text = blocks[0].content.find(block => block.type === 'text')?.text;
  if (!text) throw new Error(`Tool ${callId} returned no JSON text`);
  return JSON.parse(text);
}

async function run(ctx) {
  const output = process.env.REQPILOT_PROBE_OUTPUT;
  const python = process.env.REQPILOT_PROBE_PYTHON;
  const capabilityRoot = process.env.REQPILOT_PROBE_CAPABILITY;
  const pairRoot = process.env.REQPILOT_PROBE_PAIR;
  const documents = new Map();
  for (const suffix of ['a', 'b']) {
    const bytes = await readFile(join(output, `synthetic-${suffix}.pdf`));
    documents.set(`p-${suffix}/d-${suffix}`, {
      bytes, sha: sha256(bytes), fileName: `synthetic-${suffix}.pdf`,
    });
  }

  const workbenches = new Map();
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    const send = value => {
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify(value));
    };
    const listing = url.pathname.match(/^\/api\/projects\/([^/]+)\/sor-documents$/);
    if (listing) {
      const project = listing[1];
      const found = documents.get(`${project}/d-${project.at(-1)}`);
      send(found ? [{ id: `d-${project.at(-1)}`, file_name: found.fileName, source_sha256: found.sha }] : []);
      return;
    }
    const file = url.pathname.match(/^\/api\/projects\/([^/]+)\/sor-documents\/([^/]+)\/file$/);
    if (file) {
      const found = documents.get(`${file[1]}/${file[2]}`);
      if (!found) { response.writeHead(404); response.end(); return; }
      response.writeHead(200, { 'content-type': 'application/pdf' });
      response.end(found.bytes);
      return;
    }
    if (request.method === 'POST' && url.pathname.endsWith('/plugin-workbench')) {
      const target = url.pathname.match(/^\/api\/projects\/([^/]+)\/sor-documents\/([^/]+)\/agent-structure\/delivery\/plugin-workbench$/);
      const found = target && documents.get(`${target[1]}/${target[2]}`);
      if (!found) { response.writeHead(404); response.end(); return; }
      request.setEncoding('utf8');
      let body = '';
      for await (const part of request) body += part;
      const payload = JSON.parse(body);
      if (!workbenches.has(payload.session_id)) {
        workbenches.set(payload.session_id, {
          session_id: payload.session_id,
          source_sha256: found.sha,
          project_id: target[1],
          document_id: target[2],
          revision_id: null,
          revision_number: 0,
          map_confirmation_required: false,
          stages: [], versions: [], feedback: [],
        });
      }
      send(workbenches.get(payload.session_id));
      return;
    }
    const workbench = workbenches.get(url.pathname.split('/').at(-1));
    if (workbench) send(workbench);
    else { response.writeHead(404); response.end(); }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');

  let runtime;
  const firstWave = new Map();
  let releaseFirstWave;
  const firstWaveReady = new Promise(resolvePromise => { releaseFirstWave = resolvePromise; });
  let firstWaveReleased = false;
  let activeProcesses = [];
  const requestTools = new Map();
  const requestAllTools = new Map();
  const callCounts = new Map();

  const launchRecordFor = async sessionId => {
    const root = join(runtime.dataRoot, 'task-launches');
    const files = await readdir(root).catch(error => {
      if (error.code === 'ENOENT') return [];
      throw error;
    });
    for (const file of files.filter(value => value.endsWith('.json'))) {
      const record = JSON.parse(await readFile(join(root, file), 'utf8'));
      if (record.sessionId === sessionId) return record;
    }
    return null;
  };

  const { LlmAdapter } = await import(pathToFileURL(join(pairRoot, 'packages/llm/llm/src/index.ts')).href);
  class IsolationAdapter extends LlmAdapter {
    async *stream(options) {
      if (options.provider !== 'reqpilot-isolation-probe' || options.model !== 'noop') {
        throw new Error('Unexpected scripted model route');
      }
      const sessionId = String(options.sessionId);
      const record = await launchRecordFor(sessionId);
      const role = record ? 'task' : 'unrelated';
      const allTools = (options.tools || []).map(tool => tool.name);
      const tools = allTools.filter(name => name.startsWith('mcp__reqpilot__'));
      const call = (callCounts.get(sessionId) || 0) + 1;
      callCounts.set(sessionId, call);
      requestTools.set(`${sessionId}:${call}`, tools);
      requestAllTools.set(`${sessionId}:${call}`, allTools);

      if (call === 1) {
        if (role === 'task' && (!tools.includes('mcp__reqpilot__task_open') || !tools.includes('mcp__reqpilot__task_inspect'))) {
          throw new Error(`Task Session ${sessionId} lacks ReqPilot tools on its first request`);
        }
        if (role === 'unrelated' && tools.length !== 0) {
          throw new Error(`Unrelated Session ${sessionId} observed ReqPilot tools`);
        }
        firstWave.set(sessionId, { role, tools, workspace: record?.prepared?.workspace ?? null });
        if (firstWave.size === 3 && !firstWaveReleased) {
          const taskRows = [...firstWave.values()].filter(row => row.role === 'task');
          const controlRows = [...firstWave.values()].filter(row => row.role === 'unrelated');
          if (taskRows.length !== 2 || controlRows.length !== 1) {
            throw new Error(`First request wave was not two task Sessions plus one control: ${JSON.stringify([...firstWave.values()])}`);
          }
          if (JSON.stringify(taskRows[0].tools) !== JSON.stringify(taskRows[1].tools)) {
            throw new Error('The two task Sessions did not expose the same ReqPilot tool names');
          }
          activeProcesses = activeMcpProcesses(output);
          if (activeProcesses.length !== 2) {
            throw new Error(`Expected two live task MCP stdio processes, found ${activeProcesses.length}`);
          }
          if (new Set(activeProcesses.map(row => row.workspace)).size !== 2
            || taskRows.some(row => !activeProcesses.some(process => process.workspace === row.workspace))) {
            throw new Error('The two live MCP stdio processes were not bound to the two task workspaces');
          }
          firstWaveReleased = true;
          releaseFirstWave();
        }
        await Promise.race([
          firstWaveReady,
          sleep(30000).then(() => { throw new Error('Three-Session first request barrier timed out'); }),
        ]);
      }

      if (role === 'unrelated') {
        if (call !== 1) throw new Error(`Unrelated Session unexpectedly made model call ${call}`);
        yield* finishText('UNRELATED_SESSION_COMPLETE');
        return;
      }
      const suffix = record.projectId.at(-1);
      if (call === 1) {
        yield* toolCall(`isolation-${suffix}-open`, 'mcp__reqpilot__task_open', {
          mode: 'resume', task_id: record.prepared.task_id,
        });
        return;
      }
      if (call === 2) {
        yield* toolCall(`isolation-${suffix}-inspect`, 'mcp__reqpilot__task_inspect', {});
        return;
      }
      if (call === 3) {
        yield* finishText(`TASK_${suffix.toUpperCase()}_COMPLETE`);
        return;
      }
      if (call === 4) {
        const firstNonMcp = requestAllTools.get(`${sessionId}:1`).filter(name => !name.startsWith('mcp__reqpilot__'));
        const currentNonMcp = allTools.filter(name => !name.startsWith('mcp__reqpilot__'));
        if (tools.length !== 0 || firstNonMcp.length === 0
          || JSON.stringify(firstNonMcp) !== JSON.stringify(currentNonMcp)) {
          throw new Error(`Task Session ${sessionId} did not retain only its unrelated tools after adapter disposal`);
        }
        yield* finishText(`TASK_${suffix.toUpperCase()}_POST_ADAPTER_DISPOSAL_COMPLETE`);
        return;
      }
      throw new Error(`Task Session ${sessionId} unexpectedly made model call ${call}`);
    }
  }
  ctx.llm.registerAdapter(['reqpilot-isolation-probe'], new IsolationAdapter());

  runtime = {
    dataRoot: join(output, 'data'),
    python,
    ownerId: randomUUID(),
    snapshot: {
      status: 'READY', mode: 'managed',
      serviceOrigin: `http://127.0.0.1:${server.address().port}`,
    },
    async command(argv, env = {}) {
      await mkdir(this.dataRoot, { recursive: true });
      const child = ctx.subprocess.spawn({
        argv, cwd: this.dataRoot, env,
        stdio: { stdin: 'ignore', stdout: { maxBytes: 65536 }, stderr: { maxBytes: 65536 } },
        graceMs: 5000,
      });
      const result = await child.done;
      const stdout = child.collected.stdout.readFrom(0).text;
      const stderr = child.collected.stderr.readFrom(0).text;
      if (result.exitCode !== 0) throw new Error(stdout + stderr);
      return stdout;
    },
  };

  const sessions = ctx.get('sessionController');
  const workspaces = ctx.get('workspaceController');
  const createdSessions = [];
  const launcher = new TaskLauncher(runtime, {
    capabilityRoot, agentPreset: 'standard', taskMcp: true,
  }, () => ({
    workspaces,
    sessions: {
      inspect: id => sessions.inspect(id),
      async create(request) {
        const created = await sessions.create(request);
        createdSessions.push({ session_id: created.sessionId, workspace_id: request.workspaceId, kind: 'task' });
        await sessions.selectModel({
          sessionId: created.sessionId, provider: 'reqpilot-isolation-probe', model: 'noop',
        });
        return created;
      },
      prompt: (request, signal) => sessions.prompt(request, signal),
    },
  }));
  const disposeTaskMcp = installTaskMcp(ctx, launcher);

  try {
    await launcher.initialize();
    const unrelatedPath = join(output, 'unrelated-workspace');
    mkdirSync(unrelatedPath, { recursive: true });
    const unrelatedWorkspace = await workspaces.create({ path: unrelatedPath });
    const unrelatedSessionId = `session-${randomUUID()}`;
    await sessions.create({
      sessionId: unrelatedSessionId,
      workspaceId: unrelatedWorkspace.workspace.workspaceId,
      agentPreset: 'standard',
    });
    createdSessions.push({
      session_id: unrelatedSessionId,
      workspace_id: unrelatedWorkspace.workspace.workspaceId,
      kind: 'unrelated',
    });
    await sessions.selectModel({
      sessionId: unrelatedSessionId, provider: 'reqpilot-isolation-probe', model: 'noop',
    });

    const inputs = ['a', 'b'].map(suffix => ({
      commandId: randomUUID(),
      projectId: `p-${suffix}`,
      documentId: `d-${suffix}`,
      sourceSha256: documents.get(`p-${suffix}/d-${suffix}`).sha,
      goal: `Prepare isolated synthetic task ${suffix.toUpperCase()}; do not perform extraction.`,
    }));
    const [receiptA, receiptB, unrelatedPrompt] = await Promise.all([
      launcher.start(inputs[0]),
      launcher.start(inputs[1]),
      sessions.prompt({
        sessionId: unrelatedSessionId,
        requestId: `reqpilotx-control-${randomUUID()}`,
        mode: 'queue',
        content: [{ type: 'text', text: 'Complete the unrelated isolation control turn.' }],
      }, new AbortController().signal),
    ]);
    if (unrelatedPrompt.accepted !== true) throw new Error('Unrelated control prompt was not accepted');

    const awaitTurnEnd = async (sessionId, minimumTurns = 1) => {
      for (let attempt = 0; attempt < 800; attempt++) {
        const inspection = await sessions.inspect(sessionId);
        const endings = inspection.events.filter(event => event.type === 'turn/end');
        const ended = endings.at(-1);
        if (endings.length >= minimumTurns) {
          if (ended.data.reason.kind !== 'completed') {
            throw new Error(`Session ${sessionId} ended as ${ended.data.reason.kind}: ${ended.data.reason.error?.message || ''}`);
          }
          return inspection;
        }
        await sleep(50);
      }
      throw new Error(`Session ${sessionId} did not complete`);
    };
    const inspections = await Promise.all([
      awaitTurnEnd(receiptA.sessionId),
      awaitTurnEnd(receiptB.sessionId),
      awaitTurnEnd(unrelatedSessionId),
    ]);
    const records = await Promise.all([receiptA, receiptB].map(receipt => launchRecordFor(receipt.sessionId)));
    if (records.some(record => !record)) throw new Error('Missing durable task launch record');
    if (new Set(records.map(record => record.prepared.workspace)).size !== 2
      || new Set(records.map(record => record.prepared.task_id)).size !== 2
      || new Set(records.map(record => record.prepared.revision)).size !== 2
      || new Set(records.map(record => record.workspaceId)).size !== 2) {
      throw new Error('The two task launches did not keep distinct workspace, task, revision and DSH workspace identities');
    }

    const taskEvidence = records.map((record, index) => {
      const suffix = record.projectId.at(-1);
      const open = toolResult(inspections[index], `isolation-${suffix}-open`);
      const inspect = toolResult(inspections[index], `isolation-${suffix}-inspect`);
      if (open.status !== 'RESUMED' || inspect.status !== 'READY' || open.task_id !== record.prepared.task_id
        || inspect.task_id !== record.prepared.task_id
        || open.revision !== record.prepared.revision
        || inspect.revision !== record.prepared.revision
        || resolve(open.task_root).startsWith(resolve(record.prepared.workspace) + '/') !== true
        || resolve(inspect.task_root).startsWith(resolve(record.prepared.workspace) + '/') !== true) {
        throw new Error(`Task ${suffix} tool results did not stay bound to its prepared task/revision/workspace`);
      }
      const other = records[1 - index];
      if (inspect.task_id === other.prepared.task_id || inspect.revision === other.prepared.revision
        || resolve(inspect.task_root).startsWith(resolve(other.prepared.workspace) + '/')) {
        throw new Error(`Task ${suffix} inspect result crossed into the other task`);
      }
      return {
        suffix,
        session_id: record.sessionId,
        workspace_id: record.workspaceId,
        workspace: record.prepared.workspace,
        task_id: record.prepared.task_id,
        revision: record.prepared.revision,
        task_open_status: open.status,
        implicit_task_inspect_status: inspect.status,
        implicit_task_inspect_task_id: inspect.task_id,
        implicit_task_inspect_revision: inspect.revision,
        mcp_tools_first_request: requestTools.get(`${record.sessionId}:1`),
        model_calls: callCounts.get(record.sessionId),
        turn_end: 'completed',
      };
    });
    const unrelatedToolEvents = inspections[2].events.filter(event => event.type === 'tool/call' || event.type === 'tool/result');
    if (unrelatedToolEvents.length !== 0 || callCounts.get(unrelatedSessionId) !== 1
      || (requestTools.get(`${unrelatedSessionId}:1`) || []).length !== 0) {
      throw new Error('Unrelated control Session observed or called ReqPilot tools');
    }
    if (activeProcesses.length !== 2) throw new Error('The active MCP stdio process witness was lost');

    await disposeTaskMcp();
    const afterAdapterDisposal = activeMcpProcesses(output);
    if (afterAdapterDisposal.length !== 0) throw new Error('Task MCP stdio survived adapter disposal');
    const agents = ctx.get('agents');
    for (const record of records) {
      const agent = agents?.get(record.sessionId);
      if (!agent) throw new Error(`Task Agent ${record.sessionId} was disposed with the adapter`);
      const tools = agent.ctx.get('tools');
      if (tools.get('mcp__reqpilot__task_open', agent) || tools.get('mcp__reqpilot__task_inspect', agent)) {
        throw new Error(`Task Agent ${record.sessionId} retained ReqPilot tools after adapter disposal`);
      }
      const retained = requestAllTools.get(`${record.sessionId}:1`).filter(name => !name.startsWith('mcp__reqpilot__'));
      if (retained.length === 0 || retained.some(name => !tools.get(name, agent))) {
        throw new Error(`Task Agent ${record.sessionId} lost unrelated tools with the adapter`);
      }
    }
    const disposalBeforeExit = await readFile(join(output, 'agent-context-disposals.jsonl'), 'utf8').catch(error => {
      if (error.code === 'ENOENT') return '';
      throw error;
    });
    if (disposalBeforeExit.trim()) throw new Error('An Agent context was disposed with the task MCP adapter');
    const followups = await Promise.all(records.map(record => sessions.prompt({
      sessionId: record.sessionId,
      requestId: `reqpilotx-post-adapter-${randomUUID()}`,
      mode: 'queue',
      content: [{ type: 'text', text: 'Complete one control turn after the task MCP adapter is disposed.' }],
    }, new AbortController().signal)));
    if (followups.some(value => value.accepted !== true)) throw new Error('A live task Agent rejected its post-disposal turn');
    await Promise.all(records.map(record => awaitTurnEnd(record.sessionId, 2)));
    for (const evidence of taskEvidence) {
      evidence.mcp_tools_after_adapter_disposal = requestTools.get(`${evidence.session_id}:4`);
      evidence.retained_non_mcp_tool_count = requestAllTools.get(`${evidence.session_id}:4`)
        .filter(name => !name.startsWith('mcp__reqpilot__')).length;
      evidence.model_calls = callCounts.get(evidence.session_id);
      evidence.post_adapter_disposal_turn_end = 'completed';
    }

    const expectedDisposals = [receiptA.sessionId, receiptB.sessionId, unrelatedSessionId];
    await writeFile(join(output, 'receipt-pre-disposal.json'), JSON.stringify({
      status: 'PASS_BEFORE_DSH_DISPOSAL',
      bundle_sha256: records[0].bundleSha,
      real_dsh_profile: 'web',
      real_services: [
        'workspaceController.create', 'sessionController.create', 'sessionController.selectModel',
        'sessionController.prompt', 'sessionController.inspect', 'subprocess',
      ],
      task_sessions: taskEvidence,
      unrelated_session: {
        session_id: unrelatedSessionId,
        workspace_id: unrelatedWorkspace.workspace.workspaceId,
        mcp_tools_first_request: [],
        tool_events: 0,
        model_calls: 1,
        turn_end: 'completed',
      },
      simultaneous_same_name_tool_set: taskEvidence[0].mcp_tools_first_request,
      active_mcp_stdio_processes: activeProcesses,
      adapter_disposed_while_task_agents_live: true,
      mcp_stdio_processes_after_adapter_disposal: afterAdapterDisposal,
      expected_agent_context_disposals: expectedDisposals,
      created_sessions: createdSessions,
      remote_model_calls: 0,
      scripted_adapter_calls: [...callCounts.values()].reduce((sum, count) => sum + count, 0),
      scope: 'real DSH Session/Workspace/Agent loop, official MCP stdio bridge, and live Host-adapter disposal with scripted keyless LLM; no extraction or remote model',
    }, null, 2) + '\n');
  } finally {
    await launcher.close();
    await new Promise(resolvePromise => server.close(resolvePromise));
  }
}
