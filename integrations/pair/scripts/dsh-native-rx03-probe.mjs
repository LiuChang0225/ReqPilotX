/** Real DSH Session probe for RX03 bounded correction through the task MCP route. */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { installTaskMcp } from '../host/src/task-mcp.js';
import { TaskLauncher } from '../host/src/task-launch.js';

export const name = 'reqpilot-native-rx03-probe';
export const inject = ['sessionController', 'workspaceController', 'agentPresets', 'subprocess', 'agentDefaultModel'];

export function apply(ctx) {
  const exit = ctx.get('appExit');
  void run(ctx).then(() => exit(0)).catch(async error => {
    await writeFile(join(process.env.REQPILOT_NATIVE_OUTPUT, 'failure.json'),
      JSON.stringify({ status: 'FAIL', message: error.message, stack: error.stack }, null, 2));
    exit(1);
  });
}

async function run(ctx) {
  const output = process.env.REQPILOT_NATIVE_OUTPUT;
  const python = process.env.REQPILOT_NATIVE_PYTHON;
  const capabilityRoot = process.env.REQPILOT_NATIVE_CAPABILITY;
  const repo = process.env.REQPILOT_NATIVE_REPO;
  const commandId = process.env.REQPILOT_NATIVE_COMMAND;
  const sourcePath = process.env.REQPILOT_NATIVE_SOURCE;
  const mapping = process.env.REQPILOT_NATIVE_MAPPING;
  const scenarioPath = process.env.REQPILOT_NATIVE_SCENARIO;
  const setup = process.env.REQPILOT_NATIVE_SETUP;
  const bytes = await readFile(sourcePath);
  const sourceSha = createHash('sha256').update(bytes).digest('hex');
  const dataRoot = join(output, 'owned');
  await mkdir(join(dataRoot, 'task-launches'), { recursive: true, mode: 0o700 });

  const works = new Map();
  const service = createServer(async (req, res) => {
    const send = value => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
    if (req.url === '/api/projects/p/sor-documents') { send([{ id: 'd', file_name: 'synthetic.pdf', source_sha256: sourceSha }]); return; }
    if (req.url.endsWith('/file')) { res.writeHead(200, { 'content-type': 'application/pdf' }); res.end(bytes); return; }
    if (req.method === 'POST' && req.url.endsWith('/plugin-workbench')) {
      let body = ''; for await (const part of req) body += part; const payload = JSON.parse(body);
      if (!works.has(payload.session_id)) works.set(payload.session_id, { schema_version: 'reqpilot-plugin-workbench/v1',
        session_id: payload.session_id, project_id: 'p', document_id: 'd', source_sha256: sourceSha,
        revision_id: null, revision_number: 0, map_confirmation_required: false, confirmed_map_sha256: null,
        stages: [], versions: [], feedback: [] });
      send(works.get(payload.session_id)); return;
    }
    const value = works.get(req.url.split('/').at(-1)); if (value) send(value); else { res.writeHead(404); res.end(); }
  });
  service.listen(0, '127.0.0.1'); await once(service, 'listening');
  const runtime = { dataRoot, python, ownerId: randomUUID(),
    snapshot: { status: 'READY', mode: 'managed', serviceOrigin: 'http://127.0.0.1:' + service.address().port },
    async command(argv, env = {}) {
      await mkdir(dataRoot, { recursive: true });
      const child = ctx.subprocess.spawn({ argv, cwd: dataRoot, env,
        stdio: { stdin: 'ignore', stdout: { maxBytes: 65536 }, stderr: { maxBytes: 65536 } }, graceMs: 5000 });
      const result = await child.done;
      if (result.exitCode !== 0) throw Error(child.collected.stdout.readFrom(0).text + child.collected.stderr.readFrom(0).text);
      return child.collected.stdout.readFrom(0).text;
    } };

  const sessions = ctx.get('sessionController'), workspaces = ctx.get('workspaceController');
  const launcher = new TaskLauncher(runtime, { capabilityRoot, agentPreset: 'standard', taskMcp: true },
    () => ({ workspaces, defaultModel: ctx.get('agentDefaultModel'), sessions }));
  installTaskMcp(ctx, launcher);
  try {
    await launcher.initialize();
    const input = { commandId, projectId: 'p', documentId: 'd', sourceSha256: sourceSha, goal: '' };
    await launcher.start(input);
    const record = JSON.parse(await readFile(join(dataRoot, 'task-launches', commandId + '.json'), 'utf8'));
    const prepared = record.prepared;
    if (!prepared?.task_id || !prepared.workspace || !prepared.task_root) throw Error('Task preparation did not persist the task workspace identity');
    const setupArgs = [python, setup,
      '--repo', repo, '--workspace', prepared.workspace, '--task-root', prepared.task_root,
      '--bundle', capabilityRoot, '--mapping', mapping, '--scenario', scenarioPath];
    const setupChild = ctx.subprocess.spawn({ argv: setupArgs, cwd: output,
      env: { PATH: process.env.PATH || '/usr/bin:/bin', HOME: process.env.HOME || output, LANG: process.env.LANG || 'C',
        PYTHONPATH: [join(repo, 'packages/engine/src'), join(repo, 'apps/server/src'), join(repo, 'prototype'),
          join(repo, 'plugins/reqpilot/scripts'), join(repo, 'plugins/reqpilot/tests')].join(':') },
      stdio: { stdin: 'ignore', stdout: { maxBytes: 65536 }, stderr: { maxBytes: 65536 } }, graceMs: 120000 });
    const setupResult = await setupChild.done;
    if (setupResult.exitCode !== 0) throw Error('RX03 setup failed: ' + setupChild.collected.stdout.readFrom(0).text + setupChild.collected.stderr.readFrom(0).text);
    const scenario = JSON.parse(await readFile(scenarioPath, 'utf8'));
    let inspection;
    for (let i = 0; i < 2400; i++) {
      inspection = await sessions.inspect(record.sessionId);
      const ended = inspection.events.filter(event => event.type === 'turn/end');
      if (ended.length) {
        if (ended.at(-1).data.reason.kind !== 'completed') throw Error('Scripted turn failed: ' + JSON.stringify(ended.at(-1).data.reason));
        break;
      }
      await new Promise(ok => setTimeout(ok, 50));
    }
    if (!inspection || !inspection.events.some(event => event.type === 'turn/end')) throw Error('Scripted DSH turn did not finish');
    const final = await launcher.inspect(commandId);
    const workflow = final.workflowState;
    const toolResults = inspection.events.filter(event => event.type === 'tool/result')
      .map(event => event.data?.message).filter(Boolean);
    const resultText = toolResults.map(value => JSON.stringify(value)).join('\n');
    const requestHeader = inspection.events.find(event => event.type === 'request/header')?.data?.header;
    const mcpTools = (requestHeader?.tools || []).map(tool => tool.name).filter(name => name.startsWith('mcp__reqpilot__'));
    for (const required of ['mcp__reqpilot__task_open', 'mcp__reqpilot__review_apply', 'mcp__reqpilot__task_deliver']) {
      if (!mcpTools.includes(required)) throw Error(required + ' was not visible before the first model request');
    }
    if (!resultText.includes('RESUMED')) throw Error('task_open RESUME result missing');
    if (!resultText.includes('REVIEW_OPERATION_COMMITTED')) throw Error('review_apply committed result missing');
    if (!resultText.includes('local_delivery_ready')) throw Error('task_deliver result missing');
    if (!workflow || workflow.stages.find(stage => stage.id === 'DELIVERABLE')?.status !== 'SAVED'
      || workflow.ready_to_review !== true || workflow.human_acceptance !== 'NOT_CLAIMED')
      throw Error('Saved workflow/delivery state did not reach review readiness: ' + JSON.stringify(workflow));
    const assistantText = inspection.events.filter(event => event.type === 'assistant/message')
      .map(event => JSON.stringify(event.data)).join('\n');
    if (!assistantText.includes('RX03_NATIVE_PROBE')) throw Error('Scripted final text missing');
    const eventBody = JSON.stringify({ session: inspection.session, events: inspection.events }, null, 2);
    await writeFile(join(output, 'session-events.json'), eventBody + '\n');
    await writeFile(join(output, 'workflow-state.json'), JSON.stringify(workflow, null, 2) + '\n');
    const evidence = { schema: 'reqpilot-rx03-native-scenario/v1',
      source_sha256: sourceSha, task_id: final.taskId, session_id: final.sessionId, work_ref: final.workRef,
      model_selection: final.selectedAgentModel, request_header_model: requestHeader?.config,
      mcp_tools_before_first_request: mcpTools,
      scripted_turns: [
        { step: 1, assistant: { kind: 'tool-call', name: 'mcp__reqpilot__task_open',
          arguments: { mode: 'resume', task_id: final.taskId } }, tool_result_status: 'RESUMED' },
        { step: 2, assistant: { kind: 'tool-call', name: 'mcp__reqpilot__review_apply',
          arguments: { task_id: final.taskId, command_id: scenario.commands.review_apply,
            self_review_finding: scenario.self_review_finding,
            changes: { map_revision: { decisions: scenario.map_decisions } } } }, tool_result_status: 'REVIEW_OPERATION_COMMITTED' },
        { step: 3, assistant: { kind: 'tool-call', name: 'mcp__reqpilot__task_deliver',
          arguments: { task_id: final.taskId, command_id: scenario.commands.deliver } }, tool_result_status: 'local_delivery_ready' },
        { step: 4, assistant: { kind: 'text', text: 'RX03_NATIVE_PROBE: bounded correction and delivery saved; human acceptance remains unclaimed.' } },
      ],
      review_before: scenario.review_sha256, expected_workflow: { DELIVERABLE: 'SAVED', ready_to_review: true, human_acceptance: 'NOT_CLAIMED' },
      observed_workflow: Object.fromEntries(workflow.stages.map(stage => [stage.id, stage.status])),
      projection: 'PUBLIC_SESSION_INSPECT_EVENTS; exact event body hash in receipt' };
    await writeFile(join(output, 'scenario.json'), JSON.stringify(evidence, null, 2) + '\n');
    const result = { status: 'PASS', schema: 'reqpilot-rx03-native-session/v1',
      task_id: final.taskId, session_id: final.sessionId, work_ref: final.workRef,
      command_id: commandId, source_sha256: sourceSha, model_selection: final.modelSelection,
      selected_agent_model: final.selectedAgentModel, session_event_count: inspection.events.length,
      session_events_sha256: createHash('sha256').update(eventBody).digest('hex'),
      mcp_tools_before_first_request: mcpTools, request_header_model: requestHeader?.config,
      review_apply_committed: true, delivery_ready: true, workflow_after: workflow,
      human_acceptance: 'NOT_CLAIMED', prose_advanced_workflow: false,
      scripted_model_adapter: false, mock_openai_http: true, real_model: false, remote_model_calls: 0,
      limitation: 'Loopback OpenAI-compatible scripted endpoint and real DSH Session/Workspace/MCP; no provider model, customer document or semantic business acceptance.' };
    await writeFile(join(output, 'receipt.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
  } finally {
    await launcher.close();
    await new Promise(ok => service.close(ok));
  }
}
